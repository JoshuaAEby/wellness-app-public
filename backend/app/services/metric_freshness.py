"""
Is each input still arriving?

Every downstream number here rests on ingestion that can stop silently. Three
real cases motivated this, and none of them surfaced anywhere in the app:

  • `partner_present` produced ZERO rows for six weeks. The HA entity it reads
    went `unknown` in March; `_home_fraction` returns None and logs nothing, so
    23 correlation pairs sat at n=0 indefinitely.
  • Mac screen time was silently stale for months (the knowledgeC WAL bug).
  • VO2max quietly stopped accruing — found only by hand-querying.

The existing `_screentime_staleness_check` is the right idea with a hardcoded
36-hour threshold for one stream. This generalises it.

## Why the threshold comes from the metric's own history

A fixed threshold cannot work across this frame. `steps` arrives many times a
day; an event metric like `alcohol` has a median gap measured in days and a long tail. One
number either screams about the second or never fires for the first. So each
metric's alert threshold is derived from its OWN observed inter-observation
gaps — a metric is late when it is late *for itself*.

## Alert, never exclude

This module reports; it does not gate any analysis. That distinction is load-
bearing. The obvious "fix" for a stalled event metric — stop zero-filling it —
was proposed and REJECTED: an event metric had a genuine
40-day run of true zeros, and auto-excluding would have silently discarded 40
days of correct data. A human is the only thing that can tell "did not happen"
from "stopped recording", so the system's job is to ask, not to decide.

Acknowledging therefore SNOOZES rather than mutes: it clears the alert for one
more expected-gap's worth of time, then asks again if the metric is still
silent. A permanent mute would turn the one case a person confirmed as normal
into a permanent blind spot for the case where the pipeline really does break.
"""
from __future__ import annotations

import bisect
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.timeutils import local_date_sql, local_today

logger = logging.getLogger(__name__)

# Observations needed before a metric's own cadence can be estimated. Below this
# the metric reports as `new` and never alerts — a series with three points has
# no gap distribution worth trusting.
MIN_OBS_FOR_CADENCE = 8

# Multiplier on the p95 observed gap. p95 alone would cry wolf on ~5% of normal
# gaps; the headroom buys quiet at the cost of a little detection latency, which
# is the right trade for a notification that reaches a phone.
STALE_FACTOR = 1.5

# No metric alerts before this many days of silence regardless of how tight its
# cadence is, so a single missed overnight sync is never a notification.
MIN_STALE_DAYS = 3

# Cap: past this, a metric is stale no matter how ragged its history. Without it
# a very irregular series can compute a threshold of months and never alert.
MAX_STALE_DAYS = 45


@dataclass
class Freshness:
    metric: str
    label: str
    source: str                      # which table/stream it comes from
    last_seen: Optional[date]
    days_since: Optional[int]
    n_obs: int
    typical_gap_days: Optional[float]   # median — what "normal" looks like
    threshold_days: Optional[int]       # silence beyond this is a stall
    state: str                          # live | stale | acknowledged | never | new
    acked_until: Optional[date] = None
    note: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "metric": self.metric, "label": self.label, "source": self.source,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "days_since": self.days_since, "n_obs": self.n_obs,
            "typical_gap_days": self.typical_gap_days,
            "threshold_days": self.threshold_days, "state": self.state,
            "acked_until": self.acked_until.isoformat() if self.acked_until else None,
            "note": self.note,
        }


