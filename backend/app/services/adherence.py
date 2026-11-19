"""Rolling habit adherence % + the variance monitor.

Ported to the backend 2026-08-03 from `frontend/src/tabs/Habits.jsx` (`AdherenceCard`),
which was the only implementation. It lived client-side, so nothing server-side could
reuse it — the gamification layer needs it, and two copies would drift.

Semantics are preserved exactly, including the split denominator decided with the user:
manual habits count over CALENDAR days (an unlogged day is a miss); auto-derived habits
count over JUDGEABLE days (null = unknowable, excluded). A habit stuck ≥95% or ≤5% over
a solid denominator carries ~no information — the ate_late lesson, automated.
"""
import math
from datetime import date as Date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.habit_metrics import build_calendar

# Daily yes/no habits where a rolling adherence % is meaningful (excludes
# frequency events like workouts/walks).
#
# `manual` mirrors the frontend HABITS table: True = the user toggles it (so an
# unlogged day is a miss), False = derived from Apple Health data (so an unjudgeable
# day is excluded rather than counted against you).
#
# `no_late_caffeine` became DERIVED on 2026-08-19 (latest logged dose vs the cutoff),
# so it is no longer manual. Reclassified 2026-09-07 — and verified to be a NUMERICAL
# NO-OP: `build_calendar` never returns None for this key (it falls back to the
# historical default True), so calendar days and judgeable days are the same set.
# 30-day adherence reads 70% (21/30) either way.
#
# `meditation` ALSO became derived on 2026-08-19, but it stays `manual: True`
# DELIBERATELY. It returns None whenever Apple has no mindful-minutes row, and the data
# is genuinely sparse — 9 sessions in the entire history, none since 2026-08-22, so 27
# of the last 30 days are None. Reclassifying it would move 30-day adherence from
# 7% (2/30) to 67% (2/3): a friendlier number computed over a denominator of three,
# below MIN_DEN_FOR_FLAG, and false. Ingestion was checked and is healthy — the sits
# are not happening. "Absence of a recorded session" is the honest reading here, the
# same rule worked_out and went_for_walk already use.
ADHERENCE_HABITS = [
    {"key": "no_late_caffeine",    "label": "No late caffeine",    "emoji": "☕",  "color": "#f59e0b", "manual": False},
    {"key": "outdoor_light_am",    "label": "Outdoor light",       "emoji": "☀️", "color": "#38bdf8", "manual": False},
    {"key": "supplements",         "label": "Supplements",         "emoji": "💊", "color": "#a78bfa", "manual": True},
    {"key": "creatine",            "label": "Creatine",            "emoji": "🧪", "color": "#fb923c", "manual": True},
    {"key": "meditation",          "label": "Meditation",          "emoji": "🧘", "color": "#10b981", "manual": True},
    {"key": "slept_well",          "label": "Slept well",          "emoji": "😴", "color": "#818cf8", "manual": False},
    {"key": "consistent_bedtime",  "label": "Consistent bedtime",  "emoji": "⏰", "color": "#818cf8", "manual": False},
    {"key": "consistent_waketime", "label": "Consistent wake time", "emoji": "⏰", "color": "#818cf8", "manual": False},
    {"key": "no_alcohol",          "label": "No alcohol",          "emoji": "🍵", "color": "#34d399", "manual": False},
    # no_xp: Momentum weights read this list, and fluids were never part of Discipline.
    {"key": "fluids_goal",         "label": "Fluids goal",         "emoji": "💧", "color": "#22d3ee", "manual": False, "no_xp": True},
]

DEFAULT_DAYS = 30

# The variance flag needs a solid denominator to be trustworthy.
MIN_DEN_FOR_FLAG = 20
AUTOMATIC_PCT = 95   # nearly always done → low information, consider retiring
RARE_PCT = 5         # almost never done → aspirational/failing, redesign or drop


def _round_half_up(x: float) -> int:
    """JS Math.round semantics (half away from zero for positives).

    Python's round() is banker's rounding, which would disagree with the frontend
    on exact .5 cases (e.g. 15/30 → 50 either way, but 0.5-boundary percentages
    like 29/40 would differ). Percentages here are always >= 0.
    """
    return math.floor(x + 0.5)


def adherence_from_calendar(days: list[dict]) -> list[dict]:
    """Compute per-habit adherence rows from a habit-calendar payload.

    Pure function over `habit_metrics.build_calendar()` output so it can be reused
    for any window without another DB round trip.
    """
    rows = []
    for habit in ADHERENCE_HABITS:
        key = habit["key"]
        num = den = 0
        for d in days:
            v = d.get(key)
            if habit["manual"]:                 # calendar days: unlogged = miss
                den += 1
                if v is True:
                    num += 1
            elif v is True or v is False:       # judgeable days only
                den += 1
                if v is True:
                    num += 1

        if den == 0:
            continue

        pct = _round_half_up(num / den * 100)
        flag = None
        if den >= MIN_DEN_FOR_FLAG:
            if pct >= AUTOMATIC_PCT:
                flag = "automatic"
            elif pct <= RARE_PCT:
                flag = "rare"

        rows.append({
            "key":    key,
            "label":  habit["label"],
            "emoji":  habit["emoji"],
            "color":  habit["color"],
            "manual": habit["manual"],
            "pct":    pct,
            "num":    num,
            "den":    den,
            "flag":   flag,
        })
    return rows


async def compute_adherence(
    db: AsyncSession,
    end: Date,
    days: int = DEFAULT_DAYS,
) -> dict:
    """Rolling adherence over the `days`-day window ending on `end` (inclusive)."""
    start = end - timedelta(days=days - 1)
    calendar = await build_calendar(db, start, end)
    return {
        "start": start.isoformat(),
        "end":   end.isoformat(),
        "days":  days,
        "habits": adherence_from_calendar(calendar),
    }
