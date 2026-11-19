"""Progress since training began — ABSOLUTE change over months (note 56, 2026-09-25).

Why this exists: every daily number in the app is built to be stationary. HRV and RHR
are shown as z-scores against a ROLLING baseline, and readiness is built on those, so a
real improvement is absorbed into the baseline and the daily figures sit near zero
forever. Measured at the time: over six months the HRV baseline rose ~10% and the RHR baseline
fell ~5%, while the monthly median z-scores stayed within ±0.2. The
athlete's reading — "most metrics are stagnant" — was the app's design, not his body.

Method, per metric:
  * start       = the day after the last day CTL was below START_CTL (training began in
                  earnest), unless `since` is given.
  * before      = median of the BASELINE_DAYS before start (the untrained state).
  * now         = median of the last RECENT_DAYS.
  * change      = now − before, with a 95% CI from a MOVING-BLOCK bootstrap (7-day blocks —
                  consecutive days are autocorrelated, so resampling single days would make
                  the interval falsely narrow). THIS is the verdict: "am I better than before
                  I started?". Adaptation is not linear (HRV rose Apr→Jul, then plateaued), so
                  a straight-line trend understates it — the first build used the trend and
                  called a clear HRV rise "unclear".
  * trend       = Theil–Sen slope over [start − BASELINE_DAYS, today], secondary context.
  * verdict     = improved / worsened only when the change CI excludes zero; else "unclear".
Whole-day exclusions (sick, travel …) are dropped — they swamp HR and HRV.

The four weeks before training are not a fair reference on their own (2026-11-02): they fell at
the end of a winter trough (HRV well below the previous summer's), so a rebound read as a gain. Where
last year exists, the same dates a year earlier decide the verdict; see `combined_verdict`.
"""
from __future__ import annotations

import random
import statistics as st
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Optional

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.acwr import acwr_band
from app.models import DailyMetric, HealthMetric, Workout
from app.timeutils import local_date_sql, local_day_bounds_utc, local_today, to_local_date

START_CTL = 5.0
BASELINE_DAYS = 28
RECENT_DAYS = 28
MAX_TREND_POINTS = 60      # Theil–Sen is O(n²); thin evenly beyond this
BOOTSTRAPS = 200
HRR_MIN_PEAK = 150         # heart-rate recovery only after genuinely hard efforts


def _theil_sen(pts: list[tuple[int, float]]) -> Optional[float]:
    s = [(y2 - y1) / (x2 - x1) for i, (x1, y1) in enumerate(pts)
         for (x2, y2) in pts[i + 1:] if x2 != x1]
    return st.median(s) if s else None


BLOCK_DAYS = 7


def _block_median_boot(vals: list[float], rng: random.Random) -> float:
    """Median of a moving-block bootstrap resample of a (time-ordered) window."""
    n = len(vals)
    if n <= BLOCK_DAYS:
        return st.median(rng.choice(vals) for _ in vals)
    out: list[float] = []
    while len(out) < n:
        i = rng.randrange(0, n - BLOCK_DAYS + 1)
        out.extend(vals[i:i + BLOCK_DAYS])
    return st.median(out[:n])


