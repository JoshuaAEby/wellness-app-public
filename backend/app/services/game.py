"""Gamification — the RPG stats layer (read-only mirror over real data).

Design: docs/GAMIFICATION.md. THE INVARIANT: this module IMPORTS from the metrics /
habits / training services and writes ONLY to game-owned tables (`game_stat_daily`).
No analysis path may read those tables back. Nothing here ever writes to
daily_metrics, correlations, experiments, or the coach.

## The model (revised 2026-08-04, user decision)

Every stat is a **cumulative XP accumulator**, not a band-function over a raw value.
The original design banded raw values, which meant holding steady at a high level
showed as no progress. The user's requirement: "as long as I'm pushing myself and working
hard I should get credit for that."

Note this is deliberately NOT a trajectory/derivative model, despite the word
"trajectory" in the conversation that produced it — a derivative pays out only while
something is *changing* and reads zero at steady state, which is the exact failure
being fixed. It is stock-and-flow:

    score_d  = today's performance vs YOUR OWN rolling standard, in [0, 2]
               (≈1.0 = performing at your own norm → full daily credit)
    xp_d     = BASE_XP_PER_DAY * score_d          (never negative)
    cum_xp   = Σ xp_d                             (never decreases)
    level    = max L with cum_xp >= LEVEL_K * L**LEVEL_EXP   (superlinear)

Consequences, all intended:
  * Steady hard work keeps leveling you up, decelerating as levels get expensive.
  * A bad week slows accrual; it never takes a level away (forgiveness by design).
  * Percentile-vs-your-own-trailing-year references mean an all-time-best reading
    earns near-max XP — no frozen epoch, no privileged reference window.
  * Flagged days (travel/sick/incomplete) accrue nothing: held, not penalized.

## Experiment safety

No stat pays a discrete jackpot for a single same-day action on the variable under
active randomization. Strength and Endurance read trailing-28d tonnage and CTL (a
42-day EWMA) respectively, so one workout nudges the accrual rate rather than paying
out. See the Guardrails section of the design doc.
"""
import logging
import math
import statistics as st
from datetime import date as Date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, union_all
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.acwr import ACWR_HIGH
from app.config import settings
from app.models import (
    DailyMetric,
    DayFlag,
    FoodLog,
    GameStatDaily,
    HabitLog,
    HealthMetric,
    HevyWorkout,
    SleepSession,
    SubjectiveLog,
    UserSettings,
)
from app.services.adherence import ADHERENCE_HABITS
from app.services.day_flags import excluded_dates
from app.services.habit_metrics import build_calendar
from app.timeutils import local_date_sql, local_today, to_local_date, to_local
from app.foodday import food_date_sql

logger = logging.getLogger(__name__)

# ── Level curve ───────────────────────────────────────────────────────────────
# 100 levels, not 10 (user decision 2026-08-04), superlinear so early levels come
# fast and later ones cost more.
#
# LEVEL_K was 4.0 (chosen to preserve the old 10-level scale's total XP) and is now
# 8.0. At 4.0 the March-epoch run still put the user at Lv 42 after five months with a
# ~2-year horizon to cap, because his Mar-Aug scores averaged ~1.5x his own norm
# (a sustained growth phase earns ~15 xp/day, not the nominal 10). Doubling K puts
# him in the low 20s now and pushes the cap to ~5.5 years at a 10 xp/day plateau.
#
# This is the ONE knob for pacing: raise K if levels come too fast, lower it if too
# slow. Nothing else needs to change.
BASE_XP_PER_DAY = 10.0
LEVEL_K = 8.0
LEVEL_EXP = 1.7
MAX_LEVEL = 100

# ── XP epoch ──────────────────────────────────────────────────────────────────
# XP accrual starts here. March 2026 is when the user started training in earnest and
# when most of this measurement pipeline was built, so it is the honest start of
# the run — crediting 2021-2025 data would have him near the level cap on day one
# with no headroom (the first cut landed at levels 67-83 of 100).
#
# IMPORTANT: this gates ACCRUAL ONLY. The rolling reference windows (the 180d
# ratio norms and 365d percentiles) still read pre-epoch history, so "your own
# norm" is properly calibrated from the very first scored day instead of
# bootstrapping off a handful of observations.
XP_EPOCH_START = Date(2026, 3, 1)

# Rolling reference windows
REF_DAYS = 365          # percentile references look back this far
RATIO_REF_DAYS = 180    # ratio-style references (tonnage, CTL)
SCORE_CAP = 2.0         # a day can earn at most 2x the nominal rate

STATS = ("strength", "endurance", "vitality", "attunement", "constitution", "discipline")

STAT_META = {
    "strength":     {"label": "Strength",     "icon": "💪", "color": "#f97316",
                     "basis": "trailing-28d tonnage vs your own 180-day norm"},
    "endurance":    {"label": "Endurance",    "icon": "🫁", "color": "#22d3ee",
                     "basis": "CTL (chronic training load) vs your own 180-day norm"},
    "vitality":     {"label": "Vitality",     "icon": "❤️", "color": "#f43f5e",
                     "basis": "HRV + resting-HR baselines, percentile vs your trailing year"},
    "attunement":   {"label": "Attunement",   "icon": "🌙", "color": "#818cf8",
                     "basis": "wake-time regularity + sleep debt + morning light"},
    "constitution": {"label": "Constitution", "icon": "🛡️", "color": "#a78bfa",
                     "basis": "protein consistency + body composition + illness resistance"},
    "discipline":   {"label": "Discipline",   "icon": "⚔️", "color": "#34d399",
                     "basis": "difficulty-weighted habit adherence"},
}


def level_threshold(level: int) -> float:
    """Cumulative XP required to REACH `level`. level 0 = 0 XP."""
    if level <= 0:
        return 0.0
    return LEVEL_K * (level ** LEVEL_EXP)


def level_for_xp(xp: float) -> int:
    lvl = 0
    while lvl < MAX_LEVEL and xp >= level_threshold(lvl + 1):
        lvl += 1
    return lvl