def _p(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated quantile of an already-sorted list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_vals[int(pos)]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _away_between(away: list[date], a: date, b: date) -> int:
    """Count of away days d with a < d <= b (`away` sorted)."""
    return bisect.bisect_right(away, b) - bisect.bisect_right(away, a)


def _cadence(days: list[date], away: Optional[list[date]] = None) -> tuple[Optional[float], Optional[int]]:
    """(median gap, alert threshold) in days, from a metric's observed days.

    Gaps are between DISTINCT observation days, so a metric sampled 300 times an
    hour and one sampled once a day both come out with a gap of 1 — the question
    is "is it still arriving", not "how dense is it".

    Away days (travel flags) are taken out of every gap: weight or the bedroom sensors
    going quiet for a fortnight abroad is not a cadence, and counting it would loosen
    the threshold for good after every trip (2026-10-19).
    """
    if len(days) < MIN_OBS_FOR_CADENCE:
        return None, None
    away = away or []
    gaps = sorted(max(1.0, float((b - a).days - _away_between(away, a, b)))
                  for a, b in zip(days, days[1:]))
    if not gaps:
        return None, None
    median = _p(gaps, 0.5)
    threshold = int(min(max(math.ceil(_p(gaps, 0.95) * STALE_FACTOR),
                            MIN_STALE_DAYS), MAX_STALE_DAYS))
    return round(median, 1), threshold


# Streams that do not live in health_metrics. Each entry maps a display label to
# a (table, timestamp column, is_local_time) triple resolved below.
_EXTRA_SOURCES = [
    ("sleep_session",  "Sleep sessions",   "sleep_sessions"),
    ("workout",        "Workouts",         "workouts"),
    ("food_log",       "Food log",         "food_log"),
    ("subjective",     "Check-ins",        "subjective_log"),
    ("habit_log",      "Habit log",        "habit_logs"),
    ("caffeine_log",   "Caffeine log",     "caffeine_log"),
    ("hevy_workout",   "Hevy strength",    "hevy_workouts"),
    ("screentime_iphone", "Screen time (iPhone)", "screentime_events"),
    ("weather_daily",  "Weather",          "weather_daily"),
]


async def _health_metric_days(db: AsyncSession) -> dict[str, list[date]]:
    """Distinct local observation days per health_metrics type.

    EVERY row counts, including value == 0. Filtering to value > 0 was tried
    first and is wrong: for state metrics a zero IS the measurement, and the
    filter made healthy daily feeds look sparse and dying. `slept_away` writes
    0.0 on the ~85% of nights spent at home, so under the filter it showed 6
    observations and 23 days of silence — an invented stall for a feed that had
    run correctly every night.

    The question here is "did the ingestion path produce output", and a row is
    that evidence whatever its value: health_metrics rows are written only when
    something actually ingested. Event metrics are unaffected — nothing writes
    a zero row for a workout that did not happen.
    """
    day = local_date_sql(HealthMetric.timestamp)
    rows = (await db.execute(
        select(HealthMetric.metric_type, day.label("d"))
        .group_by(HealthMetric.metric_type, day)
        .order_by(HealthMetric.metric_type, day)
    )).all()
    out: dict[str, list[date]] = {}
    for r in rows:
        out.setdefault(r.metric_type, []).append(
            r.d if isinstance(r.d, date) else date.fromisoformat(str(r.d)))
    return out


async def _extra_days(db: AsyncSession) -> dict[str, list[date]]:
    """Distinct observation days for the non-health_metrics streams."""
    from sqlalchemy import text

    specs = {
        "sleep_session":      "SELECT DISTINCT date AS d FROM sleep_sessions WHERE total_min IS NOT NULL",
        "workout":            "SELECT DISTINCT date(start_time,'localtime') AS d FROM workouts",
        # food_log.timestamp is naive LOCAL already — no 'localtime' modifier.
        "food_log":           "SELECT DISTINCT date(timestamp) AS d FROM food_log WHERE COALESCE(skipped,0)=0",
        "subjective":         "SELECT DISTINCT date AS d FROM subjective_log",
        "habit_log":          "SELECT DISTINCT date AS d FROM habit_logs",
        "caffeine_log":       "SELECT DISTINCT date(timestamp,'localtime') AS d FROM caffeine_log",
        "hevy_workout":       "SELECT DISTINCT date(start_time,'localtime') AS d FROM hevy_workouts",
        "screentime_iphone":  "SELECT DISTINCT date(start_time,'localtime') AS d FROM screentime_events WHERE device='iPhone'",
        "weather_daily":      "SELECT DISTINCT date AS d FROM weather_daily",
    }
    out: dict[str, list[date]] = {}
    for key, sql in specs.items():
        try:
            rows = (await db.execute(text(sql + " ORDER BY d"))).all()
        except Exception as exc:                      # table absent on an old DB
            logger.warning("Freshness: %s unavailable — %s", key, exc)
            continue
        days = []
        for r in rows:
            v = r[0]
            if v is None:
                continue
            days.append(v if isinstance(v, date) else date.fromisoformat(str(v)))
        out[key] = days
    return out


# Inputs that are DECLARED — the correlation engine reads them — but whose
# absence would otherwise be invisible, because a metric with zero rows has no
# row to be stale. `never` is a distinct and more alarming state than `stale`.
DECLARED_INPUTS = {
    "partner_present":  "Partner present (HA)",
    "bedroom_temp":     "Bedroom temp (HA)",
    "bedroom_humidity": "Bedroom humidity (HA)",
    "tv_prebed_min":    "TV before bed (HA)",
    "slept_away":       "Away night (HA)",
    "wfh_day":          "WFH day (HA)",
    "cardio_recovery":  "Apple HRR (cross-check)",
    "mindful_minutes":  "Mindful minutes",
    "nap":              "Naps",
    "water":            "Water",
}

_LABEL_OVERRIDES = {**DECLARED_INPUTS}


def _label(metric: str) -> str:
    if metric in _LABEL_OVERRIDES:
        return _LABEL_OVERRIDES[metric]
    return metric.replace("_", " ").capitalize()


async def compute_freshness(db: AsyncSession) -> list[Freshness]:
    """One Freshness row per watched stream, worst first."""
    from app.models import MetricAck

    from app.services.day_flags import away_dates

    today = local_today()
    # Days away don't count toward "quiet for N days" — the 2026-10 trip fired this
    # alarm every morning for streams that only exist at home (scale, bedroom sensors).
    away = sorted(await away_dates(db, today - timedelta(days=800), today))
    hm_days = await _health_metric_days(db)
    extra_days = await _extra_days(db)

    acks = {a.metric: a for a in
            (await db.execute(select(MetricAck))).scalars().all()}

    out: list[Freshness] = []

    def _add(metric: str, label: str, source: str, days: list[date]) -> None:
        ack = acks.get(metric)
        acked_until = ack.acked_until if ack else None
        note = ack.note if ack else None

        if not days:
            out.append(Freshness(metric, label, source, None, None, 0, None, None,
                                 "never", acked_until, note))
            return

        last = days[-1]
        since = (today - last).days - _away_between(away, last, today)
        median, threshold = _cadence(days, away)

        if threshold is None:
            state = "new"
        elif since > threshold:
            state = "acknowledged" if (acked_until and today <= acked_until) else "stale"
        else:
            state = "live"

        out.append(Freshness(metric, label, source, last, since, len(days),
                             median, threshold, state, acked_until, note))

    for metric, days in sorted(hm_days.items()):
        _add(metric, _label(metric), "health_metrics", days)
    # Declared-but-absent inputs have no health_metrics rows at all, so the loop
    # above never reaches them. This is the partner_present case.
    for metric, label in DECLARED_INPUTS.items():
        if metric not in hm_days:
            _add(metric, label, "health_metrics", [])
    for key, label, table in _EXTRA_SOURCES:
        _add(key, label, table, extra_days.get(key, []))

    rank = {"never": 0, "stale": 1, "acknowledged": 2, "new": 3, "live": 4}
    out.sort(key=lambda f: (rank[f.state], -(f.days_since or 0)))
    return out


async def acknowledge(db: AsyncSession, metric: str, note: str | None = None) -> dict:
    """Snooze `metric` for one more expected-gap's worth of silence.

    The horizon is the metric's own threshold, not a fixed week: acknowledging a
    daily feed should ask again in days, acknowledging a sporadic one should not
    ask again for a while. Re-acknowledging extends from today, so a genuinely
    dormant metric costs one tap per threshold-length rather than nagging.
    """
    from app.models import MetricAck

    rows = await compute_freshness(db)
    match = next((f for f in rows if f.metric == metric), None)
    if match is None:
        raise ValueError(f"unknown metric {metric!r}")

    horizon = match.threshold_days or MIN_STALE_DAYS
    until = local_today() + timedelta(days=horizon)

    existing = (await db.execute(
        select(MetricAck).where(MetricAck.metric == metric)
    )).scalar_one_or_none()
    if existing is None:
        existing = MetricAck(metric=metric)
        db.add(existing)
    existing.acked_at = datetime.utcnow()
    existing.acked_until = until
    existing.note = note
    await db.commit()

    logger.info("Freshness: %s acknowledged until %s (%s)", metric, until, note or "-")
    return {"metric": metric, "acked_until": until.isoformat(),
            "horizon_days": horizon, "note": note}


async def clear_ack(db: AsyncSession, metric: str) -> dict:
    """Drop an acknowledgement so the metric can alert again immediately."""
    from sqlalchemy import delete

    from app.models import MetricAck

    await db.execute(delete(MetricAck).where(MetricAck.metric == metric))
    await db.commit()
    return {"metric": metric, "cleared": True}


# Imported late so the module can be read without pulling the ORM in first.
from app.models import HealthMetric  # noqa: E402
