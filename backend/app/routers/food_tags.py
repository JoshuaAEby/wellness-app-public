"""Food composition and the coverage tripwire.

Shares the /api/nutrition prefix with food_log.py; kept separate because this is
derived analysis over tags, not CRUD over entries.
"""
import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import FoodItemTag
from app.services import food_tags

router = APIRouter(prefix="/api/nutrition", tags=["nutrition"])
logger = logging.getLogger(__name__)


@router.get("/composition")
async def get_composition(
    days: int = Query(90, ge=7, le=730),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    return await food_tags.composition(db, days)


@router.get("/composition/trend")
async def get_composition_trend(
    weeks: int = Query(16, ge=4, le=104),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    return await food_tags.composition_trend(db, weeks)


@router.get("/coverage")
async def get_coverage(
    recent_days: int = Query(food_tags.RECENT_DAYS, ge=7, le=120),
    baseline_days: int = Query(food_tags.BASELINE_DAYS, ge=30, le=730),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    return await food_tags.coverage(db, recent_days, baseline_days)


@router.get("/tags/status")
async def get_tag_status(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """How much of the food library is classified — the honesty gate for both views."""
    tagged = (await db.execute(select(func.count(FoodItemTag.id)))).scalar() or 0
    pending = await food_tags.untagged_items(db)
    return {
        "tagged_items": tagged,
        "untagged_items": len(pending),
        # Most-frequent-first, so this doubles as "what to tag next".
        "next_untagged": [name for _k, name in pending[:20]],
    }


@router.post("/tags/backfill")
async def post_tag_backfill(
    limit: int = Query(400, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    return await food_tags.run_tagging(db, limit)