def level_progress(xp: float) -> dict:
    """Level + progress toward the next one."""
    lvl = level_for_xp(xp)
    cur = level_threshold(lvl)
    nxt = level_threshold(lvl + 1) if lvl < MAX_LEVEL else None
    if nxt is None:
        return {"level": lvl, "xp": round(xp, 1), "xp_into_level": None,
                "xp_to_next": None, "pct_to_next": 100.0}
    span = nxt - cur
    return {
        "level": lvl,
        "xp": round(xp, 1),
        "xp_into_level": round(xp - cur, 1),
        "xp_to_next": round(nxt - xp, 1),
        "pct_to_next": round(100.0 * (xp - cur) / span, 1) if span > 0 else 0.0,
    }


# ── Scoring helpers ───────────────────────────────────────────────────────────

def _pct_rank(value: float, history: list[float], higher_is_better: bool = True) -> Optional[float]:
    """Percentile of `value` within `history`, in [0,1]. None if too little history."""
    hist = [h for h in history if h is not None]
    if value is None or len(hist) < 10:
        return None
    below = sum(1 for h in hist if h < value)
    p = below / len(hist)
    return p if higher_is_better else 1.0 - p


def _ratio_score(value: Optional[float], ref: Optional[float]) -> Optional[float]:
    """value/ref clamped to [0, SCORE_CAP]. ref = your own rolling norm, so 1.0 means
    'performing exactly at your usual standard' → full daily credit."""
    if value is None or not ref or ref <= 0:
        return None
    return max(0.0, min(SCORE_CAP, value / ref))


def _pct_score(p: Optional[float]) -> Optional[float]:
    """Percentile in [0,1] → score in [0, SCORE_CAP], linear. p=0.5 (your median) → 1.0."""
    if p is None:
        return None
    return max(0.0, min(SCORE_CAP, 2.0 * p))


def _mad(vals: list[float]) -> float:
    """Median absolute deviation, scaled by 1.4826 so it matches the SD on normally
    distributed data.

    Used instead of the standard deviation for wake/bed-time regularity because SD
    has a breakdown point of 0 — a SINGLE bad record dominates it. Real case
    (2026-08-05): two malformed sleep sessions (a night the watch ended at 01:16,
    and a night+nap stitched into one session whose end was 15:38) pushed the
    14-night wake-time SD to 178 min while the true spread was ~39 min. Attunement
    read 6th percentile and the UI called it the user's biggest opportunity — both
    artefacts. MAD has a 50% breakdown point, so no single record can do that, and
    it needs no hand-maintained list of bad dates.
    """
    if not vals:
        return 0.0
    m = st.median(vals)
    return 1.4826 * st.median([abs(v - m) for v in vals])


def _median(vals: list[float]) -> Optional[float]:
    v = [x for x in vals if x is not None]
    return st.median(v) if v else None


def _comp(key, label, weight, score, raw=None, detail=None) -> dict:
    """One component of a stat's daily score.

    `score` is the component's sub-score in [0,1]; None means no data today, in
    which case it is dropped from the blend (neither helps nor hurts). `weight` is
    its share of the stat. `detail` is the human string the UI shows.
    """
    return {
        "key": key, "label": label, "weight": weight,
        "score": None if score is None else round(max(0.0, min(1.0, score)), 4),
        "raw": raw, "detail": detail,
    }


def _blend(components: list[dict]) -> Optional[float]:
    """Weighted mean of component sub-scores → stat score in [0, SCORE_CAP].

    Missing components drop out and the remaining weights renormalise.
    """
    live = [c for c in components if c["score"] is not None]
    tw = sum(c["weight"] for c in live)
    if not live or tw <= 0:
        return None
    return SCORE_CAP * sum(c["weight"] * c["score"] for c in live) / tw


def _ratio_sub(value: Optional[float], ref: Optional[float]) -> Optional[float]:
    """Ratio-style component → sub-score in [0,1]. 0.5 = exactly at your own norm
    (ratio 1.0), 1.0 = double it. Puts ratio stats on the same 0..1 scale as the
    percentile stats so one blend formula covers every stat."""
    if value is None or not ref or ref <= 0:
        return None
    return max(0.0, min(1.0, (value / ref) / SCORE_CAP))


def levers(components: list[dict]) -> list[dict]:
    """Rank components by how much XP/day is still available from each.

    xp = BASE * SCORE_CAP * Σ(wᵢ·sᵢ)/Σw, so ∂xp/∂sᵢ = BASE * SCORE_CAP * wᵢ/Σw, and
    the most component i can still yield is that derivative times its remaining
    headroom (1 - sᵢ). Ranking by THAT answers "where does effort buy the most XP
    right now", which is not the same question as "which component is lowest" — a
    low component with a small weight is a worse lever than a middling one with a
    large weight.
    """
    live = [c for c in components if c.get("score") is not None]
    tw = sum(c["weight"] for c in live)
    if not live or tw <= 0:
        return []
    out = []
    for c in live:
        head = max(0.0, 1.0 - c["score"])
        out.append({
            **c,
            "headroom": round(head, 3),
            "xp_available": round(BASE_XP_PER_DAY * SCORE_CAP * (c["weight"] / tw) * head, 2),
        })
    return sorted(out, key=lambda c: c["xp_available"], reverse=True)



# ── Raw series builders ───────────────────────────────────────────────────────

async def _tonnage_by_day(db: AsyncSession) -> dict[Date, float]:
    rows = (await db.execute(
        select(HevyWorkout.start_time, HevyWorkout.tonnage_kg)
        .where(HevyWorkout.tonnage_kg.isnot(None))
    )).all()
    out: dict[Date, float] = {}
    tz = ZoneInfo(settings.tz)
    for ts, kg in rows:
        d = to_local_date(ts)
        out[d] = out.get(d, 0.0) + (kg or 0.0)
    return out


async def _daily_metrics(db: AsyncSession) -> dict[Date, DailyMetric]:
    rows = (await db.execute(select(DailyMetric))).scalars().all()
    return {r.date: r for r in rows}


