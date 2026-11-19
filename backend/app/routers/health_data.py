import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import bearer_matches, get_current_user
from app.config import settings
from app.database import get_db
from app.models import UserSettings
from app.services.ingestion import ingest_health_export
from app.timeutils import local_today, local_now

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("")
async def health_check():
    """Unauthenticated health check for Docker."""
    return {"status": "ok"}


@router.post("/webhook")
async def receive_webhook(
    request: Request,
    authorization: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
):
    """Health Auto Export webhook endpoint. Authenticated via WEBHOOK_SECRET Bearer token."""
    if not bearer_matches(authorization, settings.webhook_secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook secret")

    body: Any = await request.json()
    logger.debug("Webhook payload keys: %s", list(body.keys()) if isinstance(body, dict) else type(body).__name__)

    # Health Auto Export sends {"data": {"metrics": [...], "workouts": [...]}}
    metric_list = []
    workout_list = []
    if isinstance(body, dict) and "data" in body:
        data = body["data"]
        if isinstance(data, dict):
            metric_list = data.get("metrics", [])
            workout_list = data.get("workouts", [])
        elif isinstance(data, list):
            metric_list = data
    elif isinstance(body, list):
        metric_list = body
    else:
        logger.warning("Unrecognised webhook payload shape: %s", str(body)[:200])

    records_processed = await ingest_health_export(db, metric_list, workout_list)

    # Time-zone timeline (phase 1 of tz awareness): one observation per webhook of the
    # offset the phone's clock was on. Fail-safe; nothing reads it yet. services/tz.py.
    from app.services.tz import record_observation, refresh_current_zone
    if await record_observation(db, metric_list, workout_list):
        # Phase 2: a new observation may move "today" and the scheduler (services/tz).
        await refresh_current_zone(db)

    # Stamp last_webhook_at whenever this call delivers actual data
    if records_processed > 0:
        result = await db.execute(select(UserSettings).limit(1))
        user_settings = result.scalar_one_or_none()
        if user_settings:
            user_settings.last_webhook_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await db.commit()

        # Event-driven morning pipeline: the first morning webhook that has
        # delivered today's finalized sleep session marks "user is awake and
        # data is fresh" — fire recompute → coach confirm → summary → illness
        # check right now instead of waiting for a fixed clock time. Runs as a
        # background task so the webhook response stays fast; the pipeline's
        # own once-per-day claim dedupes overlapping triggers.
        try:
            from zoneinfo import ZoneInfo
            from app.models import SleepSession
            from app.timeutils import local_today

            now_local = local_now()
            today = local_today()
            already_ran = (user_settings is not None
                           and user_settings.last_morning_pipeline_date == today)
            # Until 13:00, not 12:00: a 12:24 wake (9/24) synced too late to trigger,
            # leaving that day to a fallback (note 57).
            if 4 <= now_local.hour < 13 and not already_ran:
                has_sleep = (await db.execute(
                    select(SleepSession.date).where(SleepSession.date == today)
                )).scalar_one_or_none()
                if has_sleep is not None:
                    import asyncio
                    from app.services.morning import run_morning_pipeline
                    asyncio.create_task(run_morning_pipeline(trigger="webhook"))
        except Exception as exc:  # trigger failure must never break ingestion
            logger.error("Morning-pipeline trigger check failed: %s", exc)

    return {"status": "ok", "records_processed": records_processed}


@router.get("/raw")
async def get_raw_metrics(
    metric_type: str,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Return recent raw health_metrics rows for a given metric_type."""
    from sqlalchemy import select
    from app.models import HealthMetric

    result = await db.execute(
        select(HealthMetric)
        .where(HealthMetric.metric_type == metric_type)
        .order_by(HealthMetric.timestamp.desc())
        .limit(limit)
    )
    rows = result.scalars().all()
    return [
        {
            "id": r.id,
            "metric_type": r.metric_type,
            "timestamp": r.timestamp.isoformat() if r.timestamp else None,
            "value": r.value,
            "unit": r.unit,
            "source": r.source,
        }
        for r in rows
    ]
