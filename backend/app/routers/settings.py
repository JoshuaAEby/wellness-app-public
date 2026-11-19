from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import settings as app_settings
from app.database import get_db
from app.services.nutrition_goals import fiber_goal_for
from app.models import HealthMetric, SleepSession, UserSettings, Workout

router = APIRouter(prefix="/api/settings", tags=["settings"])


async def _get_or_create_settings(db: AsyncSession) -> UserSettings:
    result = await db.execute(select(UserSettings).limit(1))
    row = result.scalar_one_or_none()
    if row is None:
        row = UserSettings()
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


def _serialize(row: UserSettings) -> dict:
    return {
        "id": row.id,
        "target_sleep_hours": row.target_sleep_hours,
        "max_hr": row.max_hr,
        "resting_hr_override": row.resting_hr_override,
        "training_goal": row.training_goal,
        "steps_goal": row.steps_goal if row.steps_goal is not None else 10000,
        "move_goal": row.move_goal if row.move_goal is not None else 500,
        "exercise_goal": row.exercise_goal if row.exercise_goal is not None else 30,
        "ai_enabled": row.ai_enabled if row.ai_enabled is not None else True,
        "protein_goal_g": row.protein_goal_g,   # None = auto-calculated
        "fiber_goal_g": row.fiber_goal_g,       # None = auto (14 g per 1,000 kcal)
        # The RESOLVED target, so clients never re-derive the rule themselves.
        "fiber_goal_effective": fiber_goal_for(row),
        "calorie_goal": row.calorie_goal,        # None = not set (fixed mode)
        "calorie_goal_mode": "balance" if (row.calorie_goal_mode or "fixed") == "maintain" else (row.calorie_goal_mode or "fixed"),
        "weight_change_goal_lbs_per_week": row.weight_change_goal_lbs_per_week,
        "height_cm": row.height_cm,
        "birth_year": row.birth_year,
        "biological_sex": row.biological_sex,
        # Last 4 chars of the configured widget token so the UI can verify
        # which token is active without exposing the full value.
        "widget_token_hint": (
            f"...{app_settings.widget_token[-4:]}"
            if app_settings.widget_token and app_settings.widget_token != "changeme"
            else None
        ),
        "caffeine_cutoff_hour": row.caffeine_cutoff_hour if row.caffeine_cutoff_hour is not None else 14,
        "light_cutoff_hour":    row.light_cutoff_hour    if row.light_cutoff_hour    is not None else 10,
    }


async def _serialize_full(db: AsyncSession, row: UserSettings) -> dict:
    """Base settings + the resolved calorie goal (TDEE-backed in maintain mode)."""
    from app.services.energy import resolve_calorie_goal
    base = _serialize(row)
    resolved = await resolve_calorie_goal(db, row)
    base["calorie_goal_effective"]   = resolved["value"]
    base["calorie_goal_dynamic"]     = resolved["dynamic"]
    base["calorie_goal_daily_offset"] = resolved["daily_offset"]
    base["calorie_tdee"]             = resolved["tdee"]
    base["calorie_confidence"]       = resolved["confidence"]
    return base