async def _waketime_sd(db: AsyncSession) -> dict[Date, float]:
    """Rolling 14-night robust spread of local wake time, in minutes (MAD-based).
    Lower = more regular. See _mad for why this is not a standard deviation."""
    rows = (await db.execute(
        select(SleepSession.date, SleepSession.wake_time)
        .where(SleepSession.wake_time.isnot(None))
        .order_by(SleepSession.date)
    )).all()
    tz = ZoneInfo(settings.tz)
    wake = {}
    for d, wt in rows:
        lt = to_local(wt)
        wake[d] = lt.hour * 60 + lt.minute
    out: dict[Date, float] = {}
    for d in sorted(wake):
        prior = [wake[d - timedelta(days=i)] for i in range(0, 14)
                 if (d - timedelta(days=i)) in wake]
        if len(prior) >= 7:
            out[d] = _mad(prior)
    return out


async def _protein_by_day(db: AsyncSession) -> dict[Date, float]:
    # Plain func.date, NOT local_date_sql: food entries are stored at local noon, so
    # func.date() already IS their local day (timeutils.local_date_sql says so
    # explicitly). Converting again is a double-shift that only happens to be
    # harmless at offsets within +/-11h.
    day = food_date_sql(FoodLog.timestamp)
    rows = (await db.execute(
        select(day.label("d"), func.sum(FoodLog.protein_g))
        .group_by(day)
    )).all()
    out = {}
    for d, p in rows:
        try:
            out[Date.fromisoformat(d)] = p or 0.0
        except (TypeError, ValueError):
            continue
    return out


async def _metric_by_day(db: AsyncSession, metric_type: str) -> dict[Date, float]:
    day = local_date_sql(HealthMetric.timestamp)
    rows = (await db.execute(
        select(day.label("d"), func.avg(HealthMetric.value))
        .where(HealthMetric.metric_type == metric_type)
        .group_by(day)
    )).all()
    out = {}
    for d, v in rows:
        try:
            out[Date.fromisoformat(d)] = v
        except (TypeError, ValueError):
            continue
    return out


def _rolling(series: dict[Date, float], day: Date, days: int) -> list[float]:
    return [v for d, v in series.items()
            if v is not None and 0 <= (day - d).days < days]


def _win_before(arr: list, i: int, days: int, truthy: bool = False) -> list:
    """Values in the `days` days STRICTLY BEFORE index i.

    Matches the original `0 < (d - dd).days <= days` dictionary scans exactly, but
    as a bounded slice instead of a full pass. `truthy` mirrors the two filter
    styles those scans used — some tested `and dm[dd].ctl` (dropping a 0.0), others
    `is not None`. Kept distinct rather than unified: unifying them would silently
    change scores.
    """
    seg = arr[max(0, i - days):i]
    return [v for v in seg if v] if truthy else [v for v in seg if v is not None]


def _win_incl(arr: list, i: int, days: int) -> list:
    """Values in the `days`-day window ENDING AT and INCLUDING index i.

    Matches `0 <= (d - dd).days < days`, i.e. indices [i-days+1, i].
    """
    return [v for v in arr[max(0, i - days + 1):i + 1] if v is not None]


def _trailing_sum(series: dict[Date, float], day: Date, days: int) -> float:
    return sum(v for d, v in series.items() if 0 <= (day - d).days < days)


def _smoothed(series: dict[Date, float], day: Date, days: int) -> Optional[float]:
    vals = _rolling(series, day, days)
    return st.mean(vals) if vals else None


# ── Discipline: difficulty-weighted habit points ──────────────────────────────
# A habit's point value is proportional to (1 - its historical hit rate): habits you
# nearly always do are worth little, hard ones are worth a lot. This is the variance
# monitor's logic in a costume — it makes the stat measure discipline rather than
# attendance. Weights are computed once per recompute over the full history (they
# drift slowly; see the design doc's note on freezing them if that becomes an issue).
MIN_HABIT_WEIGHT = 0.15


def _habit_weights(calendar: list[dict], logged_days: set) -> dict[str, float]:
    """Difficulty weight per habit = 1 - its hit rate, floored at MIN_HABIT_WEIGHT.

    Computed ONLY over days that have a real HabitLog row and fall on/after the XP
    epoch. Using the whole calendar was wrong: build_calendar emits a row for every
    day back to 2021, where nothing was logged, so every manual habit looked ~0%
    adherence and drew a near-maximal weight (creatine scored 0.963 "difficulty"
    despite ~77% real adherence, almost the same as meditation at a true 0%).
    """
    weights = {}
    rows = [r for r in calendar
            if Date.fromisoformat(r["date"]) in logged_days
            and Date.fromisoformat(r["date"]) >= XP_EPOCH_START]
    for h in ADHERENCE_HABITS:
        if h.get("no_xp"):
            continue
        key = h["key"]
        num = den = 0
        for row in rows:
            v = row.get(key)
            if h["manual"]:
                den += 1
                if v is True:
                    num += 1
            elif v is True or v is False:
                den += 1
                if v is True:
                    num += 1
        rate = (num / den) if den else 0.5
        weights[key] = max(MIN_HABIT_WEIGHT, 1.0 - rate)
    return weights


def _discipline_components(row: dict, weights: dict[str, float]) -> list[dict]:
    """One component per habit, weighted by difficulty. Unjudgeable auto habits are
    dropped (score None) rather than counted as misses."""
    comps = []
    for h in ADHERENCE_HABITS:
        key = h["key"]
        v = row.get(key)
        if not h["manual"] and v is None:
            score = None                   # unjudgeable auto habit — excluded
        else:
            score = 1.0 if v is True else 0.0
        w = weights.get(key, 0.5)
        comps.append(_comp(
            key, h["label"], w, score,
            raw=v,
            detail=("done today" if v is True else "not done today" if score is not None
                    else "not judgeable today") + f" · worth {w:.2f} (difficulty-weighted)",
        ))
    return comps


# ── The main computation ──────────────────────────────────────────────────────

