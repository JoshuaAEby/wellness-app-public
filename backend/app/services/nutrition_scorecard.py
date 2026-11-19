"""Nutrition scorecard — is the diet improving? (2026-10-04, after the note-67 scorecard)

The user: "all of the above" — every nutrition trend worth watching, in one card, the same
shape as the fitness scorecard on Progress. Principles agreed in the brainstorm:

  * Score what he controls, on COMPLETE days only (incomplete_data days dropped — a half-
    logged day reads as a low-protein, low-fiber, low-sugar day all at once).
  * MEANS, not medians: "dessert day" and "vegetables today" are yes/no, and the median of
    a yes/no series is just whichever side is more common.
  * Recent = last 4 complete Mon–Sun weeks; reference = the first 4 of the 16-week window
    (~3 months earlier). Verdict = improved / worse only when a 7-day moving-block
    bootstrap 95% interval for the difference excludes zero; else unclear.
  * Total calories are deliberately NOT a row (user: not useful). Instead: average distance
    from break-even, i.e. intake − (RMR + k × active calories) — exactly the model behind
    the `energy_balance` correlation input and the calorie goal. Described, never scored:
    whether a surplus is good depends on the goal.
  * Post-dinner eating starts 2026-08-17: food times before that were a noon placeholder.
"""
from __future__ import annotations

import random
import statistics as st
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FoodLog, HealthMetric, UserSettings
from app.timeutils import local_day_bounds_utc, local_today, local_food_date_sql
from app.foodday import food_day, food_day_end, food_day_start

WEEKS = 16
CMP_WEEKS = 4
BOOTSTRAPS = 400
BLOCK = 7
MIN_DAYS = 8
VEG_GROUPS = ("leafy_green", "orange_red_veg", "other_veg")


def _block_mean(vals: list[float], rng: random.Random) -> float:
    n = len(vals)
    if n <= BLOCK:
        return st.mean(rng.choice(vals) for _ in vals)
    out: list[float] = []
    while len(out) < n:
        i = rng.randrange(0, n - BLOCK + 1)
        out.extend(vals[i:i + BLOCK])
    return st.mean(out[:n])


def _row(key: str, label: str, unit: str, better: Optional[str], daily: dict[date, float],
         weeks: list[date], nd: int = 0, **extra) -> dict:
    """Recent-vs-reference comparison + weekly series for one daily series."""
    ref_end = weeks[CMP_WEEKS - 1] + timedelta(days=6)
    rec_start = weeks[-CMP_WEEKS]
    days = sorted(daily)
    ref = [daily[d] for d in days if weeks[0] <= d <= ref_end]
    rec = [daily[d] for d in days if d >= rec_start]
    rnd = lambda v: None if v is None else (round(v) if nd == 0 else round(v, nd))
    weekly = []
    for w in weeks:
        vals = [daily[d] for d in days if w <= d <= w + timedelta(days=6)]
        weekly.append({"week_of": w.isoformat(), "value": rnd(st.mean(vals)) if vals else None,
                       "days": len(vals)})
    out = {"key": key, "label": label, "unit": unit, "better": better,
           "before": rnd(st.mean(ref)) if ref else None, "now": rnd(st.mean(rec)) if rec else None,
           "n_before": len(ref), "n_now": len(rec), "weekly": weekly, **extra}
    if out["before"] is not None and out["now"] is not None:
        out["change"] = rnd(st.mean(rec) - st.mean(ref))
    if len(ref) < MIN_DAYS or len(rec) < MIN_DAYS:
        out["verdict"] = "insufficient"
        return out
    rng = random.Random(len(ref) * 31 + len(rec) * 17 + len(key))
    diffs = sorted(_block_mean(rec, rng) - _block_mean(ref, rng) for _ in range(BOOTSTRAPS))
    lo, hi = diffs[int(0.025 * BOOTSTRAPS)], diffs[int(0.975 * BOOTSTRAPS) - 1]
    out.update(ci_low=rnd(lo), ci_high=rnd(hi))
    if lo > 0 or hi < 0:
        up = lo > 0
        out["verdict"] = (("up" if up else "down") if better is None
                          else "improved" if up == (better == "up") else "worsened")
    else:
        out["verdict"] = "unclear"
    return out