def _trend(series: dict[date, float], start: date, end: date, better: str,
           min_points: int = 10) -> dict:
    """Before/now/trend for one daily series. `better` is "up" or "down"."""
    first = start - timedelta(days=BASELINE_DAYS)
    pts_all = sorted((d, v) for d, v in series.items() if first <= d <= end and v is not None)
    before = [v for d, v in pts_all if d < start]
    baseline_label = "before training"
    # Workout-derived and sparse series (HRR60, VO2max) barely exist before training —
    # there were no hard efforts to recover from. Fall back to the FIRST month of
    # training, labelled as such, rather than calling a real change "insufficient".
    if len(before) < 4:
        before = [v for d, v in pts_all if start <= d < start + timedelta(days=RECENT_DAYS)]
        baseline_label = "first month of training"
    recent = [v for d, v in pts_all if d > end - timedelta(days=RECENT_DAYS)]
    by_month = defaultdict(list)
    by_week = defaultdict(list)
    for d, v in pts_all:
        by_month[d.strftime("%Y-%m")].append(v)
        by_week[(d - timedelta(days=d.weekday())).isoformat()].append(v)   # ISO Monday
    out = {
        "n": len(pts_all),
        "before": round(st.median(before), 1) if before else None,
        "now": round(st.median(recent), 1) if recent else None,
        "monthly": [{"month": m, "median": round(st.median(v), 1)} for m, v in sorted(by_month.items())],
        # Weekly medians for the full-size scorecard plot (note 68).
        "weekly": [{"week_of": w, "value": round(st.median(v), 1)} for w, v in sorted(by_week.items())],
        "better": better,
        "baseline_label": baseline_label,
    }
    if out["before"] is not None and out["now"] is not None:
        out["change"] = round(out["now"] - out["before"], 1)
    if len(pts_all) < min_points or len(before) < 4 or len(recent) < 4:
        out["verdict"] = "insufficient"
        return out
    rng_c = random.Random(len(before) * 31 + len(recent) * 17)
    diffs = sorted(_block_median_boot(recent, rng_c) - _block_median_boot(before, rng_c)
                   for _ in range(BOOTSTRAPS))
    c_lo, c_hi = diffs[int(0.025 * BOOTSTRAPS)], diffs[int(0.975 * BOOTSTRAPS) - 1]
    out.update(change_ci_low=round(c_lo, 1), change_ci_high=round(c_hi, 1))
    x = [((d - first).days, v) for d, v in pts_all]
    step = max(1, len(x) // MAX_TREND_POINTS)
    thin = x[::step]
    span = (end - first).days
    slope = _theil_sen(thin)
    # Seeded so the card shows the same interval on every load of the same data.
    rng = random.Random(len(thin) * 7919 + span)
    boots = sorted(
        (_theil_sen(sorted(rng.choice(thin) for _ in thin)) or 0.0) for _ in range(BOOTSTRAPS))
    lo = boots[int(0.025 * BOOTSTRAPS)] * span
    hi = boots[int(0.975 * BOOTSTRAPS) - 1] * span
    out.update(trend=round(slope * span, 1), ci_low=round(lo, 1), ci_high=round(hi, 1))
    if c_lo > 0 or c_hi < 0:
        up = c_lo > 0
        if better is None:            # e.g. weight: a direction, not a judgement
            out["verdict"] = "up" if up else "down"
        else:
            out["verdict"] = "improved" if up == (better == "up") else "worsened"
    else:
        out["verdict"] = "unclear"
    return out


# Same dates a year earlier, in whole weeks so weekdays line up. Walking HR has a seasonal
# swing of several bpm, which a before/after against the month training began reads as change:
# it fell from April to August in BOTH years. The same months in each year cancel that out.
YOY_SHIFT = timedelta(days=364)
YOY_MONTHS = 12
YOY_MIN_MONTH_DAYS = 10
YOY_MIN_POINTS = 8
YOY_CHECK_DAYS = 90        # the scorecard's check: long enough that one trip can't decide it
PAST_BEST_MIN_DAYS = 3     # a month needs this many days to count towards a past best


def past_best(series: dict[date, float], before: date, better: str) -> Optional[float]:
    """Best calendar-month median before `before`. Apple's VO₂ estimate wanders on its own
    (33–38 with no training), so beating one year's dates isn't enough: it has to leave the
    range it already covered."""
    months = defaultdict(list)
    for d, v in series.items():
        if d < before:
            months[(d.year, d.month)].append(v)
    meds = [st.median(v) for v in months.values() if len(v) >= PAST_BEST_MIN_DAYS]
    if not meds:
        return None
    return round(max(meds) if better == "up" else min(meds), 1)


def year_over_year(series: dict[date, float], start: date, end: date, better: str,
                   prior_best: Optional[float] = None) -> dict:
    """Median over [start, end] against the same window a year earlier, plus calendar months
    for the chart. Keep the window fixed (training start to now), not the best months.
    `prior_best` (see `past_best`): an improvement that doesn't pass it is "within_range"."""
    now = [v for d, v in sorted(series.items()) if start <= d <= end]
    last = [v for d, v in sorted(series.items()) if start - YOY_SHIFT <= d <= end - YOY_SHIFT]
    out: dict = {"n": len(now), "n_last": len(last), "better": better,
                 "now": round(st.median(now), 1) if now else None,
                 "last_year": round(st.median(last), 1) if last else None,
                 "months": []}
    y, m = end.year, end.month
    for _ in range(YOY_MONTHS):
        vals = [v for d, v in series.items() if d.year == y and d.month == m]
        prev = [v for d, v in series.items() if d.year == y - 1 and d.month == m]
        out["months"].insert(0, {
            "month": date(y, m, 1).strftime("%b"),
            "this": round(st.median(vals), 1) if len(vals) >= YOY_MIN_MONTH_DAYS else None,
            "last": round(st.median(prev), 1) if len(prev) >= YOY_MIN_MONTH_DAYS else None,
        })
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    if len(now) < YOY_MIN_POINTS or len(last) < YOY_MIN_POINTS:
        out["verdict"] = "insufficient"
        return out
    out["change"] = round(out["now"] - out["last_year"], 1)
    rng = random.Random(len(now) * 31 + len(last) * 17)
    diffs = sorted(_block_median_boot(now, rng) - _block_median_boot(last, rng)
                   for _ in range(BOOTSTRAPS))
    lo, hi = diffs[int(0.025 * BOOTSTRAPS)], diffs[int(0.975 * BOOTSTRAPS) - 1]
    out.update(ci_low=round(lo, 1), ci_high=round(hi, 1))
    if lo > 0 or hi < 0:
        out["verdict"] = "improved" if (lo > 0) == (better == "up") else "worsened"
    else:
        out["verdict"] = "unclear"
    if prior_best is not None:
        out["past_best"] = prior_best
        beyond = out["now"] > prior_best if better == "up" else out["now"] < prior_best
        if out["verdict"] == "improved" and not beyond:
            out["verdict"] = "within_range"
    return out


def combined_verdict(before_verdict: str, year_verdict: Optional[str]) -> tuple[str, str]:
    """(verdict, basis) for a scorecard row. Same dates last year cancel season and don't hang
    on one four-week reference, so they decide. Better than before training but not than last
    year is a "rebound": real, but back to normal rather than new."""
    if year_verdict in ("improved", "worsened"):
        return year_verdict, "last_year"
    if year_verdict == "unclear":
        return ("rebound" if before_verdict == "improved" else "unclear"), "last_year"
    return before_verdict, "before"


def with_last_year(metric: dict, series: dict[date, float], end: date) -> dict:
    year = year_over_year(series, end - timedelta(days=YOY_CHECK_DAYS - 1), end, metric["better"])
    year.pop("months")
    verdict, basis = combined_verdict(metric.get("verdict"), year["verdict"])
    return {**metric, "before_verdict": metric.get("verdict"), "verdict": verdict,
            "verdict_basis": basis, "year": year}


async def _excluded(db: AsyncSession, start: date, end: date) -> set:
    from app.services.day_flags import excluded_dates
    return await excluded_dates(db, start, end)


async def _hm_daily(db: AsyncSession, metric: str, start: date, end: date,
                    agg: str = "avg") -> dict[date, float]:
    """Per-local-day value of a health metric: mean of samples, or `agg="sum"` for counts
    (steps, flights) that arrive as many partial samples per day."""
    lday = local_date_sql(HealthMetric.timestamp)
    fn = func.sum if agg == "sum" else func.avg
    rows = (await db.execute(
        select(lday.label("d"), fn(HealthMetric.value).label("v"))
        .where(HealthMetric.metric_type == metric)
        .where(HealthMetric.timestamp >= local_day_bounds_utc(start)[0])
        .where(HealthMetric.timestamp < local_day_bounds_utc(end)[1])
        .group_by(lday)
    )).all()
    return {date.fromisoformat(str(r.d)): float(r.v) for r in rows if r.v is not None}


async def training_start(db: AsyncSession) -> Optional[date]:
    """Day after the last day CTL was below START_CTL — when training began in earnest."""
    last_low = (await db.execute(
        select(func.max(DailyMetric.date)).where(DailyMetric.ctl < START_CTL)
        .where(DailyMetric.date < local_today())
    )).scalar()
    if last_low is None:
        return (await db.execute(select(func.min(DailyMetric.date)))).scalar()
    return last_low + timedelta(days=1)


# Strength verdicts (2026-11-02). A trend of each lift's session-best e1RM read 15 of 17 lifts
# "unclear" with intervals of hundreds of percent: class sets change rep scheme every time, a
# lift returns every three weeks, and few sets are near failure, so no logged number is a max.
# Two measures that survive that, both over HISTORY_START onward (dumbbells were per hand before):
#   * pooled rate: log(top-set load) ~ lift + reps + months, days resampled for the interval.
#   * per lift: heaviest load for the SAME reps early vs recently, only with ≥ 2 sessions in each.
LB_PER_KG = 2.20462
LOAD_STEP_LB = 2.5
TOP_SET_MAX_REPS = 20
RATE_MIN_LIFT_SESSIONS = 3
RATE_MIN_ROWS = 12
RATE_BOOTSTRAPS = 400
RATE_SEED = 20261102
RECENT_RATE_DAYS = 150
EARLY_WINDOW_DAYS = 60
RECENT_WINDOW_DAYS = 90
MATCH_REPS = 10
ASSIST_MATCH_REPS = 5      # band work is programmed at 5 reps
MIN_WINDOW_SESSIONS = 2
# Fixed by meaning from Hevy's labels, never from which lifts moved. Full-body and core lifts
# stay in the overall rate only.
MUSCLE_GROUPS = (
    ("arms_shoulders", "Arms and shoulders", {"biceps", "triceps", "forearms", "shoulders"}),
    ("chest_back", "Chest and back", {"chest", "lats", "upper_back", "traps"}),
    ("legs_hips", "Legs and hips", {"quadriceps", "hamstrings", "glutes", "calves", "lower_back",
                                    "abductors", "adductors"}),
)


def _rate_fit(lift: np.ndarray, X: np.ndarray, y: np.ndarray) -> float:
    """Slope on the last column of X, with a separate intercept per lift."""
    n = lift.max() + 1
    counts = np.bincount(lift, minlength=n)
    demean = lambda v: v - (np.bincount(lift, weights=v, minlength=n) / np.maximum(counts, 1))[lift]
    Xd = np.column_stack([demean(X[:, j]) for j in range(X.shape[1])])
    return float(np.linalg.lstsq(Xd, demean(y), rcond=None)[0][-1])


def rep_adjusted_rate(rows: list[tuple]) -> dict:
    """Pure. rows: (lift, day, lb, reps), one per lift per session. %/month of load at equal reps
    (100 × the slope of log load; a log point is about 1% at these sizes)."""
    days = sorted({r[1] for r in rows})
    lifts = sorted({r[0] for r in rows})
    out: dict = {"n_rows": len(rows), "n_lifts": len(lifts), "n_days": len(days), "rate": None}
    if len(rows) < RATE_MIN_ROWS or len(days) < 4 or len(rows) <= len(lifts) + 2:
        return {**out, "verdict": "insufficient"}
    li = {x: i for i, x in enumerate(lifts)}
    lift = np.array([li[r[0]] for r in rows])
    day = np.array([days.index(r[1]) for r in rows])
    months = np.array([(r[1] - days[0]).days / 30.44 for r in rows])
    X = np.column_stack([np.array([r[3] for r in rows], float), months])
    y = np.log(np.array([r[2] for r in rows], float))
    rate = _rate_fit(lift, X, y)
    by_day = [np.flatnonzero(day == i) for i in range(len(days))]
    rng = np.random.default_rng(RATE_SEED)
    boots = []
    for _ in range(RATE_BOOTSTRAPS):
        idx = np.concatenate([by_day[i] for i in rng.integers(0, len(days), len(days))])
        boots.append(_rate_fit(lift[idx], X[idx], y[idx]))
    lo, hi = np.percentile(boots, [2.5, 97.5])
    verdict = "improved" if lo > 0 else "worsened" if hi < 0 else "unclear"
    return {**out, "rate": round(100 * rate, 1), "ci_low": round(100 * float(lo), 1),
            "ci_high": round(100 * float(hi), 1), "verdict": verdict}


def top_sets(sets: list[tuple]) -> list[tuple]:
    """Pure. sets: (lift, day, lb, reps). The set with the best Epley per lift per session,
    limited to lifts seen on at least RATE_MIN_LIFT_SESSIONS days."""
    best: dict[tuple, tuple] = {}
    for lift, day, lb, reps in sets:
        if not lb or not reps or reps > TOP_SET_MAX_REPS:
            continue
        cur = best.get((lift, day))
        if cur is None or lb * (1 + reps / 30) > cur[2] * (1 + cur[3] / 30):
            best[(lift, day)] = (lift, day, lb, reps)
    n = Counter(lift for lift, _ in best)
    return [r for k, r in sorted(best.items(), key=lambda kv: (kv[0][1], kv[0][0]))
            if n[k[0]] >= RATE_MIN_LIFT_SESSIONS]


def rep_matched_lifts(sets: dict[tuple[str, bool], list[tuple]], early: tuple[date, date],
                      recent: tuple[date, date]) -> list[dict]:
    """Pure. sets: {(title, assisted): [(day, lb, reps), ...]}. Heaviest load for at least
    MATCH_REPS in each window; for assisted lifts the LEAST help for ASSIST_MATCH_REPS (a best of
    two windows, so a lift with more sessions gets more chances; both need the same floor)."""
    out = []
    for (title, assisted), rows in sets.items():
        reps_needed = ASSIST_MATCH_REPS if assisted else MATCH_REPS
        pick = min if assisted else max

        def best(window):
            inside = [r for r in rows if window[0] <= r[0] <= window[1]]
            loads = [lb for _, lb, reps in inside if lb and (reps or 0) >= reps_needed]
            return (pick(loads) if loads else None), len({r[0] for r in inside})

        (e_lb, e_n), (r_lb, r_n) = best(early), best(recent)
        if e_lb is None or r_lb is None or min(e_n, r_n) < MIN_WINDOW_SESSIONS:
            continue
        gain = e_lb / r_lb if assisted else r_lb / e_lb
        out.append({"exercise": title, "assisted": assisted, "reps": reps_needed,
                    "early_lb": e_lb, "recent_lb": r_lb,
                    "early_sessions": e_n, "recent_sessions": r_n,
                    "change_pct": round(100 * (gain - 1)),
                    "verdict": "up" if gain > 1 else "down" if gain < 1 else "same"})
    out.sort(key=lambda r: (-r["change_pct"], r["exercise"]))
    return out


async def strength_progress(db: AsyncSession, start: date, end: date) -> dict:
    from app.models import HevyExerciseTemplate, HevySet, HevyWorkout
    meta = {t: (kind, muscle) for t, kind, muscle in (await db.execute(
        select(HevyExerciseTemplate.title, HevyExerciseTemplate.exercise_type,
               HevyExerciseTemplate.primary_muscle_group))).all()}
    rows = (await db.execute(
        select(HevyWorkout.start_time, HevySet.exercise_title, HevySet.weight_kg, HevySet.reps)
        .join(HevyWorkout, HevyWorkout.id == HevySet.hevy_workout_id)
        .where(HevyWorkout.start_time >= local_day_bounds_utc(start)[0])
        .where((HevySet.set_type.is_(None)) | (HevySet.set_type == "normal"))
    )).all()
    loaded: list[tuple] = []
    by_lift: dict[tuple[str, bool], list[tuple]] = defaultdict(list)
    for ts, title, kg, reps in rows:
        kind = meta.get(title, (None, None))[0]
        if kind not in ("weight_reps", "bodyweight_assisted") or not kg or not reps:
            continue
        day, lb = to_local_date(ts), round(kg * LB_PER_KG / LOAD_STEP_LB) * LOAD_STEP_LB
        by_lift[(title, kind == "bodyweight_assisted")].append((day, lb, reps))
        if kind == "weight_reps":
            loaded.append((title, day, lb, reps))

    top = top_sets(loaded)
    recent_from = end - timedelta(days=RECENT_RATE_DAYS - 1)
    early = (start, start + timedelta(days=EARLY_WINDOW_DAYS - 1))
    recent = (end - timedelta(days=RECENT_WINDOW_DAYS - 1), end)
    lifts = rep_matched_lifts(by_lift, early, recent)
    gains = [1 + r["change_pct"] / 100 for r in lifts]
    return {
        "from": start.isoformat(), "early_to": early[1].isoformat(), "recent_from": recent[0].isoformat(),
        "overall": rep_adjusted_rate(top),
        "recent_rate": {**rep_adjusted_rate([r for r in top if r[1] >= recent_from]),
                        "days": RECENT_RATE_DAYS},
        "groups": [{"key": key, "label": label,
                    **rep_adjusted_rate([r for r in top if meta.get(r[0], (None, None))[1] in muscles])}
                   for key, label, muscles in MUSCLE_GROUPS],
        "lifts": lifts,
        "n_all_lifts": len(by_lift),
        "n_up": sum(1 for r in lifts if r["verdict"] == "up"),
        "n_same": sum(1 for r in lifts if r["verdict"] == "same"),
        "n_down": sum(1 for r in lifts if r["verdict"] == "down"),
        "typical_pct": round(100 * (float(np.exp(np.mean(np.log(gains)))) - 1)) if gains else None,
    }


# One line each for the top of Fitness → Cardio and Strength, built from the verdicts above so
# they can't disagree with the Progress tab. `state` colours the title only when a verdict's
# interval excluded zero; everything else stays ink.
CARDIO_WORDS = {"improved": "better", "worsened": "worse", "unclear": "no clear change",
                "rebound": "rebound only", "within_range": "within its past range"}


def _pct(v: float) -> str:
    return "0" if v == 0 else f"{v:+g}"


def _short_day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%b} {d.day}"