async def compute_series(db: AsyncSession, through: Optional[Date] = None) -> dict:
    """Compute the full per-day XP/level series for every stat.

    Returns {stat: [ {date, raw, score, xp, cum_xp, level}, ... ]}. Pure read — the
    caller decides whether to persist.
    """
    tz = ZoneInfo(settings.tz)
    through = through or local_today()

    dm = await _daily_metrics(db)
    tonnage = await _tonnage_by_day(db)
    wake_sd = await _waketime_sd(db)
    protein = await _protein_by_day(db)
    bodyfat = await _metric_by_day(db, "body_fat_percentage")
    leanmass = await _metric_by_day(db, "lean_body_mass")
    weight = await _metric_by_day(db, "weight")

    us = (await db.execute(select(UserSettings).limit(1))).scalar_one_or_none()
    protein_goal = _protein_goal(us, weight, through)

    # Start from the earliest day any stat has data for.
    candidates = [min(s) for s in (dm, tonnage, wake_sd, protein) if s]
    if not candidates:
        return {s: [] for s in STATS}
    start = min(candidates)

    # Habit calendar + difficulty weights (habit data is young; this is cheap)
    hstart = min(start, through)
    # Dates with an ACTUAL HabitLog row. build_calendar returns a row for every
    # day in range with derived habits filled in (no_alcohol is True whenever no
    # alcohol was recorded, etc.), so without this gate Discipline would earn XP
    # back to 2021 on defaults for habits that weren't being tracked yet.
    logged_habit_days = {r[0] for r in (await db.execute(select(HabitLog.date))).all()}

    calendar = await build_calendar(db, hstart, through)
    cal_by_date = {Date.fromisoformat(r["date"]): r for r in calendar}
    weights = _habit_weights(calendar, logged_habit_days)

    # Analysis-excluded days (travel / sick / poor env / high stress / other).
    # NOTE this deliberately does NOT include `incomplete_data`, which is
    # nutrition-only — an unlogged dinner shouldn't freeze Strength. Incomplete
    # nutrition days are handled inside _constitution's protein denominator.
    flagged = set(await excluded_dates(db, start, through))
    incomplete = {r[0] for r in (await db.execute(
        select(DayFlag.date).where(DayFlag.reason == "incomplete_data")
    )).all()}

    # ── Day-indexed arrays (perf, 2026-08-31) ────────────────────────────────
    # Every reference window below used to be a full-dictionary scan inside the
    # per-day loop — O(days²) over 1,924 days — and each scan read ORM attributes
    # (dm[dd].ctl and friends), which cost 2.9 MILLION SQLAlchemy instrumented
    # attribute loads, 1.1 s of the 4.8 s job in pure descriptor overhead.
    #
    # Days are contiguous, so a date window is a contiguous index slice. Values are
    # extracted to plain lists ONCE, and the scans become bounded slices.
    all_days = [start + timedelta(days=i) for i in range((through - start).days + 1)]
    day_i = {d: i for i, d in enumerate(all_days)}

    def _col(src: dict, attr: str | None = None) -> list:
        if attr is None:
            return [src.get(d) for d in all_days]
        return [getattr(src[d], attr) if d in src else None for d in all_days]

    ctl_arr = _col(dm, "ctl")
    hrv_arr = _col(dm, "hrv_baseline")
    rhr_arr = _col(dm, "rhr_baseline")
    debt_arr = _col(dm, "sleep_debt_hours")
    sd_arr = _col(wake_sd)
    ton_arr = _col(tonnage)
    bf_arr = _col(bodyfat)
    lm_arr = _col(leanmass)

    # Protein: precomputed as "did this day hit goal", None when the day has no
    # nutrition row OR is incomplete-flagged. Folding the incomplete filter in here
    # keeps _constitution's denominator identical to the old `days` list.
    prot_hit_arr = [None if (d not in protein or d in incomplete)
                    else (protein[d] >= protein_goal) for d in all_days]

    # Illness: None = no daily_metrics row that day, True = an elevated-marker day.
    # Precomputed so the 28-day window never touches an ORM attribute.
    def _bad(r):
        return (r.hrv_z_score is not None and r.hrv_z_score <= -1.0
                and r.rhr_z_score is not None and r.rhr_z_score >= 1.0)
    ill_arr = [_bad(dm[d]) if d in dm else None for d in all_days]

    # Trailing-28d tonnage series (the Strength raw). `0 <= (d - dd).days < 28`
    # is the inclusive-of-today window, i.e. indices [i-27, i].
    ton28: dict[Date, float] = {}
    ton28_arr: list[float] = []
    for i, dd in enumerate(all_days):
        v = sum(x for x in ton_arr[max(0, i - 27):i + 1] if x)
        ton28[dd] = v
        ton28_arr.append(v)

    out: dict[str, list[dict]] = {s: [] for s in STATS}
    cum = {s: 0.0 for s in STATS}

    d = start
    while d <= through:
        i = day_i[d]
        m = dm.get(d)
        held = d in flagged
        raws: dict[str, Optional[float]] = {}
        comps: dict[str, list[dict]] = {}

        # ── Strength: trailing-28d tonnage vs your own 180d norm ──────────────
        raw = ton28.get(d)
        ref = _median([v for v in _win_before(ton28_arr, i, RATIO_REF_DAYS) if v > 0])
        sub = _ratio_sub(raw, ref) if raw else (0.0 if ref else None)
        comps["strength"] = [_comp(
            "tonnage", "28-day tonnage", 1.0, sub, raw,
            (f"{raw:,.0f} kg over 28d vs your {ref:,.0f} kg norm" if raw and ref
             else "no lifting logged in the last 28 days" if ref else "no tonnage history yet"),
        )]
        raws["strength"] = raw

        # ── Endurance: CTL vs your own 180d norm ─────────────────────────────
        ctl = m.ctl if m else None
        ctl_ref = _median(_win_before(ctl_arr, i, RATIO_REF_DAYS, truthy=True))
        comps["endurance"] = [_comp(
            "ctl", "Chronic training load", 1.0, _ratio_sub(ctl, ctl_ref), ctl,
            (f"CTL {ctl:.1f} vs your {ctl_ref:.1f} norm" if ctl and ctl_ref else "no load history yet"),
        )]
        raws["endurance"] = ctl

        # ── Vitality: HRV + RHR baselines, percentile vs trailing year ────────
        hrv = m.hrv_baseline if m else None
        rhr = m.rhr_baseline if m else None
        hrv_hist = _win_before(hrv_arr, i, REF_DAYS, truthy=True)
        rhr_hist = _win_before(rhr_arr, i, REF_DAYS, truthy=True)
        p_hrv = _pct_rank(hrv, hrv_hist, higher_is_better=True)
        p_rhr = _pct_rank(rhr, rhr_hist, higher_is_better=False)
        comps["vitality"] = [
            _comp("hrv", "HRV baseline", 0.5, p_hrv, hrv,
                  f"{hrv:.1f} ms — {p_hrv:.0%} of your last year" if hrv and p_hrv is not None
                  else "not enough HRV history"),
            _comp("rhr", "Resting HR baseline", 0.5, p_rhr, rhr,
                  f"{rhr:.1f} bpm — better than {p_rhr:.0%} of your last year"
                  if rhr and p_rhr is not None else "not enough RHR history"),
        ]
        raws["vitality"] = hrv

        # ── Attunement: wake-time regularity + sleep debt + morning light ─────
        sd = wake_sd.get(d)
        sd_hist = _win_before(sd_arr, i, REF_DAYS)
        p_sd = _pct_rank(sd, sd_hist, higher_is_better=False)   # lower SD is better
        debt = m.sleep_debt_hours if m else None
        debt_hist = _win_before(debt_arr, i, REF_DAYS)
        p_debt = _pct_rank(debt, debt_hist, higher_is_better=False)
        light = cal_by_date.get(d, {}).get("outdoor_light_am")
        # Gated on wake-time regularity specifically: sleep_debt_hours exists in
        # daily_metrics back to 2021 and would otherwise carry the stat on its own,
        # long before there was any sleep-stage data to be regular about.
        if p_sd is None:
            comps["attunement"] = []
        else:
            comps["attunement"] = [
                _comp("wake_sd", "Wake-time regularity", 0.425, p_sd, sd,
                      f"±{sd:.0f} min typical swing over 14 nights — better than "
                      f"{p_sd:.0%} of your last year"),
                _comp("sleep_debt", "Sleep debt", 0.425, p_debt, debt,
                      f"{debt:.1f}h debt — better than {p_debt:.0%} of your last year"
                      if debt is not None and p_debt is not None else "no sleep-debt history"),
                _comp("morning_light", "Morning daylight", 0.15,
                      None if light is None else (1.0 if light else 0.0), light,
                      "got morning light" if light else "no morning light credit"
                      if light is not None else "not judgeable today"),
            ]
        raws["attunement"] = sd

        # ── Constitution: protein + body composition + illness resistance ─────
        comps["constitution"], raws["constitution"] = _constitution(
            i, prot_hit_arr, bf_arr, lm_arr, ill_arr, protein_goal
        )

        # ── Discipline: difficulty-weighted habit adherence ───────────────────
        row = cal_by_date.get(d)
        comps["discipline"] = (
            _discipline_components(row, weights)
            if (row and d in logged_habit_days) else []
        )
        raws["discipline"] = None

        pre_epoch = d < XP_EPOCH_START
        for s in STATS:
            sc = _blend(comps[s])
            # Pre-epoch days, held days (travel/sick) and no-data days accrue
            # nothing. Pre-epoch rows are still emitted (raw/score/components
            # populated) so history charts and reference windows keep full context.
            xp = 0.0 if (pre_epoch or held or sc is None) else BASE_XP_PER_DAY * sc
            cum[s] += xp
            out[s].append({
                "date": d,
                "raw": raws[s],
                "score": None if sc is None else round(sc, 3),
                "xp": round(xp, 2),
                "cum_xp": round(cum[s], 2),
                "level": level_for_xp(cum[s]),
                "held": held,
                "components": comps[s],
            })
        d += timedelta(days=1)

    return out


