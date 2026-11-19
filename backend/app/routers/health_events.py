"""Health events log (note 93): vaccines, procedures, medication changes."""
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import HealthEvent
from app.services.health_events import SICK_OFFSETS, event_response, sick_episodes

router = APIRouter(prefix="/api/health-events", tags=["health-events"])


class HealthEventIn(BaseModel):
    date: date
    kind: Literal["vaccine", "procedure", "medication", "other"] = "vaccine"
    name: str = Field(min_length=1, max_length=120)
    notes: str | None = None


def _serialize(e: HealthEvent) -> dict:
    return {"id": e.id, "date": e.date.isoformat(), "kind": e.kind, "name": e.name,
            "notes": e.notes}


@router.get("")
async def list_events(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)):
    rows = (await db.execute(
        select(HealthEvent).order_by(HealthEvent.date.desc(), HealthEvent.id.desc())
    )).scalars().all()
    return [_serialize(e) for e in rows]


@router.post("")
async def create_event(body: HealthEventIn, db: AsyncSession = Depends(get_db),
                       _: str = Depends(get_current_user)):
    e = HealthEvent(date=body.date, kind=body.kind, name=body.name.strip(),
                    notes=(body.notes or "").strip() or None)
    db.add(e)
    await db.commit()
    await db.refresh(e)
    return _serialize(e)


@router.get("/sick-episodes")
async def list_sick_episodes(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)):
    return await sick_episodes(db)


@router.get("/sick-episodes/{start}/response")
async def sick_response(start: date, db: AsyncSession = Depends(get_db),
                        _: str = Depends(get_current_user)):
    if not any(e["start"] == start.isoformat() for e in await sick_episodes(db)):
        raise HTTPException(404, "No sick episode starts on that date")
    return await event_response(db, start, SICK_OFFSETS)


async def _get(db: AsyncSession, event_id: int) -> HealthEvent:
    e = await db.get(HealthEvent, event_id)
    if e is None:
        raise HTTPException(404, "No such event")
    return e


@router.get("/{event_id}/response")
async def get_response(event_id: int, db: AsyncSession = Depends(get_db),
                       _: str = Depends(get_current_user)):
    return await event_response(db, (await _get(db, event_id)).date)


@router.delete("/{event_id}", status_code=204)
async def delete_event(event_id: int, db: AsyncSession = Depends(get_db),
                       _: str = Depends(get_current_user)):
    await db.delete(await _get(db, event_id))
    await db.commit()
