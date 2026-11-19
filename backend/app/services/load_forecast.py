"""
Forward projection of ACWR under hypothetical training.

ACWR is unusual among this app's metrics in being *deterministic* a fortnight out
once you fix a training choice — ATL and CTL are EWMAs, so tomorrow's pair is a
pure function of today's pair and tomorrow's load. That makes the ratio a decision
variable ("what does a week of this do to me?") rather than only a report card,
which is the whole reason this module exists.

WHY THE RECURSION AND NOT A ROLLING MEAN
----------------------------------------
`metrics_engine` computes

    atl = a_atl * load + (1 - a_atl) * atl_prev        a_atl = 1 - e^(-1/7)
    ctl = a_ctl * load + (1 - a_ctl) * ctl_prev        a_ctl = 1 - e^(-1/42)

so the state is Markovian: today's (atl, ctl) is a *complete sufficient statistic*
for the future. No trailing window of TRIMP is needed to project — two scalars are.
Projecting with simple 7/42-day rolling means instead (the obvious first guess)
produces a forecast that does NOT join the measured series continuously, and the
discontinuity lands exactly at "today", which is where the eye goes.

The same alphas are imported from metrics_engine rather than redefined, so the
forecast can never silently drift from the series it continues.

WARMUP
------
`metrics_engine` pins acwr to 1.0 until WARMUP_DAYS of load history exist, and that
sentinel is a *display* convention, not a measurement. Projection is only offered
once the warmup has passed; otherwise the cone would extend a flat 1.0 that means
"unknown".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.acwr import ACWR_HIGH, ACWR_LOW
from app.models import DailyMetric, Workout
from app.services.metrics_engine import ALPHA_ATL, ALPHA_CTL, WARMUP_DAYS
from app.timeutils import local_day_bounds_utc, local_today
from app.workout_categories import LOW_INTENSITY_TYPES

# Corridor bounds. Fixed in ratio space — that is the point of plotting the ratio
# rather than the load: in load units the band moves with CTL, so you are judging
# distance from a target that is itself in motion.
# Since 2026-11-04 the corridor is the app-wide normal range (app/acwr.py), not 0.8–1.3.
CORRIDOR_LO = ACWR_LOW
CORRIDOR_HI = ACWR_HIGH

# One ATL time constant (e^(-1/7) per day). The two scenarios diverge
# exponentially, so by 14 days (2τ) the envelope mostly restated "if you stop,
# fatigue decays" and the cone took a third of the chart (note 49, 2026-09-23).
# Day 1 of each scenario is surfaced separately as the one-session answer.
DEFAULT_HORIZON = 7

# Window used to characterise "a typical workout" — long enough to average over a
# training block, short enough to track a change in what your sessions look like.
TYPICAL_WINDOW_DAYS = 60


@dataclass
class Point:
    t: int
    atl: float
    ctl: float
    acwr: float


def project(atl0: float, ctl0: float, loads: list[float]) -> list[Point]:
    """Roll the EWMA pair forward over a sequence of hypothetical daily loads."""
    atl, ctl, out = atl0, ctl0, []
    for i, load in enumerate(loads, start=1):
        atl = ALPHA_ATL * load + (1 - ALPHA_ATL) * atl
        ctl = ALPHA_CTL * load + (1 - ALPHA_CTL) * ctl
        out.append(Point(t=i, atl=round(atl, 2), ctl=round(ctl, 2),
                         acwr=round(atl / ctl, 4) if ctl > 0 else None))
    return out


def _zone(acwr: float | None) -> str | None:
    if acwr is None:
        return None
    if acwr > CORRIDOR_HI:
        return "above"
    if acwr < CORRIDOR_LO:
        return "below"
    return "inside"


def _corridor_event(start_acwr: float | None, points: list[Point]) -> dict | None:
    """The next corridor transition, phrased relative to where you actually are.

    Reporting "leaves the corridor on day 1" is wrong — and actively confusing —
    when today is ALREADY outside it, which is the common case for the scenario a
    person is worried about. So the event depends on the starting zone: from inside
    the interesting moment is the exit, from outside it is the return.
    """
    start = _zone(start_acwr)
    if start is None:
        return None

    if start == "inside":
        for p in points:
            z = _zone(p.acwr)
            if z and z != "inside":
                return {"kind": "exits", "direction": z, "day": p.t}
        return {"kind": "stays_in"}

    for p in points:
        if _zone(p.acwr) == "inside":
            return {"kind": "returns", "from": start, "day": p.t}
    # Never re-enters within the horizon — say which side it is stuck on, and
    # whether it is at least heading back.
    last = points[-1].acwr if points else None
    improving = (last is not None and start_acwr is not None and
                 (last < start_acwr if start == "above" else last > start_acwr))
    return {"kind": "stays_out", "direction": start, "improving": improving}


async def acwr_forecast(db: AsyncSession, horizon: int = DEFAULT_HORIZON) -> dict:
    """Current load state plus `horizon` days of projection under three choices.

    Scenarios are deliberately the three a person actually picks between, not a
    parameter sweep: stop, keep going, or push. `maintain` uses the trailing 7-day
    MEAN daily load (rest days included) — the honest "carry on as I have been",
    since using only training days would project a phantom block with no recovery.
    """
    rows = (await db.execute(
        select(DailyMetric)
        .where(DailyMetric.date <= local_today())
        .order_by(DailyMetric.date.desc())
        .limit(60)
    )).scalars().all()
    if not rows:
        return {"available": False, "reason": "no training-load history"}

    rows = list(reversed(rows))          # oldest → newest
    cur = rows[-1]
    if cur.atl is None or cur.ctl is None or not cur.ctl:
        return {"available": False, "reason": "load state not computed yet"}

    # Warmup gate, mirroring metrics_engine: a 1.0 sentinel is not a measurement.
    first_load = (await db.execute(
        select(DailyMetric.date).where(DailyMetric.daily_load > 0)
        .order_by(DailyMetric.date).limit(1)
    )).scalar_one_or_none()
    if first_load is None or (cur.date - first_load).days < WARMUP_DAYS:
        return {"available": False, "reason": "not enough load history for a ratio"}

    # Two scenarios: train on none of the next N days, or do one typical session on
    # every one of them. Only "rest" is a true bound (load can't go below zero); "train"
    # is a reference line, NOT a ceiling — a week of above-typical sessions, or two in a
    # day, lands above it (note 75, 2026-10-25: the old copy called the pair a bound).
    #
    # `typical` is the mean TRIMP per TRAINING SESSION. It was the mean daily_load over
    # days with any load, which (a) averaged walk-only days (~10 TRIMP) in with gym
    # sessions (~75-82) — mean 55 vs median 67 over the 60 d before the change — and
    # (b) was labelled "sessions" while counting days. Walks and other
    # LOW_INTENSITY_TYPES are excluded, as are watch-left-running `suspect` sessions,
    # which never count toward daily_load either.
    window_start = local_day_bounds_utc(cur.date - timedelta(days=TYPICAL_WINDOW_DAYS - 1))[0]
    window_end = local_day_bounds_utc(cur.date)[1]
    trained = [w.trimp for w in (await db.execute(
        select(Workout)
        .where(Workout.start_time >= window_start)
        .where(Workout.start_time < window_end)
    )).scalars().all()
        if not w.suspect and w.trimp and w.trimp > 0
        and (w.activity_type or "") not in LOW_INTENSITY_TYPES]
    typical = sum(trained) / len(trained) if trained else 0.0

    scenarios = {
        "rest":  [0.0] * horizon,
        "train": [typical] * horizon,
    }

    out = {}
    for name, loads in scenarios.items():
        pts = project(cur.atl, cur.ctl, loads)
        out[name] = {
            "points": [{"day": p.t, "acwr": p.acwr, "atl": p.atl, "ctl": p.ctl} for p in pts],
            "end_acwr": pts[-1].acwr,
            "end_atl": pts[-1].atl,
            "end_ctl": pts[-1].ctl,
            "corridor_event": _corridor_event(cur.acwr, pts),
        }

    return {
        "available": True,
        "horizon": horizon,
        "corridor": {"low": CORRIDOR_LO, "high": CORRIDOR_HI},
        "current": {
            "date": cur.date.isoformat(),
            "atl": round(cur.atl, 2),
            "ctl": round(cur.ctl, 2),
            "acwr": round(cur.acwr, 3) if cur.acwr is not None else None,
        },
        "typical_workout_load": round(typical, 2),
        "typical_n_sessions": len(trained),
        "scenarios": out,
    }