def _protein_goal(us, weight: dict, through: Date) -> float:
    """The app's protein goal, against a 90-day MEAN weight rather than the latest.

    Deliberately not `resolve_protein_goal()`: this drives a gamification stat over a
    long window, where a single scale reading moving the target would make past days
    re-score. The multiplier table and default now come from the canonical source, so
    only the weight basis differs (the old inline copy also defaulted to 0.75 where
    everywhere else defaults to 0.80).
    """
    if us and us.protein_goal_g:
        return float(us.protein_goal_g)
    from app.services.nutrition_goals import PROTEIN_MULT, PROTEIN_MULT_DEFAULT
    m = PROTEIN_MULT.get((us.training_goal if us else "building") or "building",
                         PROTEIN_MULT_DEFAULT)
    recent = [v for dd, v in weight.items() if (through - dd).days <= 90]
    lb = st.mean(recent) if recent else 180.0
    return lb * m


def _constitution(i, prot_hit, bf_arr, lm_arr, ill_arr, goal):
    """Protein consistency (primary — it has by far the most headroom) + body
    composition (the user explicitly wants BF% and lean mass tracked) + illness
    resistance. Composition is split into two components rather than one so the
    lever ranking can distinguish "lose fat" from "hold lean mass".

    BF% is 30-day smoothed: the source is a bioimpedance scale, so unsmoothed
    day-to-day movement is noise, not signal.
    """
    # protein: share of the last 28 complete (non-incomplete-flagged) logged days
    # that hit goal. prot_hit is None on days with no nutrition row or an
    # incomplete flag, so the window's non-None count IS the old `days` length.
    hits = _win_incl(prot_hit, i, 28)
    n_days = len(hits)
    p_protein = (sum(1 for h in hits if h) / n_days) if n_days else None

    bf_win = _win_incl(bf_arr, i, 30)
    bf = st.mean(bf_win) if bf_win else None
    p_bf = _pct_rank(bf, _win_before(bf_arr, i, REF_DAYS), higher_is_better=False)

    lm_win = _win_incl(lm_arr, i, 30)
    lm = st.mean(lm_win) if lm_win else None
    lm_ref = _median(_win_before(lm_arr, i, RATIO_REF_DAYS))
    p_lm = None
    if lm and lm_ref:
        # 1.0 at parity with your own 180-day norm, scaled +/- around it
        p_lm = max(0.0, min(1.0, 0.5 + (lm - lm_ref) / max(1.0, 0.05 * lm_ref) * 0.5))

    # ill_arr is None on days with no daily_metrics row, so the window's non-None
    # count matches the old `len(ill_days)` and its True count the old `bad`.
    ill_win = _win_incl(ill_arr, i, 28)
    p_ill = (1.0 - sum(1 for b in ill_win if b) / len(ill_win)) if ill_win else None

    # Illness resistance alone must NOT carry the stat: hrv/rhr z-scores exist in
    # daily_metrics long before nutrition or body-composition tracking began, and a
    # lone ~1.0 illness-free score would peg Constitution at max level.
    if p_protein is None and p_bf is None and p_lm is None:
        return [], bf

    return [
        _comp("protein", "Protein consistency", 0.45, p_protein, p_protein,
              f"{p_protein:.0%} of complete days hit {goal:.0f}g ({n_days} days)"
              if p_protein is not None else "no complete nutrition days yet"),
        _comp("body_fat", "Body fat", 0.175, p_bf, bf,
              f"{bf:.1f}% (30d avg) — better than {p_bf:.0%} of your last year"
              if bf and p_bf is not None else "no body-fat history"),
        _comp("lean_mass", "Lean mass", 0.175, p_lm, lm,
              f"{lm:.1f} lb (30d avg) vs your {lm_ref:.1f} lb norm"
              if lm and lm_ref else "no lean-mass history"),
        _comp("illness_free", "Illness resistance", 0.20, p_ill, p_ill,
              f"{p_ill:.0%} of the last 28 days clear of elevated recovery markers"
              if p_ill is not None else "no recovery-marker history"),
    ], bf


