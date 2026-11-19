"""Gamification — RPG character sheet. Read-only mirror; see docs/GAMIFICATION.md."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.services import game

router = APIRouter(prefix="/api/game", tags=["game"])


@router.get("")
async def get_game(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Character sheet: six stats with level/XP/progress, plus today's condition."""
    stats = await game.current_stats(db)
    cond = await game.condition(db)
    return {**stats, "condition": cond}


@router.get("/history/{stat}")
async def get_stat_history(
    stat: str,
    days: int = Query(default=365, ge=7, le=1095),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    if stat not in game.STATS:
        raise HTTPException(status_code=404, detail=f"Unknown stat: {stat}")
    return await game.stat_history(db, stat, days)


@router.post("/recompute")
async def recompute(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Force a full recompute (the morning pipeline does this daily)."""
    return await game.recompute_and_store(db)