def strength_headline(s: dict) -> Optional[dict]:
    overall, recent = s["overall"], s["recent_rate"]
    if overall["verdict"] == "insufficient":
        return None
    months = round(recent["days"] / 30)
    if recent["verdict"] == "improved":
        title, state = "Still getting stronger", "improved"
    elif recent["verdict"] == "worsened":
        title, state = "Strength is slipping", "worsened"
    elif overall["verdict"] == "improved":
        title, state = "Stronger than at the start, no clear gain lately", "neutral"
    elif overall["verdict"] == "worsened":
        title, state = "Weaker than at the start", "worsened"
    else:
        title, state = "No clear change in strength", "neutral"
    detail = f"{_pct(overall['rate'])}% a month at equal reps since {_short_day(s['from'])}"
    if recent["rate"] is not None:
        detail += f", {_pct(recent['rate'])}% over the last {months} months"
    detail += "."
    named = lambda v: [g["label"] for g in s["groups"] if g["verdict"] == v]
    for label, verdict in (("Up", "improved"), ("Down", "worsened"), ("No clear change", "unclear")):
        if named(verdict):
            detail += f" {label}: {', '.join(named(verdict)).lower()}."
    return {"title": title, "detail": detail, "state": state, "since": s["from"]}


def cardio_headline(metrics: list[dict], vs_last_year: dict) -> Optional[dict]:
    by_key = {m["key"]: m for m in metrics}
    markers = [("Walking HR", by_key["walk_hr"]["verdict"]), ("resting HR", by_key["rhr"]["verdict"]),
               ("VO₂", vs_last_year["vo2"].get("verdict")), ("HR recovery", by_key["hrr"]["verdict"])]
    read = [(name, v) for name, v in markers if v in CARDIO_WORDS]
    if not read:
        return None
    better = sum(1 for _, v in read if v == "improved")
    worse = sum(1 for _, v in read if v == "worsened")
    state = "worsened" if worse > better else "improved" if better and not worse else "neutral"
    parts = []
    for name, v in read:
        y = by_key["walk_hr"].get("year") or {}
        if name == "Walking HR" and y.get("now") is not None and y.get("last_year") is not None:
            parts.append(f"Walking HR {y['now']:g} vs {y['last_year']:g} bpm a year ago, {CARDIO_WORDS[v]}")
        else:
            parts.append(f"{name} {CARDIO_WORDS[v]}")
    detail = "; ".join(parts) + "."
    missing = [name for name, v in markers if v not in CARDIO_WORDS]
    if missing:
        detail += f" Too little recent data: {', '.join(missing)}."
    return {"title": f"{better} of {len(read)} fitness markers clearly better", "detail": detail[0].upper() + detail[1:],
            "state": state}