# ── Persistence ───────────────────────────────────────────────────────────────

async def recompute_and_store(db: AsyncSession, through: Optional[Date] = None) -> dict:
    """Recompute the whole series and replace `game_stat_daily`. Idempotent.

    Cheap enough to do in full (a few thousand rows); levels are slow-moving so this
    runs once a day from the morning pipeline.
    """
    series = await compute_series(db, through)
    await db.execute(delete(GameStatDaily))
    rows = []
    for stat, points in series.items():
        for p in points:
            rows.append({
                "date": p["date"], "stat": stat, "raw": p["raw"], "score": p["score"],
                "xp": p["xp"], "cum_xp": p["cum_xp"], "level": p["level"],
                "components": p["components"],
            })
    for i in range(0, len(rows), 500):
        await db.execute(sqlite_insert(GameStatDaily), rows[i:i + 500])
    await db.commit()
    logger.info("Game stats recomputed: %d rows", len(rows))
    return {"rows": len(rows), "stats": {s: (series[s][-1]["level"] if series[s] else 0)
                                         for s in STATS}}


def level_up_drivers(rows_desc: list, up_idx: int, max_window: int = 35) -> list[dict]:
    """Which components actually produced the XP that carried a level-up.

    `rows_desc` is newest-first. The window runs from the level-up day back through
    every day spent at the previous level — i.e. the stretch of effort that banked
    the XP — capped at `max_window` days.

    Contribution is exact, not a guess: each day's XP is
    BASE * SCORE_CAP * Σ(wᵢ·sᵢ)/Σw, so component i contributed
    BASE * SCORE_CAP * wᵢ·sᵢ/Σw that day. Summing over the window and dividing by
    the total gives each component's true share of the level.

    NOTE ON FRAMING: for behaviour-backed stats (Discipline, Strength, Attunement)
    these ARE the behaviours, and naming them reinforces what worked. For
    physiology-backed stats (Vitality) the drivers are outcomes — HRV rose — NOT
    things the user did. The UI must say "what earned it", never "what you did",
    because attributing an HRV rise to a specific behaviour is precisely the causal
    claim this app refuses to assert (that is the correlation engine's job).
    """
    if up_idx >= len(rows_desc):
        return []
    lvl = rows_desc[up_idx].level
    if lvl is None:
        return []

    window = []
    for r in rows_desc[up_idx:]:
        if r.level is None or r.level < lvl - 1:
            break
        window.append(r)
        if len(window) >= max_window:
            break
    if not window:
        return []

    agg: dict[str, dict] = {}
    for r in window:
        live = [c for c in (r.components or []) if c.get("score") is not None]
        tw = sum(c["weight"] for c in live)
        if tw <= 0:
            continue
        for c in live:
            e = agg.setdefault(c["key"], {"key": c["key"], "label": c["label"],
                                          "xp": 0.0, "scores": []})
            e["xp"] += BASE_XP_PER_DAY * SCORE_CAP * c["weight"] * c["score"] / tw
            e["scores"].append(c["score"])

    total = sum(e["xp"] for e in agg.values())
    if total <= 0:
        return []

    out = []
    for e in agg.values():
        out.append({
            "key": e["key"],
            "label": e["label"],
            "share": round(e["xp"] / total, 3),
            "xp": round(e["xp"], 1),
            "mean_score": round(sum(e["scores"]) / len(e["scores"]), 3),
        })
    out.sort(key=lambda x: x["share"], reverse=True)
    return out[:3]


# ── Condition: today's buffs / debuffs / ailments (STATE, not progress) ───────
# Everything here is a costume over a signal the app ALREADY computes and already
# shows the user, so it leaks nothing the blinded coach hides (readiness numbers
# live on the Today tab; the coach hides its *mechanism*, not the metrics).
# Daily fluctuation lives here and NEVER touches a permanent stat — a bad night is
# a debuff, not a level loss.

ACWR_OVERREACHING = ACWR_HIGH   # was 1.5; the app-wide high edge since 2026-11-04 (app/acwr.py)
SLEEP_DEBT_EXHAUSTED_H = 2.0