async def build_nutrition_scorecard(db: AsyncSession) -> dict:
    from app.services.day_flags import nutrition_excluded_dates
    from app.services.energy import current_activity_factor, resolve_rmr
    from app.services.food_tags import GROUPS, _entries, tag_map
    from app.services.nutrition_goals import fiber_goal_for, resolve_protein_goal

    end = local_today() - timedelta(days=1)                  # today is still being logged
    last_monday = end - timedelta(days=end.weekday())
    if end.weekday() != 6:                                   # use COMPLETE weeks only
        last_monday -= timedelta(days=7)
    weeks = [last_monday - timedelta(days=7 * i) for i in range(WEEKS - 1, -1, -1)]
    start, stop = weeks[0], weeks[-1] + timedelta(days=6)

    settings = (await db.execute(select(UserSettings).limit(1))).scalar_one_or_none()
    protein_goal = await resolve_protein_goal(db, settings)
    fiber_goal = fiber_goal_for(settings)
    excluded = await nutrition_excluded_dates(db, start, stop)

    rows = (await db.execute(
        select(FoodLog.timestamp, FoodLog.meal_type, FoodLog.calories, FoodLog.protein_g,
               FoodLog.fiber_g, FoodLog.sugar_g, FoodLog.sodium_mg, FoodLog.skipped,
               FoodLog.time_estimated, FoodLog.source_kind)
        .where(FoodLog.timestamp >= food_day_start(start))
        .where(FoodLog.timestamp < food_day_end(stop))
    )).all()
    by_day: dict[date, list] = defaultdict(list)
    for r in rows:
        by_day[food_day(r.timestamp)].append(r)
    all_days = [start + timedelta(days=i) for i in range((stop - start).days + 1)]
    complete = [d for d in all_days if d in by_day and d not in excluded
                and any(not r.skipped for r in by_day[d])]
    cset = set(complete)

    def daysum(attr):
        return {d: float(sum(getattr(r, attr) or 0 for r in by_day[d] if not r.skipped)) for d in complete}

    protein, fiber, sugar, sodium, kcal = (daysum(a) for a in
                                           ("protein_g", "fiber_g", "sugar_g", "sodium_mg", "calories"))

    # Food groups per day, from the per-ITEM tags (never keyword-over-text).
    tags = await tag_map(db)
    groups_day: dict[date, set] = defaultdict(set)
    for d_iso, k, _kcal in await _entries(db, start, stop):
        d = date.fromisoformat(d_iso)
        t = tags.get(k)
        if d in cset and t:
            groups_day[d].update(g for g in t["groups"] if g in GROUPS)
    variety = {d: float(len(groups_day[d])) for d in complete}
    veg = {d: float(any(g in groups_day[d] for g in VEG_GROUPS)) for d in complete}

    dessert = {d: float(any(r.meal_type == "dessert" and not r.skipped and (r.calories or 0) > 0
                            for r in by_day[d])) for d in complete}

    # Post-dinner eating — the SHARED definition (services/evening_eating), so this row
    # and the after-dinner experiment's compliance can never disagree about an evening.
    from app.services.evening_eating import ate_after_dinner
    post_dinner = {d: float(v) for d, v in (await ate_after_dinner(db, start, stop)).items()
                   if v is not None and d in cset}

    # Alcohol: every day counts (no completeness concept for drinks; unlogged = none).
    lday = local_food_date_sql(HealthMetric.timestamp)      # 04:00 food day
    alc = dict((date.fromisoformat(str(d)), float(v)) for d, v in (await db.execute(
        select(lday, func.sum(HealthMetric.value)).where(HealthMetric.metric_type == "alcohol")
        .where(HealthMetric.timestamp >= local_day_bounds_utc(start)[0])
        .where(HealthMetric.timestamp < local_day_bounds_utc(stop)[1]).group_by(lday))).all())
    alcohol = {d: alc.get(d, 0.0) * 7 for d in all_days}            # drinks/day → /week
    dry = {d: float(alc.get(d, 0.0) <= 0) * 7 for d in all_days}     # dry days per week

    # Distance from break-even, the app's own expenditure model.
    act = dict((date.fromisoformat(str(d)), float(v)) for d, v in (await db.execute(
        select(lday, func.sum(HealthMetric.value)).where(HealthMetric.metric_type == "active_calories")
        .where(HealthMetric.timestamp >= local_day_bounds_utc(start)[0])
        .where(HealthMetric.timestamp < local_day_bounds_utc(stop)[1]).group_by(lday))).all())
    rmr = (await resolve_rmr(db, settings))["rmr"] if settings else None
    k = await current_activity_factor(db)
    balance = ({d: kcal[d] - (rmr + k * act[d]) for d in complete if d in act} if rmr else {})

    completeness = {d: float(d in cset) * 100 for d in all_days}

    pct = lambda series: {d: v * 100 for d, v in series.items()}
    rows_out = [
        _row("protein", "Protein", "g/day", "up", protein, weeks, target=protein_goal,
             now_days_at_target=_share(protein, weeks, lambda v: protein_goal and v >= 0.9 * protein_goal)),
        _row("fiber", "Fiber", "g/day", "up", fiber, weeks, nd=1, target=fiber_goal,
             now_days_at_target=_share(fiber, weeks, lambda v: fiber_goal and v >= fiber_goal)),
        _row("variety", "Food variety", "groups/day", "up", variety, weeks, nd=1,
             note=f"Of {len(GROUPS)} groups: leafy greens, orange/red veg, other veg, fruit, legumes, "
                  "nuts & seeds, whole grains, fish, fermented."),
        _row("veg", "Vegetables", "% of days", "up", pct(veg), weeks),
        _row("sugar", "Sugar", "g/day", "down", sugar, weeks),
        _row("sodium", "Sodium", "mg/day", "down", sodium, weeks, target=2300),
        _row("alcohol", "Alcohol", "drinks/week", "down", alcohol, weeks, nd=1),
        _row("dry", "Alcohol-free days", "days/week", "up", dry, weeks, nd=1),
        _row("dessert", "Dessert days", "% of days", "down", pct(dessert), weeks),
        _row("post_dinner", "Eating after dinner", "% of days", "down", pct(post_dinner), weeks,
             note="A dessert or snack timed after dinner. Counted from Aug 17, when meal times became real."),
        _row("balance", "Vs break-even", "kcal/day", None, balance, weeks,
             note=("Intake minus estimated expenditure (resting rate + activity × your fitted factor "
                   f"k={k:.2f}). Positive = eating above break-even. Not scored — whether a surplus "
                   "is good depends on the goal.")),
        _row("complete", "Logging completeness", "% of days", "up", completeness, weeks,
             note="Days with every main meal logged or skipped. Every other row reads only these days."),
    ]
    return {
        "available": True,
        "reference": {"from": weeks[0].isoformat(),
                      "to": (weeks[CMP_WEEKS - 1] + timedelta(days=6)).isoformat()},
        "recent": {"from": weeks[-CMP_WEEKS].isoformat(), "to": stop.isoformat()},
        "rows": rows_out,
    }


def _share(daily: dict[date, float], weeks: list[date], ok) -> Optional[float]:
    """Days on target per week over the recent window."""
    rec = [v for d, v in daily.items() if d >= weeks[-CMP_WEEKS]]
    return round(7 * sum(1 for v in rec if ok(v)) / len(rec), 1) if rec else None
