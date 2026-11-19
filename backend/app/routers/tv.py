"""TV sessions and who watched them (note 85). See services/tv."""
from datetime import date as Date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import TvSession
from app.services import tv

router = APIRouter(prefix="/api/tv", tags=["tv"])


class AnswerIn(BaseModel):
    watcher: Literal["me", "family", "other"] | None     # null = undo


@router.get("/sessions/pending")
async def pending(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)):
    return await tv.pending(db)


@router.get("/range")
async def by_watcher(start: Date, end: Date, db: AsyncSession = Depends(get_db),
                     _: str = Depends(get_current_user)):
    return await tv.range_by_watcher(db, start, end)


@router.post("/sessions/{session_id}/answer")
async def answer(session_id: int, body: AnswerIn, db: AsyncSession = Depends(get_db),
                 _: str = Depends(get_current_user)):
    row = await db.get(TvSession, session_id)
    if row is None:
        raise HTTPException(404, "No such session")
    row.watcher = body.watcher
    row.mine = None if body.watcher is None else body.watcher == "me"
    row.answered_at = None if body.watcher is None else datetime.utcnow()
    await db.commit()
    # A session after midnight falls before the same date's bedtime or the next one's.
    for d in (row.date, row.date + timedelta(days=1)):
        await tv.refresh_prebed(db, d)
    return tv.serialize(row)
