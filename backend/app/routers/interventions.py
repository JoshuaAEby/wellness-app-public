"""User-flagged interventions + interrupted-time-series (before/after) analysis."""
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import Intervention

router = APIRouter(prefix="/api/interventions", tags=["interventions"])


CATEGORIES = ("supplement", "training", "diet", "sleep", "other")


class InterventionIn(BaseModel):
    date: date
    label: str = Field(min_length=1, max_length=120)
    notes: str | None = None
    # Optional so an older client neither 422s nor wipes nothing it didn't send: on
    # update, an omitted category keeps the stored one (see update_intervention).
    category: Literal["supplement", "training", "diet", "sleep", "other"] | None = None


def _serialize(iv: Intervention) -> dict:
    return {"id": iv.id, "date": iv.date.isoformat(), "label": iv.label, "notes": iv.notes,
            "category": iv.category}


@router.get("")
async def list_interventions(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)):
    rows = (await db.execute(select(Intervention).order_by(Intervention.date.desc()))).scalars().all()
    return [_serialize(r) for r in rows]


@router.post("")
async def create_intervention(
    body: InterventionIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    iv = Intervention(date=body.date, label=body.label.strip(), notes=(body.notes or None),
                      category=body.category)
    db.add(iv)
    await db.commit()
    await db.refresh(iv)
    return _serialize(iv)


@router.put("/{iv_id}")
async def update_intervention(
    iv_id: int,
    body: InterventionIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    iv = (await db.execute(select(Intervention).where(Intervention.id == iv_id))).scalar_one_or_none()
    if iv is None:
        raise HTTPException(status_code=404, detail="Intervention not found")
    iv.date = body.date
    iv.label = body.label.strip()
    iv.notes = body.notes or None
    if "category" in body.model_fields_set:
        iv.category = body.category
    await db.commit()
    await db.refresh(iv)
    return _serialize(iv)


@router.delete("/{iv_id}")
async def delete_intervention(
    iv_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    await db.execute(delete(Intervention).where(Intervention.id == iv_id))
    await db.commit()
    return {"ok": True}


@router.get("/{iv_id}/analysis")
async def intervention_analysis(
    iv_id: int,
    washout: int = Query(default=7, ge=0, le=60),
    pre_days: int = Query(default=90, ge=28, le=365),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Segmented-regression before/after report for the given intervention."""
    iv = (await db.execute(select(Intervention).where(Intervention.id == iv_id))).scalar_one_or_none()
    if iv is None:
        raise HTTPException(status_code=404, detail="Intervention not found")
    from app.services.trends import analyze_intervention
    results = await analyze_intervention(db, iv.date, washout=washout, pre_days=pre_days)
    return {"intervention": _serialize(iv), "washout": washout, "results": results}
