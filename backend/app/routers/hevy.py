"""Hevy integration — sync controls + derived strength metrics. JWT-authenticated."""
import logging
from collections import defaultdict
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models import HevyExerciseTemplate, HevySet, HevySyncState, HevyWorkout, Workout
from app.services.hevy_sync import run_backfill, run_incremental_sync
from app.services.strength import e1rm_by_exercise, hard_sets_by_day, rep_bests
from app.timeutils import local_date_sql, to_local_date, local_today

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hevy", tags=["hevy"])

# Hypertrophy guideline band shown in the UI (hard sets per muscle group per week)
SETS_BAND_LOW = 10
SETS_BAND_HIGH = 20


@router.get("/status")
async def hevy_status(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)):
    state = (await db.execute(select(HevySyncState).limit(1))).scalars().first()
    n_workouts = await db.scalar(select(func.count()).select_from(HevyWorkout))
    n_linked = await db.scalar(
        select(func.count()).select_from(HevyWorkout).where(HevyWorkout.apple_uuid.isnot(None))
    )
    first = await db.scalar(select(func.min(HevyWorkout.start_time)))
    return {
        "enabled": bool(settings.hevy_api_key),
        "backfill_done": bool(state and state.backfill_done),
        "last_sync_at": state.last_sync_at if state else None,
        "last_error": state.last_error if state else None,
        "workouts": n_workouts or 0,
        "linked_to_apple": n_linked or 0,
        "first_workout": first,
    }


@router.post("/backfill")
async def hevy_backfill(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)):
    """Full import from the Hevy API. Idempotent — safe to re-run."""
    if not settings.hevy_api_key:
        raise HTTPException(status_code=400, detail="HEVY_API_KEY not configured")
    return await run_backfill(db)


@router.post("/sync")
async def hevy_sync_now(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)):
    """Manual incremental sync (same as the hourly scheduler job)."""
    if not settings.hevy_api_key:
        raise HTTPException(status_code=400, detail="HEVY_API_KEY not configured")
    return await run_incremental_sync(db)


@router.get("/strength/daily")
async def strength_daily(
    start: date,
    end: date,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Per-local-day tonnage and working sets, for the Training tab trend chart."""
    day = local_date_sql(HevyWorkout.start_time)
    rows = (await db.execute(
        select(
            day.label("day"),
            func.sum(HevyWorkout.tonnage_kg).label("tonnage_kg"),
            func.sum(HevyWorkout.working_sets).label("working_sets"),
            func.count(HevyWorkout.id).label("sessions"),
        )
        .where(day >= str(start))
        .where(day <= str(end))
        .group_by(day)
        .order_by(day)
    )).all()
    return [
        {
            "date": r.day,
            "tonnage_kg": round(r.tonnage_kg, 1) if r.tonnage_kg is not None else None,
            "working_sets": r.working_sets,
            "sessions": r.sessions,
        }
        for r in rows
    ]


@router.get("/strength/muscle-groups")
async def strength_muscle_groups(
    weeks: int = 8,
    building: bool = False,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Hard sets per muscle group per week (primary = 1.0, secondary = 0.5).

    Weeks start Monday (local dates). Returns newest-first, plus a per-group
    average over the requested window for the summary bars. `building` counts only
    sets that can build muscle (services/strength.counts_as_building).
    """
    today = local_today()
    this_monday = today - timedelta(days=today.weekday())
    window_start = this_monday - timedelta(weeks=weeks - 1)

    # weekly[week_monday][muscle_group] = weighted hard-set count. The per-set rule
    # lives in services/strength.hard_sets_by_day, shared with the coach's pull line.
    daily = await hard_sets_by_day(db, window_start, today, building_only=building)
    weekly: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for d, groups in daily.items():
        monday = d - timedelta(days=d.weekday())
        for g, v in groups.items():
            weekly[monday][g] += v

    all_groups = sorted({g for wk in weekly.values() for g in wk})
    week_list = []
    monday = this_monday
    while monday >= window_start:
        week_list.append({
            "week_start": str(monday),
            "sets": {g: round(weekly.get(monday, {}).get(g, 0.0), 1) for g in all_groups},
        })
        monday -= timedelta(weeks=1)

    # Average over weeks that have any strength work (skip fully-empty weeks so a
    # vacation doesn't drag the average) — but never divide by zero.
    active_weeks = [w for w in week_list if any(v > 0 for v in w["sets"].values())]
    averages = {
        g: round(sum(w["sets"].get(g, 0.0) for w in active_weeks) / len(active_weeks), 1)
        for g in all_groups
    } if active_weeks else {}

    return {
        "weeks": week_list,
        "averages": averages,
        "band": {"low": SETS_BAND_LOW, "high": SETS_BAND_HIGH},
        "active_weeks": len(active_weeks),
    }


@router.get("/strength/e1rm")
async def strength_e1rm(
    days: int = 365,
    top: int = 5,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Per-session best e1RM (Epley, reps ≤ 15) for the N lifts done most recently.

    Computation lives in services/strength.e1rm_by_exercise so the gamification
    layer can reuse it.
    """
    return await e1rm_by_exercise(db, days=days, top=top)


@router.get("/strength/rep-bests")
async def strength_rep_bests(
    days: int = 90,
    top: int = 10,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Rep-range bests (PR tracker): heaviest load for ≥ 8/10/12/15 reps per lift, the
    best as of the window start, and the PRs set inside the window. services/strength."""
    return await rep_bests(db, days=days, top=top)


@router.get("/for-workout/{workout_id}")
async def hevy_for_workout(
    workout_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Hevy set detail for a linked Apple Health workout (WorkoutDetailModal)."""
    workout = (await db.execute(
        select(Workout).where(Workout.id == workout_id)
    )).scalars().first()
    if workout is None:
        raise HTTPException(status_code=404, detail="Workout not found")

    hw = (await db.execute(
        select(HevyWorkout).where(HevyWorkout.apple_uuid == workout.apple_uuid)
    )).scalars().first()
    if hw is None:
        return {"linked": False}

    sets = (await db.execute(
        select(HevySet)
        .where(HevySet.hevy_workout_id == hw.id)
        .order_by(HevySet.exercise_index, HevySet.set_index)
    )).scalars().all()

    exercises: dict[int, dict] = {}
    for s in sets:
        ex = exercises.setdefault(s.exercise_index, {"title": s.exercise_title, "sets": []})
        ex["sets"].append({
            "type": s.set_type,
            "weight_kg": s.weight_kg,
            "reps": s.reps,
            "duration_seconds": s.duration_seconds,
            "distance_meters": s.distance_meters,
            "rpe": s.rpe,
        })

    return {
        "linked": True,
        "hevy_title": hw.title,
        "tonnage_kg": hw.tonnage_kg,
        "working_sets": hw.working_sets,
        "avg_top_set_rpe": hw.avg_top_set_rpe,
        "exercises": [exercises[i] for i in sorted(exercises)],
    }