@router.get("")
async def get_settings(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    row = await _get_or_create_settings(db)
    return await _serialize_full(db, row)


class SettingsUpdate(BaseModel):
    target_sleep_hours: Optional[float] = None
    max_hr: Optional[int] = None
    resting_hr_override: Optional[int] = None
    training_goal: Optional[str] = None
    steps_goal: Optional[int] = None
    move_goal: Optional[int] = None
    exercise_goal: Optional[int] = None
    ai_enabled: Optional[bool] = None
    # Nutrition goals — explicitly nullable (None clears back to auto/not-set)
    protein_goal_g: Optional[int] = None
    fiber_goal_g: Optional[int] = None
    calorie_goal: Optional[int] = None
    calorie_goal_mode: Optional[str] = None   # "fixed" | "balance" | "pace"
    weight_change_goal_lbs_per_week: Optional[float] = None
    # Physical profile — conditionally written (None = not provided, don't overwrite)
    height_cm: Optional[float] = None
    birth_year: Optional[int] = None
    biological_sex: Optional[str] = None
    # Habit cutoff times
    caffeine_cutoff_hour: Optional[int] = None
    light_cutoff_hour: Optional[int] = None


@router.put("")
async def update_settings(
    body: SettingsUpdate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    row = await _get_or_create_settings(db)
    if body.target_sleep_hours is not None:
        row.target_sleep_hours = body.target_sleep_hours
    if body.max_hr is not None:
        row.max_hr = body.max_hr
    if body.resting_hr_override is not None:
        row.resting_hr_override = body.resting_hr_override
    if body.training_goal is not None:
        row.training_goal = body.training_goal
    if body.steps_goal is not None:
        row.steps_goal = body.steps_goal
    if body.move_goal is not None:
        row.move_goal = body.move_goal
    if body.exercise_goal is not None:
        row.exercise_goal = body.exercise_goal
    if body.ai_enabled is not None:
        row.ai_enabled = body.ai_enabled
    # Nutrition goals: always write (None = clear back to auto/not-set)
    row.protein_goal_g = body.protein_goal_g
    row.fiber_goal_g = body.fiber_goal_g
    row.calorie_goal = body.calorie_goal
    if body.calorie_goal_mode is not None:
        row.calorie_goal_mode = body.calorie_goal_mode
    row.weight_change_goal_lbs_per_week = body.weight_change_goal_lbs_per_week
    # Profile fields: conditionally write (None = not sent, preserve existing value)
    if body.height_cm is not None:
        row.height_cm = body.height_cm
    if body.birth_year is not None:
        row.birth_year = body.birth_year
    if body.biological_sex is not None:
        row.biological_sex = body.biological_sex
    if body.caffeine_cutoff_hour is not None:
        row.caffeine_cutoff_hour = body.caffeine_cutoff_hour
    if body.light_cutoff_hour is not None:
        row.light_cutoff_hour = body.light_cutoff_hour
    await db.commit()
    await db.refresh(row)
    return await _serialize_full(db, row)


@router.post("/recompute")
async def recompute_all(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Recompute all daily_metrics from scratch. Used after changing settings like sleep target."""
    from app.services.metrics_engine import recompute_dates

    from app.timeutils import local_date_sql

    dates_set: set[date] = set()

    # Dates from health_metrics (local calendar days)
    hm_result = await db.execute(
        select(local_date_sql(HealthMetric.timestamp).label("d")).distinct()
    )
    for row in hm_result:
        d = row[0]
        dates_set.add(d if isinstance(d, date) else date.fromisoformat(str(d)))

    # Dates from workouts (local calendar days)
    wo_result = await db.execute(
        select(local_date_sql(Workout.start_time).label("d")).distinct()
    )
    for row in wo_result:
        d = row[0]
        dates_set.add(d if isinstance(d, date) else date.fromisoformat(str(d)))

    # Dates from sleep sessions
    sl_result = await db.execute(select(SleepSession.date).distinct())
    for row in sl_result:
        dates_set.add(row[0])

    sorted_dates = sorted(dates_set)
    await recompute_dates(db, sorted_dates)

    return {"recomputed": len(sorted_dates)}


@router.get("/ai-status")
async def get_ai_status(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Is AI working, and if not, what should be done about it.

    Reports the last observed outcome; it does NOT make a probe call. A status
    endpoint that spends money every time a page renders would be its own bug, and
    the state is repopulated by the next real parse anyway. The cost is that a
    freshly-restarted container reports "unknown" until something uses AI.
    """
    from app.services import ai_health
    snap = ai_health.snapshot()

    # The user's own switch outranks a stale failure: if AI is off, that is the
    # answer, not whatever error was recorded before it was turned off.
    row = (await db.execute(select(UserSettings).limit(1))).scalar_one_or_none()
    if row is not None and row.ai_enabled is False:
        snap = {**snap, "state": "disabled", "cause": "disabled",
                "label": ai_health.CAUSE_LABELS["disabled"],
                "action": ai_health.CAUSE_ACTIONS["disabled"]}
    return snap
