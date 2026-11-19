"""Derived nutrition targets — ONE definition, imported everywhere.

The fiber goal was independently reimplemented in three places (services/nudges,
routers/metrics' consistency endpoint, and Today.jsx). Three copies of a rule is three
chances for them to disagree, and a target that differs depending on which card you are
looking at is worse than no target.

The protein goal had the same problem with a worse symptom — see `resolve_protein_goal`.
"""
from __future__ import annotations

from typing import Optional
from app.foodday import food_date_sql

LB_PER_KG = 2.20462

# DGA/IOM: 14 g of fiber per 1,000 kcal.
FIBER_G_PER_1000_KCAL = 14
# Floored, because pure calorie-scaling shrinks the target exactly when cutting — which
# is when fiber matters most for satiety. A 1,500 kcal goal would otherwise ask for 21 g
# and call that a win.
FIBER_FLOOR_G = 25


def fiber_goal_for(settings) -> int:
    """Explicit override, else 14 g per 1,000 kcal of the calorie GOAL, floored at 25.

    Anchored to the calorie GOAL rather than the running intake: anchoring to intake
    would make the target drift upward as the day's eating accumulates.
    """
    if settings is not None and getattr(settings, "fiber_goal_g", None):
        return int(settings.fiber_goal_g)
    basis = (getattr(settings, "calorie_goal", None) if settings is not None else None) or 2000
    return max(FIBER_FLOOR_G, round((basis / 1000) * FIBER_G_PER_1000_KCAL))


# ── Protein ───────────────────────────────────────────────────────────────────
#
# g per POUND of body weight, by training goal. Canonical home; `routers/streaks`
# re-exports this name so its existing importers keep working.
PROTEIN_MULT = {"building": 0.75, "maintaining": 0.65, "cutting": 0.85, "peaking": 0.80}
PROTEIN_MULT_DEFAULT = 0.80


def protein_goal_for(settings, weight_lbs: Optional[float]) -> Optional[int]:
    """Explicit override, else body weight (lb) x the training-goal multiplier.

    Pure function so it can be unit-tested and reused without a DB round trip.
    Returns None when there is neither an override nor a weight — callers must say
    "no goal set" rather than inventing one.
    """
    if settings is not None and getattr(settings, "protein_goal_g", None):
        return int(settings.protein_goal_g)
    if weight_lbs is None:
        return None
    goal = (getattr(settings, "training_goal", None) if settings is not None else None) or "building"
    return round(weight_lbs * PROTEIN_MULT.get(goal, PROTEIN_MULT_DEFAULT))


async def latest_weight_lbs(db) -> Optional[float]:
    """Most recent body weight in POUNDS, from either weight source.

    Unit-aware on purpose. `services/nudges` reads `HealthMetric.value` raw and only
    works because every row in this database happens to be stored in `lb`; a single
    kg reading there would silently yield a 76 g protein goal.
    """
    from sqlalchemy import select

    from app.models import BodyWeightLog, HealthMetric

    manual = (await db.execute(
        select(BodyWeightLog).order_by(BodyWeightLog.timestamp.desc()).limit(1)
    )).scalar_one_or_none()
    if manual is not None and manual.weight_kg is not None:
        return manual.weight_kg * LB_PER_KG

    hm = (await db.execute(
        select(HealthMetric).where(HealthMetric.metric_type == "weight")
        .order_by(HealthMetric.timestamp.desc()).limit(1)
    )).scalar_one_or_none()
    if hm is None or hm.value is None:
        return None
    unit = (hm.unit or "").strip().lower()
    if unit in ("lb", "lbs", "pound", "pounds"):
        return hm.value
    if unit in ("kg", "kgs", "kilogram", "kilograms"):
        return hm.value * LB_PER_KG
    # Health Auto Export has always sent lb here; assume it rather than dropping the
    # goal, but the unit-aware branches above take precedence when a unit is present.
    return hm.value


async def resolve_protein_goal(db, settings) -> Optional[int]:
    """The protein goal as the rest of the app computes it.

    WHY THIS EXISTS (2026-09-07). `user_settings.protein_goal_g` is NULL for this
    user — the goal has always been derived from body weight. Three places in
    `ai_summaries` read that column RAW, got None, and told the model "no goal set".
    With no goal in the data block the model reached for the only protein number in
    its context: the worked example in WEEKLY_SYSTEM_PROMPT (">130g"). Every weekly
    briefing then reported a 130 g "lever" that is neither his goal nor a
    confirmed finding — `finding_ledger` has no confirmed protein pair at all.
    """
    return protein_goal_for(settings, await latest_weight_lbs(db))