# ACWR bands come from app/acwr.py (one rule app-wide, 2026-11-04).
CTL_STEADY = 2.0           # |8-week CTL change| below this reads as "steady"


async def training_summary(db: AsyncSession, end: date, first: date) -> dict:
    """Scorecard rows for TRAINING (note 67): load is an INPUT, not a fitness result, so it
    gets neutral wording — rising / steady / falling, never improved / worse. CTL plateaus
    at the mean daily load (it is a ~42-day EWMA), so "steady" at a constant routine is the
    expected state, not a stall. Consistency is scored against the coach's weekly target."""
    from app.services.coach_plan import WEEKLY_CLASS_TARGET
    from app.workout_categories import categorize_activity
    dm = {r.date: r for r in (await db.execute(
        select(DailyMetric.date, DailyMetric.ctl, DailyMetric.acwr)
        .where(DailyMetric.date <= end).where(DailyMetric.date >= end - timedelta(days=400))
    )).all()}
    latest = dm.get(end) or (dm[max(dm)] if dm else None)
    ago = dm.get(end - timedelta(days=56))
    peak = max((r for r in dm.values() if r.ctl is not None), key=lambda r: r.ctl, default=None)
    ctl_now = round(latest.ctl, 1) if latest and latest.ctl is not None else None
    ctl_ago = round(ago.ctl, 1) if ago and ago.ctl is not None else None
    delta = round(ctl_now - ctl_ago, 1) if ctl_now is not None and ctl_ago is not None else None
    trend = (None if delta is None else "steady" if abs(delta) < CTL_STEADY
             else "rising" if delta > 0 else "falling")
    acwr = round(latest.acwr, 2) if latest and latest.acwr is not None else None
    band = acwr_band(acwr)

    # Monthly medians over the same window as the fitness markers — every scorecard row
    # gets a curve.
    by_m: dict[str, dict[str, list]] = defaultdict(lambda: {"ctl": [], "acwr": []})
    by_w: dict[str, dict[str, list]] = defaultdict(lambda: {"ctl": [], "acwr": []})
    for d, r in dm.items():
        if d >= first:
            wk = (d - timedelta(days=d.weekday())).isoformat()
            for k in ("ctl", "acwr"):
                v = getattr(r, k)
                if v is not None:
                    by_m[d.strftime("%Y-%m")][k].append(v)
                    by_w[wk][k].append(v)
    monthly = lambda k, nd: [{"month": m, "median": round(st.median(v[k]), nd)}
                             for m, v in sorted(by_m.items()) if v[k]]
    weekly_of = lambda k, nd: [{"week_of": w, "value": round(st.median(v[k]), nd)}
                               for w, v in sorted(by_w.items()) if v[k]]

    # Gym sessions per COMPLETE Mon–Sun week, last 12 weeks (walks don't count); the
    # consistency verdict reads the last 4.
    this_monday = end - timedelta(days=end.weekday())
    if end.weekday() == 6:                      # `end` is a Sunday: that week is complete
        this_monday += timedelta(days=7)
    # Whole window (for the full-size plot), not just 12 weeks; the verdict reads the last 4.
    WEEKS = max(4, ((this_monday - first).days + 6) // 7)
    wk_start = this_monday - timedelta(days=7 * WEEKS)
    weekly = [0] * WEEKS
    for ts, at in (await db.execute(
        select(Workout.start_time, Workout.activity_type)
        .where(Workout.start_time >= local_day_bounds_utc(wk_start)[0])
        .where(Workout.start_time < local_day_bounds_utc(this_monday)[0])
    )).all():
        if categorize_activity(at) in ("hiit", "strength"):
            i = (ts.date() - wk_start).days // 7
            if 0 <= i < WEEKS:
                weekly[i] += 1
    counts = weekly[-4:]
    return {
        "ctl_now": ctl_now, "ctl_8w_ago": ctl_ago, "ctl_change_8w": delta, "ctl_trend": trend,
        "ctl_peak": round(peak.ctl, 1) if peak else None,
        "ctl_peak_date": peak.date.isoformat() if peak else None,
        "acwr_now": acwr, "acwr_band": band,
        "ctl_monthly": monthly("ctl", 1), "acwr_monthly": monthly("acwr", 2),
        "ctl_weekly": weekly_of("ctl", 1), "acwr_weekly": weekly_of("acwr", 2),
        "sessions_weekly": [{"week_of": (wk_start + timedelta(days=7 * i)).isoformat(), "n": n}
                            for i, n in enumerate(weekly)],
        "sessions_weeks": counts, "sessions_per_week": round(sum(counts) / 4, 1),
        "weeks_on_target": sum(1 for c in counts if c >= WEEKLY_CLASS_TARGET),
        "weekly_target": WEEKLY_CLASS_TARGET,
    }


async def build_progress(db: AsyncSession, since: Optional[date] = None) -> dict:
    from app.services.class_board import HISTORY_START
    end = local_today() - timedelta(days=1)       # today is still in progress
    start = since or await training_start(db)
    if start is None:
        return {"available": False, "reason": "no training history"}
    first = start - timedelta(days=BASELINE_DAYS)
    excluded = await _excluded(db, first, end)

    dm = (await db.execute(
        select(DailyMetric.date, DailyMetric.rhr_value, DailyMetric.hrv_value, DailyMetric.ctl)
        .where(DailyMetric.date >= first).where(DailyMetric.date <= end)
    )).all()
    keep = lambda d: d not in excluded
    rhr = {r.date: r.rhr_value for r in dm if r.rhr_value is not None and keep(r.date)}
    hrv = {r.date: r.hrv_value for r in dm if r.hrv_value is not None and keep(r.date)}
    ctl = {r.date: r.ctl for r in dm if r.ctl is not None}

    walk = {d: v for d, v in (await _hm_daily(db, "walking_heart_rate_avg", first, end)).items() if keep(d)}
    vo2 = {d: v for d, v in (await _hm_daily(db, "vo2_max", first, end)).items() if keep(d)}
    weight = await _hm_daily(db, "weight", first, end)
    # Steps (note 73, 2026-10-20): unrecorded activity — walking a city with a toddler — is
    # invisible to TRIMP/CTL, which only counts workouts. Steps come from the motion sensor,
    # so unlike active calories they don't depend on heart-rate sampling (which thinned
    # out abroad). Travel days are deliberately KEPT: showing them is the point. Today is
    # left out because it's a partial day.
    today = local_today()
    steps = {d: v for d, v in (await _hm_daily(db, "steps", first, end, agg="sum")).items()
             if d < today and v > 0}

    hrr_rows = (await db.execute(
        select(Workout.start_time, Workout.hr_recovery_60)
        .where(Workout.hr_recovery_60.is_not(None)).where(Workout.max_hr >= HRR_MIN_PEAK)
        .where(Workout.start_time >= local_day_bounds_utc(first)[0])
    )).all()
    hrr: dict[date, float] = {}
    for ts, v in hrr_rows:
        d = ts.date()
        if keep(d):
            hrr[d] = max(hrr.get(d, 0.0), float(v))

    sessions = defaultdict(int)
    for (ts,) in (await db.execute(
            select(Workout.start_time).where(Workout.start_time >= local_day_bounds_utc(first)[0]))).all():
        sessions[ts.strftime("%Y-%m")] += 1

    # A longer reach than `first`: last year's window and months need their own days.
    yoy_from = date(end.year - 2, end.month, 1)
    yoy_excluded = await _excluded(db, yoy_from, end)
    yoy_keep = lambda s: {d: v for d, v in s.items() if d not in yoy_excluded}
    yoy_rhr = {r.date: r.rhr_value for r in (await db.execute(
        select(DailyMetric.date, DailyMetric.rhr_value)
        .where(DailyMetric.date >= yoy_from).where(DailyMetric.date <= end)
        .where(DailyMetric.rhr_value.is_not(None)))).all()}
    yoy_hrv = {r.date: r.hrv_value for r in (await db.execute(
        select(DailyMetric.date, DailyMetric.hrv_value)
        .where(DailyMetric.date >= yoy_from).where(DailyMetric.date <= end)
        .where(DailyMetric.hrv_value.is_not(None)))).all()}
    yoy_walk = yoy_keep(await _hm_daily(db, "walking_heart_rate_avg", yoy_from, end))
    yoy_vo2 = yoy_keep(await _hm_daily(db, "vo2_max", yoy_from, end))
    yoy_rhr, yoy_hrv = yoy_keep(yoy_rhr), yoy_keep(yoy_hrv)
    vs_last_year = {
        "start": start.isoformat(), "end": end.isoformat(),
        "walk_hr": year_over_year(yoy_walk, start, end, "down"),
        "vo2": year_over_year(yoy_vo2, start, end, "up", prior_best=past_best(yoy_vo2, start, "up")),
        "rhr": year_over_year(yoy_rhr, start, end, "down"),
        "hrv": year_over_year(yoy_hrv, start, end, "up"),
    }

    from app.models import BodyMeasurement
    tape = (await db.execute(select(BodyMeasurement).order_by(BodyMeasurement.date))).scalars().all()
    tape_out = None
    if tape:
        f, l = tape[0], tape[-1]
        cm_in = lambda v: round(v / 2.54, 1) if v is not None else None
        tape_out = {"first_date": f.date.isoformat(), "last_date": l.date.isoformat(),
                    "n": len(tape),
                    **{f"{k}_first": cm_in(getattr(f, f"{k}_cm")) for k in ("waist", "arm", "chest")},
                    **{f"{k}_last": cm_in(getattr(l, f"{k}_cm")) for k in ("waist", "arm", "chest")}}

    metrics = [
        with_last_year({"key": "rhr", "label": "Resting heart rate (overnight)", "unit": "bpm",
                        **_trend(rhr, start, end, "down"),
                        "note": "Overnight value, as on Today. Falls as aerobic fitness rises."},
                       yoy_rhr, end),
        with_last_year({"key": "hrv", "label": "HRV (overnight)", "unit": "ms",
                        **_trend(hrv, start, end, "up"),
                        "note": "The value readiness uses. Rises with aerobic fitness and recovery capacity."},
                       yoy_hrv, end),
        {"key": "hrr", "label": "Heart-rate recovery, 1 min", "unit": "bpm",
         **_trend(hrr, start, end, "up", min_points=8),
         "note": f"Drop in the first minute after workouts peaking ≥{HRR_MIN_PEAK} bpm. Faster = fitter."},
        with_last_year({"key": "walk_hr", "label": "Walking heart rate", "unit": "bpm",
                        **_trend(walk, start, end, "down"),
                        "note": "Apple's daily walking average — depends on pace and terrain, so noisy."},
                       yoy_walk, end),
        {"key": "vo2", "label": "VO₂ max (Apple estimate)", "unit": "ml/kg/min",
         **_trend(vo2, start, end, "up", min_points=8),
         "note": "Estimated only on qualifying outdoor walks/runs; unreliable for gym-based training."},
    ]
    strength = await strength_progress(db, max(since or HISTORY_START, HISTORY_START), end)
    return {
        "available": True,
        "headlines": {"cardio": cardio_headline(metrics, vs_last_year),
                      "strength": strength_headline(strength)},
        "start": start.isoformat(), "baseline_from": first.isoformat(), "end": end.isoformat(),
        "days": (end - start).days,
        "metrics": metrics,
        "vs_last_year": vs_last_year,
        "capacity": {
            "ctl_before": round(st.median(v for d, v in ctl.items() if d < start), 1)
                          if any(d < start for d in ctl) else None,
            "ctl_now": round(st.median(v for d, v in ctl.items() if d > end - timedelta(days=RECENT_DAYS)), 1)
                       if ctl else None,
            "sessions_by_month": [{"month": m, "n": n} for m, n in sorted(sessions.items())],
        },
        "weight": {**_trend(weight, start, end, None), "unit": "lb",
                   "note": ("A one-time step of ~3 lb between 2026-03-26 and 03-28 (then flat) is fluid, "
                            "not tissue — creatine and/or the start of training. Scale body-fat % only "
                            "tracks weight here, so it cannot separate muscle from fat.")},
        "activity": {**_trend(steps, start, end, None), "unit": "steps/day",
                     "note": ("Everyday movement, workouts included — the volume CTL can't see. "
                              "Travel days are kept on purpose — the narrow shaded strips are trips. "
                              "Not scored.")},
        "tape": tape_out,
        "strength": strength,
        "training": await training_summary(db, end, first),
    }
