"""Supplement regimen — what's being taken, and when it started/stopped.

Distinct from `habit_logs.supplements`, which is a single daily boolean meaning "took
today's stack". That boolean can't say what the stack WAS, so the correlation engine's
`habit_supplements` input lumps every compound together and can never attribute an
effect to one of them. This is the composition, over time.

Every start and stop writes an `Intervention` at the same date. That is deliberate and
is most of the value: without a dated record, a supplement started six months ago is
invisible to every chart, so no before/after question about it can even be asked.

A caution that belongs with the feature: an intervention marker supports
interrupted-time-series reading, which is confounded with everything else that changed
at the same date — season, training block, life. Treat markers as hypothesis-generating.
A randomized experiment (kind="supplement") is what actually tests one.
"""
from datetime import date as Date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import Intervention, Supplement
from app.timeutils import local_today

router = APIRouter(prefix="/api/supplements", tags=["supplements"])


class SupplementIn(BaseModel):
    name: str
    dose: Optional[float] = None
    unit: Optional[str] = None
    timing: Optional[str] = None          # am | pm | with_food | as_needed
    started_on: Optional[Date] = None     # defaults to today
    notes: Optional[str] = None
    # Start/stop marks the Trends charts by default. Off for a backfilled historical
    # entry, where a marker at a guessed date would be worse than no marker.
    mark_intervention: bool = True


class StopIn(BaseModel):
    ended_on: Optional[Date] = None
    mark_intervention: bool = True


def _ser(s: Supplement) -> dict:
    return {
        "id": s.id, "name": s.name, "dose": s.dose, "unit": s.unit,
        "timing": s.timing,
        "started_on": s.started_on.isoformat(),
        "ended_on": s.ended_on.isoformat() if s.ended_on else None,
        "active": s.ended_on is None,
        "notes": s.notes,
    }


@router.get("")
async def list_supplements(
    include_ended: bool = True,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Current stack first, then anything discontinued (most recent start first)."""
    q = select(Supplement).order_by(Supplement.started_on.desc())
    rows = (await db.execute(q)).scalars().all()
    if not include_ended:
        rows = [r for r in rows if r.ended_on is None]
    active = [_ser(r) for r in rows if r.ended_on is None]
    ended = [_ser(r) for r in rows if r.ended_on is not None]
    return {"active": active, "ended": ended, "n_active": len(active)}


@router.get("/on/{day}")
async def stack_on(
    day: Date,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """What the regimen was on a given date — the retrospective question the daily
    boolean can't answer."""
    rows = (await db.execute(
        select(Supplement).where(Supplement.started_on <= day)
    )).scalars().all()
    on = [r for r in rows if r.ended_on is None or r.ended_on >= day]
    return {"date": day.isoformat(), "supplements": [_ser(r) for r in on]}


@router.post("")
async def add_supplement(
    body: SupplementIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    started = body.started_on or local_today()
    row = Supplement(
        name=body.name, dose=body.dose, unit=body.unit, timing=body.timing,
        started_on=started, notes=body.notes,
    )
    db.add(row)
    if body.mark_intervention:
        dose = f" {body.dose:g}{body.unit or ''}" if body.dose else ""
        db.add(Intervention(date=started, label=f"Started {body.name}{dose}",
                            notes="Auto-logged from the supplement stack.",
                            category="supplement"))
    await db.commit()
    await db.refresh(row)
    return _ser(row)


@router.post("/{supplement_id}/stop")
async def stop_supplement(
    supplement_id: int,
    body: StopIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Discontinue. The row is KEPT with an end date — deleting it would silently
    rewrite what the regimen was on every past day."""
    row = (await db.execute(
        select(Supplement).where(Supplement.id == supplement_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="supplement not found")
    if row.ended_on is not None:
        return _ser(row)
    ended = body.ended_on or local_today()
    if ended < row.started_on:
        raise HTTPException(status_code=400, detail="end date precedes the start date")
    row.ended_on = ended
    if body.mark_intervention:
        db.add(Intervention(date=ended, label=f"Stopped {row.name}",
                            notes="Auto-logged from the supplement stack.",
                            category="supplement"))
    await db.commit()
    await db.refresh(row)
    return _ser(row)


@router.delete("/{supplement_id}")
async def delete_supplement(
    supplement_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Only for entries added in error. To discontinue something you actually took, use
    /stop — that preserves the history this table exists to hold."""
    row = (await db.execute(
        select(Supplement).where(Supplement.id == supplement_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="supplement not found")
    await db.delete(row)
    await db.commit()
    return {"deleted": supplement_id}
