"""Nutrition consistency and the nutrition scorecard.

Part of the /api/metrics package (split out of routers/metrics.py 2026-10-21)."""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import FoodLog, UserSettings
from app.timeutils import local_today
from app.foodday import food_date_sql, food_day_start

router = APIRouter()


@router.get("/nutrition-consistency")
async def nutrition_consistency(
    weeks: int = Query(default=12, ge=4, le=52),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """
    Per-week protein/calorie goal hit-rate — consistency over time, which is more
    actionable than daily totals (a good week is one where you hit the goal most days).
    Returns % of *logged* days per week that hit each goal, plus the logged-day count.
    """
    from collections import defaultdict
    from app.services.energy import effective_calorie_goal, resolve_calorie_goal
    from app.services.nutrition_goals import resolve_protein_goal

    today = local_today()
    start = today - timedelta(weeks=weeks)

    settings = (await db.execute(select(UserSettings).limit(1))).scalar_one_or_none()
    # ONE derivation, shared — see services/nutrition_goals. This was a fourth inline
    # copy of the weight-resolution + multiplier logic.
    protein_goal = await resolve_protein_goal(db, settings)
    # NOT resolve_calorie_goal()["value"] — that is None by design in balance/pace
    # mode, because the goal moves daily with activity. Reading it here silently
    # dropped calorie adherence from this card entirely.
    cal_mode = (await resolve_calorie_goal(db, settings))["mode"] if settings else "fixed"
    cal_goal_by_day: dict = {}
    if settings:
        _d = start
        while _d <= today:
            _g = (await effective_calorie_goal(db, settings, _d))["goal"]
            if _g:
                cal_goal_by_day[_d] = _g
            _d += timedelta(days=1)
    calorie_goal = (await resolve_calorie_goal(db, settings))["value"] if settings else None

    # Fiber goal: explicit override, else 14 g per 1,000 kcal of the calorie GOAL
    # floored at 25 g. Same rule as services/nudges.py and Today's nutrition card —
    # anchored to the goal, not the running intake, so the target does not drift
    # upward as the day's eating accumulates.
    from app.services.nutrition_goals import fiber_goal_for
    fiber_goal = fiber_goal_for(settings)

    rows = (await db.execute(
        select(
            food_date_sql(FoodLog.timestamp).label("d"),
            func.sum(FoodLog.protein_g).label("protein"),
            func.sum(FoodLog.calories).label("calories"),
            func.sum(FoodLog.fiber_g).label("fiber"),
        )
        .where(FoodLog.timestamp >= food_day_start(start))
        .group_by(food_date_sql(FoodLog.timestamp))
    )).all()

    # Exclude nutrition-incomplete days — a partial log understates intake and
    # would show as a spurious "missed the goal", muddying the consistency view.
    from app.services.day_flags import nutrition_excluded_dates
    excluded = await nutrition_excluded_dates(db, start, today)

    weekly = defaultdict(lambda: {"logged": 0, "protein_hit": 0, "calorie_hit": 0, "fiber_hit": 0})
    for r in rows:
        d = date.fromisoformat(str(r.d))
        # TODAY is still being logged, so it is not judged (note 54) — same rule as
        # nutrition_completeness. Counted, it was nearly always a protein miss and a
        # calorie hit, which is what pushed the headline to 0% / 100% mid-week.
        if d in excluded or d >= today:
            continue
        wk = d - timedelta(days=d.weekday())  # Monday of that week
        w = weekly[wk]
        w["logged"] += 1
        if protein_goal and (r.protein or 0) >= protein_goal:
            w["protein_hit"] += 1
        day_cal_goal = cal_goal_by_day.get(d)
        if day_cal_goal and (r.calories or 0) <= day_cal_goal:
            w["calorie_hit"] += 1
        if fiber_goal and (r.fiber or 0) >= fiber_goal:
            w["fiber_hit"] += 1

    this_monday = today - timedelta(days=today.weekday())
    data = []
    for wk in sorted(weekly):
        v = weekly[wk]
        lg = v["logged"]
        data.append({
            "week": wk.isoformat(),
            "label": f"{wk.strftime('%b')} {wk.day}",
            "logged": lg,
            # The current Mon–Sun week is still accruing; the UI leads with the last
            # complete week and draws this one as provisional.
            "in_progress": wk >= this_monday,
            "protein_pct": round(100 * v["protein_hit"] / lg) if lg and protein_goal else None,
            "calorie_pct": round(100 * v["calorie_hit"] / lg) if lg and cal_goal_by_day else None,
            "fiber_pct": round(100 * v["fiber_hit"] / lg) if lg and fiber_goal else None,
        })
    # A dynamic goal has no single number; report the median actually in force so the
    # UI can label the tile rather than print "—".
    _goals = sorted(cal_goal_by_day.values())
    return {"weeks": weeks, "protein_goal": protein_goal,
            "calorie_goal": calorie_goal or (_goals[len(_goals) // 2] if _goals else None),
            "calorie_goal_mode": cal_mode,
            "fiber_goal": fiber_goal, "data": data}


@router.get("/nutrition-scorecard")
async def get_nutrition_scorecard(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Is the diet improving? Recent 4 weeks vs ~3 months earlier, per nutrition trend."""
    from app.services.nutrition_scorecard import build_nutrition_scorecard
    return await build_nutrition_scorecard(db)
