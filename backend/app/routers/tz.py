"""Time-zone timeline + manual zone periods — phase 1 of tz awareness (services/tz.py).

Read-only inspection plus CRUD for manual periods. Nothing else in the app consumes
these yet; later phases route "today", the scheduler and day bucketing through tz_at.
"""
from datetime import date as Date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import TzPeriod
from app.services import tz as tzsvc

router = APIRouter(prefix="/api/tz", tags=["tz"])


def _zone_label(z) -> str:
    return getattr(z, "key", None) or str(z)


def _serialize(p: TzPeriod) -> dict:
    return {"id": p.id, "start_date": p.start_date.isoformat(),
            "end_date": p.end_date.isoformat(), "tz_name": p.tz_name,
            "source": p.source, "note": p.note}


@router.get("")
async def get_tz(
    days: int = Query(default=30, ge=1, le=400),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Current resolved zone, the observed offset timeline, and manual periods."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    current = await tzsvc.tz_at(db, now)
    periods = (await db.execute(select(TzPeriod).order_by(TzPeriod.start_date.desc()))).scalars().all()
    return {
        "home": tzsvc.home_zone().key,
        "current": _zone_label(current),
        "timeline": await tzsvc.timeline(db, now - timedelta(days=days), now),
        "periods": [_serialize(p) for p in periods],
    }


@router.get("/prompt")
async def get_prompt(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """{needed, reason, offset_min, candidates} — drives the Today banner."""
    return await tzsvc.prompt_state(db)


class ConfirmIn(BaseModel):
    tz_name: str


@router.post("/confirm")
async def confirm(
    body: ConfirmIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Answer to "where are you?" → a manual period through today (see tz.confirm_zone)."""
    try:
        return await tzsvc.confirm_zone(db, body.tz_name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


class PeriodIn(BaseModel):
    start_date: Date
    end_date: Date
    tz_name: str
    note: str | None = None


@router.post("/periods", status_code=201)
async def add_period(
    body: PeriodIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    if body.end_date < body.start_date:
        raise HTTPException(status_code=422, detail="end_date is before start_date")
    try:
        tzsvc.zone_from_name(body.tz_name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    p = TzPeriod(start_date=body.start_date, end_date=body.end_date,
                 tz_name=body.tz_name.strip(), source="manual", note=body.note)
    db.add(p)
    await db.commit()
    await db.refresh(p)
    await tzsvc.refresh_current_zone(db)
    return _serialize(p)


@router.delete("/periods/{period_id}")
async def delete_period(
    period_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    p = await db.get(TzPeriod, period_id)
    if p is None:
        return {"ok": True, "removed": 0}
    await db.delete(p)
    await db.commit()
    await tzsvc.refresh_current_zone(db)
    return {"ok": True, "removed": 1}
