"""Admin endpoints — destructive / reconciliation operations. JWT-authenticated."""
import logging
from datetime import date, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.timeutils import local_day_bounds_utc
from app.models import DailyMetric, Workout

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


class ResyncRequest(BaseModel):
    start_date: date
    end_date: date


@router.post("/resync-workouts")
async def resync_workouts(
    body: ResyncRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """
    Clear all workouts and daily_metrics for a date range, allowing a clean
    re-ingest from Health Auto Export.

    Workflow:
      1. Call this endpoint with the affected date range.
      2. Trigger a manual sync in Health Auto Export.
      3. The webhook ingests the clean data and recomputes daily_metrics for
         dates that receive new workouts.

    Note: dates in the range that had workouts deleted from Apple Health (no
    replacement coming) will remain absent from daily_metrics after the resync.
    The ACWR EWMA seeds from the last available prior date, so the chain
    recovers — but if you want a fully clean recompute across the gap, run the
    historical recompute script after syncing.
    """
    # Local calendar days -> naive-UTC bounds. datetime.combine() on a local date
    # yields the window shifted by the UTC offset: in EDT it dropped workouts from
    # 20:00-23:59 local on the end date and wrongly swept in the same window the day
    # before the start. Half-open [start, end) per local_day_bounds_utc's contract.
    start_dt, _ = local_day_bounds_utc(body.start_date)
    _, end_dt = local_day_bounds_utc(body.end_date)

    # Count before deleting so we can return meaningful numbers
    workout_count = await db.scalar(
        select(func.count()).select_from(Workout)
        .where(Workout.start_time >= start_dt)
        .where(Workout.start_time < end_dt)
    )
    metrics_count = await db.scalar(
        select(func.count()).select_from(DailyMetric)
        .where(DailyMetric.date >= body.start_date)
        .where(DailyMetric.date <= body.end_date)
    )

    await db.execute(
        delete(Workout)
        .where(Workout.start_time >= start_dt)
        .where(Workout.start_time < end_dt)
    )
    await db.execute(
        delete(DailyMetric)
        .where(DailyMetric.date >= body.start_date)
        .where(DailyMetric.date <= body.end_date)
    )
    await db.commit()

    logger.info(
        "resync-workouts: cleared %d workouts and %d daily_metrics rows for %s to %s",
        workout_count, metrics_count, body.start_date, body.end_date,
    )

    return {
        "status": "cleared",
        "dates": f"{body.start_date} to {body.end_date}",
        "workouts_deleted": workout_count,
        "metrics_cleared": metrics_count,
    }