async def condition(db: AsyncSession, day: Optional[Date] = None) -> dict:
    """HP / mana / status ailments for `day` (default: today)."""
    tz = ZoneInfo(settings.tz)
    day = day or local_today()

    m = (await db.execute(
        select(DailyMetric).where(DailyMetric.date <= day)
        .order_by(DailyMetric.date.desc()).limit(1)
    )).scalar_one_or_none()

    subj = (await db.execute(
        select(SubjectiveLog).where(SubjectiveLog.date == day)
    )).scalar_one_or_none()

    flags = {r[0] for r in (await db.execute(
        select(DayFlag.reason).where(DayFlag.date == day)
    )).all()}

    ailments = []

    def ail(key, label, icon, detail):
        ailments.append({"key": key, "label": label, "icon": icon, "detail": detail})

    if "sick" in flags or (subj and subj.sick):
        ail("ill", "Ill", "🤒", "sick day — stats held, not penalised")
    if "travel" in flags:
        ail("away", "Away", "✈️", "travelling — stats held, not penalised")

    from app.services.recovery_alarm import assess_recovery_alarm
    alarm = await assess_recovery_alarm(db, day)
    if alarm["level"] == "elevated":
        flagged = ", ".join(mk["label"] for mk in alarm["markers"] if mk["flagged"])
        ail("run_down", "Run Down", "🌡️", flagged or "multiple recovery markers elevated")

    if m and m.sleep_debt_hours and m.sleep_debt_hours >= SLEEP_DEBT_EXHAUSTED_H:
        ail("exhausted", "Exhausted", "😴", f"{m.sleep_debt_hours:.1f}h sleep debt")
    if m and m.acwr and m.acwr >= ACWR_OVERREACHING:
        ail("overreaching", "Overreaching", "⚠️", f"ACWR {m.acwr:.2f} — acute load well above chronic")

    # Caffeine charge: a daytime stimulant buff that becomes a sleep debuff if it's
    # still on board at bedtime (the onboard_at_bed PK metric).
    from app.services.caffeine import day_summary as caffeine_day
    caff = await caffeine_day(db, day)

    return {
        "date": day.isoformat(),
        "hp": {"label": "Readiness", "value": round(m.readiness_score) if m and m.readiness_score else None,
               "max": 100, "state": m.readiness_label if m else None},
        "mana": {"label": "Energy", "value": subj.energy if subj else None, "max": 5},
        "caffeine": {"label": "Caffeine charge", "mg": caff.get("total_mg"),
                     "instances": len(caff.get("instances") or [])},
        "ailments": ailments,
    }


# ── Read API ──────────────────────────────────────────────────────────────────

# ── Momentum: current pace against the user's own recent norm ─────────────────
#
# Levels are cumulative and monotone by design — a bad stretch can't take a level away,
# it just stops earning. That makes the level unable to say "you're backsliding", which
# is exactly what a glanceable card should say. Momentum is the derivative the level
# deliberately hides.
#
# Defined as a DIFFERENCE in xp/day, normalised by the nominal rate — NOT a ratio to the
# baseline. A ratio is unstable when the baseline is near zero (early tracking, or a stat
# that has been dormant): on this data the ratio form had sd 1.71 with 69% of days beyond
# ±30%, which would have pinned the arrow to "large" almost always. The difference form
# has sd 0.32 with 75% of days inside ±0.10.
#
# Bands are ~0.5 sd and ~1.25 sd of the observed distribution, which puts roughly
# 77% / 9% / 14% of days in flat / small / large.
MOMENTUM_RECENT_DAYS = 14
MOMENTUM_BASE_DAYS = 90
MOMENTUM_MIN_RECENT = 7      # below this the recent window is too thin to read
MOMENTUM_MIN_BASE = 30
MOMENTUM_FLAT = 0.15         # ±1.5 xp/day against the nominal 10
MOMENTUM_LARGE = 0.40        # ±4 xp/day


def _momentum_drivers(rows_desc: list, top: int = 3) -> dict:
    """Which components moved the momentum, as an EXACT additive decomposition.

    Attributes each day's STORED xp across that day's live components in proportion to
    wᵢ·sᵢ. Attribution must ride on the stored value rather than recomputing
    BASE·SCORE_CAP·Σ(wᵢsᵢ)/Σw, because xp is force-zeroed on held and pre-epoch days
    while the components are still recorded — recomputing credits those days and the
    parts then do not sum to the whole (a −0.06 momentum decomposed to +0.22).

    Answers "why did this move", which is a different question from `levers`
    ("where does effort buy the most XP now"). A component can be the reason a stat is
    sliding while still being a poor lever, and vice versa.

    Note a component's sub-score can rise while its contribution falls: contribution is
    a share of the day's actual xp, so a component reporting on fewer days, or a shift
    in which components are live, moves it independently of the score. The score change
    is shown as context for the human, the delta is the arithmetic.
    """
    def per_component(rows):
        acc: dict[str, dict] = {}
        n = len(rows)
        for r in rows:
            comps = [c for c in (r.components or []) if c.get("score") is not None]
            mass = sum(c["weight"] * c["score"] for c in comps)
            for c in comps:
                e = acc.setdefault(c["key"], {"label": c.get("label") or c["key"],
                                              "xp": 0.0, "score": 0.0, "n": 0})
                if mass > 0:
                    e["xp"] += (r.xp or 0.0) * (c["weight"] * c["score"]) / mass
                e["score"] += c["score"]
                e["n"] += 1
        for e in acc.values():
            e["xp"] = e["xp"] / n if n else 0.0
            e["score"] = e["score"] / e["n"] if e["n"] else None
        return acc

    recent = [r for r in rows_desc[:MOMENTUM_RECENT_DAYS] if r.score is not None]
    base = [r for r in rows_desc[:MOMENTUM_BASE_DAYS] if r.score is not None]
    if len(recent) < MOMENTUM_MIN_RECENT or len(base) < MOMENTUM_MIN_BASE:
        return {"drivers": [], "other": 0.0}

    a, b = per_component(recent), per_component(base)
    out = []
    for key in set(a) | set(b):
        ra, rb = a.get(key), b.get(key)
        delta = ((ra["xp"] if ra else 0.0) - (rb["xp"] if rb else 0.0)) / BASE_XP_PER_DAY
        out.append({
            "key": key,
            "label": (ra or rb)["label"],
            "delta": round(delta, 3),
            "score_recent": round(ra["score"], 3) if ra and ra["score"] is not None else None,
            "score_baseline": round(rb["score"], 3) if rb and rb["score"] is not None else None,
            # Direction the BEHAVIOUR moved. The UI frames on this, not on `delta`:
            # share-based attribution can hand a component a negative delta while its
            # score rose, purely because a sibling rose more (Vitality's resting HR did
            # exactly that). Calling that "holding you back" would be false.
            "score_delta": (round(ra["score"] - rb["score"], 3)
                            if ra and rb and ra["score"] is not None
                            and rb["score"] is not None else None),
        })
    out.sort(key=lambda c: -abs(c["delta"]))
    shown = [c for c in out[:top] if abs(c["delta"]) >= 0.01]
    other = sum(c["delta"] for c in out if c not in shown)
    return {"drivers": shown, "other": round(other, 3)}