# ── Intra-day pacing: "behind your usual by this point" ──────────────────────
#
# NOTE 7 (2026-09-07). The user: *"More useful than a 'short on protein/fiber' would be
# something like 'you are eating 20g less protein than usual at this time today'."*
# The existing nudge restates the gap to goal, which is the same sentence every day and
# says nothing about whether the day is actually going badly.
#
# PROGRESS IS MEASURED BY MEAL SEQUENCE, NOT BY THE CLOCK. 57% of this user's food-log
# timestamps sit at exactly 12:00 — the local-noon default when no time was set — so a
# clock-hour comparison would be against largely synthetic times. Row id, by contrast,
# tracks eating order closely: mean relative position within the day is 0.09 for
# breakfast, 0.41 for lunch, 0.73 for dinner (90-day sample). Snacks and drinks have no
# inherent order and are simply accumulated into whichever stage they precede.
#
# The baseline is the CUMULATIVE total through each stage, so it answers "by the time
# lunch was logged, where am I usually?" — which is the question asked.

PACE_STAGES = ("breakfast", "lunch", "dinner")
PACE_LOOKBACK_DAYS = 90
PACE_MIN_DAYS = 10          # below this the quantiles are just the data points
PACE_MIN_DEFICIT_G = 15     # smaller than this is ordinary day-to-day variation


def _quantile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    return sorted_vals[min(len(sorted_vals) - 1, int(p * len(sorted_vals)))]


async def protein_pace(db, day) -> Optional[dict]:
    """Where today's protein sits against the usual total by this point in the day.

    Returns None when there is no signal to give: nothing logged yet, no core meal
    logged (too early to judge), or too little history.
    """
    from datetime import timedelta

    from sqlalchemy import select

    from app.models import FoodLog
    from app.services.day_flags import nutrition_excluded_dates

    start = day - timedelta(days=PACE_LOOKBACK_DAYS)
    rows = (await db.execute(
        select(food_date_sql(FoodLog.timestamp).label("d"), FoodLog.id,
               FoodLog.meal_type, FoodLog.protein_g)
        .where(food_date_sql(FoodLog.timestamp) >= start.isoformat())
        .where(food_date_sql(FoodLog.timestamp) <= day.isoformat())
        .where((FoodLog.skipped == False) | (FoodLog.skipped.is_(None)))  # noqa: E712
        .order_by(food_date_sql(FoodLog.timestamp), FoodLog.id)
    )).all()
    if not rows:
        return None

    # A partial meal log understates intake, so those days would drag the baseline down
    # and manufacture a "you're ahead" reading. Same exclusion as goal_trajectory_block.
    excluded = {d.isoformat() for d in await nutrition_excluded_dates(db, start, day)}

    by_day: dict = {}
    for r in rows:
        by_day.setdefault(str(r.d), []).append((r.meal_type, r.protein_g or 0.0))

    today_key = day.isoformat()
    today_items = by_day.get(today_key, [])
    if not today_items:
        return None

    def cumulative_at_stages(items):
        """{stage: cumulative protein once that stage first appears}."""
        out, run, seen = {}, 0.0, set()
        for mt, p in items:
            run += p
            if mt in PACE_STAGES and mt not in seen:
                seen.add(mt)
                out[mt] = run
        return out, run

    today_stages, today_total = cumulative_at_stages(today_items)
    # The furthest stage reached today, in meal order — that is "this point in the day".
    stage = next((s for s in reversed(PACE_STAGES) if s in today_stages), None)
    if stage is None:
        return None      # only snacks/drinks so far; too early to call the day

    hist: list[float] = []
    for d_key, items in by_day.items():
        if d_key == today_key or d_key in excluded:
            continue
        stages, _ = cumulative_at_stages(items)
        if stage in stages:
            hist.append(stages[stage])
    if len(hist) < PACE_MIN_DAYS:
        return None

    hist.sort()
    median = _quantile(hist, 0.5)
    # `today_total` rather than `today_stages[stage]`: anything eaten AFTER the stage
    # meal already counts toward today, and comparing a later number against the stage
    # baseline would understate progress.
    delta = today_total - median
    return {
        "stage": stage,
        "today_g": round(today_total),
        "median_g": round(median),
        "p25_g": round(_quantile(hist, 0.25)),
        "p75_g": round(_quantile(hist, 0.75)),
        "delta_g": round(delta),
        "n_days": len(hist),
        # Only call it "behind" when it is both materially and unusually low — a deficit
        # inside the usual spread is just an ordinary day.
        "behind": delta <= -PACE_MIN_DEFICIT_G and today_total < _quantile(hist, 0.25),
    }