def _momentum(rows_desc: list) -> dict:
    """Momentum from a newest-first row list. `rows_desc` must span >= MOMENTUM_BASE_DAYS.

    Only days that actually accrued count (score is not None), matching the xp-rate
    convention: held days (travel/sick) must not read as a slump.
    """
    recent = [r.xp or 0.0 for r in rows_desc[:MOMENTUM_RECENT_DAYS] if r.score is not None]
    base = [r.xp or 0.0 for r in rows_desc[:MOMENTUM_BASE_DAYS] if r.score is not None]
    if len(recent) < MOMENTUM_MIN_RECENT or len(base) < MOMENTUM_MIN_BASE:
        return {"momentum": None, "momentum_band": None, "momentum_dir": 0,
                "xp_rate_14d": None, "xp_rate_90d": None,
                "momentum_drivers": [], "momentum_other": 0.0}

    r14 = sum(recent) / len(recent)
    r90 = sum(base) / len(base)
    m = (r14 - r90) / BASE_XP_PER_DAY

    a = abs(m)
    if a < MOMENTUM_FLAT:
        band = "flat"
    elif a < MOMENTUM_LARGE:
        band = "small"
    else:
        band = "large"
    return {
        "momentum": round(m, 3),
        "momentum_band": band,
        "momentum_dir": 0 if band == "flat" else (1 if m > 0 else -1),
        "xp_rate_14d": round(r14, 2),
        "xp_rate_90d": round(r90, 2),
        **{"momentum_drivers": (_d := _momentum_drivers(rows_desc))["drivers"],
           "momentum_other": _d["other"]},
    }


async def current_stats(db: AsyncSession) -> dict:
    """Latest level/XP per stat, plus recent level-ups, from the stored series."""
    out = []
    level_ups = []
    # Two queries for all six stats (2026-10-21; was two per stat): a UNION ALL of the
    # per-stat "latest 120 rows" lookups — each still an index range on (stat, date) —
    # and one grouped first-date query. Plain column rows rather than ORM objects; every
    # consumer below reads attributes only.
    cols = (GameStatDaily.date, GameStatDaily.stat, GameStatDaily.raw, GameStatDaily.score,
            GameStatDaily.xp, GameStatDaily.cum_xp, GameStatDaily.level, GameStatDaily.components)
    parts = [select(*cols).where(GameStatDaily.stat == st_).order_by(GameStatDaily.date.desc())
             .limit(120).subquery() for st_ in STATS]
    by_stat: dict = {}
    for r in (await db.execute(union_all(*[select(*p.c) for p in parts]))).all():
        by_stat.setdefault(r.stat, []).append(r)
    for rs in by_stat.values():
        rs.sort(key=lambda r: r.date, reverse=True)         # newest first, as before
    first_dates = {st_: d for st_, d in (await db.execute(
        select(GameStatDaily.stat, func.min(GameStatDaily.date))
        .where(GameStatDaily.xp > 0).group_by(GameStatDaily.stat)
    )).all()}
    for stat in STATS:
        rows = by_stat.get(stat, [])
        if not rows:
            continue
        latest = rows[0]
        prog = level_progress(latest.cum_xp or 0.0)
        meta = STAT_META[stat]

        # 7-day XP rate — "how fast am I earning right now". Only days that
        # actually accrued count: a held day (travel/sick) or a day with no data
        # for this stat has score None, and averaging those in as zeros would
        # understate the rate for something the user is doing well at.
        recent = [r.xp or 0.0 for r in rows[:7] if r.score is not None]
        prior = [r.xp or 0.0 for r in rows[7:14] if r.score is not None]
        rate = (sum(recent) / len(recent)) if recent else 0.0
        prior_rate = (sum(prior) / len(prior)) if prior else None

        mom = _momentum(rows)

        # most recent level-up within the loaded window
        for i, (newer, older) in enumerate(zip(rows, rows[1:])):
            if (newer.level or 0) > (older.level or 0):
                level_ups.append({
                    "stat": stat, "label": meta["label"],
                    "level": newer.level, "date": newer.date.isoformat(),
                    "drivers": level_up_drivers(rows, i),
                })
                break

        # Level/XP come from the LATEST row (cum_xp is monotone, always current).
        # Components come from the latest row that actually HAS them: a stat whose
        # inputs haven't synced yet today scores None and stores an empty component
        # list, which would otherwise blank out the whole breakdown and lever every
        # morning until the watch uploads. `components_as_of` lets the UI say so.
        src = next((r for r in rows if r.components), None)
        comps = (src.components if src else []) or []
        ranked = levers(comps)
        out.append({
            "stat": stat, **meta, **prog,
            "score_today": latest.score if latest.score is not None else (src.score if src else None),
            "raw": latest.raw if latest.raw is not None else (src.raw if src else None),
            "components": comps,
            "components_as_of": src.date.isoformat() if src else None,
            # Best levers first: most XP/day still available from each component.
            "levers": ranked,
            "top_lever": ranked[0] if ranked and ranked[0]["xp_available"] > 0 else None,
            "xp_rate_7d": round(rate, 2),
            "xp_rate_prev_7d": round(prior_rate, 2) if prior_rate is not None else None,
            **mom,
            "tracking_since": first_dates[stat].isoformat() if first_dates.get(stat) else None,
        })

    return {"stats": out, "level_ups": sorted(level_ups, key=lambda x: x["date"], reverse=True)[:3],
            "level_curve": {"k": LEVEL_K, "exp": LEVEL_EXP, "max_level": MAX_LEVEL,
                            "base_xp_per_day": BASE_XP_PER_DAY}}


async def stat_history(db: AsyncSession, stat: str, days: int = 365) -> list[dict]:
    tz = ZoneInfo(settings.tz)
    since = local_today() - timedelta(days=days)
    rows = (await db.execute(
        select(GameStatDaily)
        .where(GameStatDaily.stat == stat)
        .where(GameStatDaily.date >= since)
        .order_by(GameStatDaily.date)
    )).scalars().all()
    return [{"date": r.date.isoformat(), "raw": r.raw, "score": r.score,
             "xp": r.xp, "cum_xp": r.cum_xp, "level": r.level} for r in rows]
