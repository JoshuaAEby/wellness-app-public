"""Caffeine instance-logging API."""
from datetime import date as Date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.services import caffeine as caf

router = APIRouter(prefix="/api/caffeine", tags=["caffeine"])


@router.get("/presets")
async def get_presets(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    return {"presets": await caf.effective_presets(db), "defaults": caf.PRESETS}


class PresetsIn(BaseModel):
    presets: list | None = None   # None resets to defaults


@router.put("/presets")
async def put_presets(
    body: PresetsIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    # Light validation: each preset needs id, label, mg.
    if body.presets is not None:
        for p in body.presets:
            if not all(k in p for k in ("id", "label", "mg")):
                raise HTTPException(status_code=422, detail="each preset needs id, label, mg")
    return {"presets": await caf.save_presets(db, body.presets)}


@router.get("/day")
async def get_day(
    date: Date = Query(default_factory=Date.today),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    return await caf.day_summary(db, date)


@router.get("/range")
async def get_range(
    start: Date = Query(...),
    end: Date = Query(...),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Daily caffeine totals over a range — the series the Habits behaviour card charts.

    Caffeine lives in its own `caffeine_log` table rather than `health_metrics`, so the
    generic /raw-trend endpoint the alcohol card uses cannot reach it. Same
    output shape ({date, value}) so the frontend can share the charting code.

    Days with no logged caffeine are simply absent; the caller fills the range. That is
    deliberate — an absent day means "not logged", which is not the same as a genuine
    zero, and this endpoint should not invent the difference.
    """
    from sqlalchemy import func, select

    from app.models import CaffeineLog
    from app.timeutils import local_date_sql, local_day_bounds_utc

    day = local_date_sql(CaffeineLog.timestamp)
    rows = (await db.execute(
        # `mg` is ALREADY the entry total (preset dose x quantity, see models.CaffeineLog),
        # so do NOT multiply by quantity here — day_summary() sums raw mg for the same
        # reason. Multiplying would double-count every multi-serving entry.
        select(day.label("date"),
               func.sum(CaffeineLog.mg).label("mg"),
               func.count().label("n"))
        .where(CaffeineLog.timestamp >= local_day_bounds_utc(start)[0])
        .where(CaffeineLog.timestamp < local_day_bounds_utc(end)[1])
        .group_by(day)
        .order_by(day)
    )).all()
    return [{"date": str(r.date), "value": round(r.mg or 0.0, 1), "drinks": r.n} for r in rows]


class InstanceIn(BaseModel):
    date: Date
    kind: str
    kcal: float | None = None        # overrides the preset's calories
    mg: float | None = None          # defaults to the preset dose × quantity
    quantity: float = 1.0
    timestamp: datetime | None = None  # naive UTC; default now / noon-on-day


@router.post("/logs", status_code=201)
async def add_log(
    body: InstanceIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    presets = await caf.effective_presets(db)
    preset = next((p for p in presets if p["id"] == body.kind), None)
    if body.mg is not None:
        mg = body.mg
    else:
        if preset is None:
            raise HTTPException(status_code=422, detail=f"unknown preset {body.kind}; supply mg")
        mg = preset["mg"] * body.quantity
    row = await caf.add_instance(db, body.date, body.kind, mg, body.quantity, body.timestamp)

    # Milk drinks carry real calories; black coffee carries none and creates no
    # nutrition row (kcal 0 -> sync is a no-op). One tap, both records.
    from app.services import intake
    kcal = body.kcal if body.kcal is not None else (preset or {}).get("kcal")
    kcal = float(kcal) * float(body.quantity or 1.0) if kcal else None
    await intake.sync_nutrition_entry(
        db, source_kind="caffeine", source_id=row.id,
        label=(preset or {}).get("label") or "Caffeinated drink",
        kcal=kcal, ts_utc=row.timestamp,
        macros=intake.scale_macros((preset or {}).get("macros"), body.quantity or 1.0))
    return caf._serialize(row)


class InstancePatch(BaseModel):
    timestamp: datetime | None = None   # naive UTC
    mg: float | None = None
    quantity: float | None = None


@router.patch("/logs/{instance_id}")
async def patch_log(
    instance_id: int,
    body: InstancePatch,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Edit a logged dose (mainly its time — see services/caffeine.update_instance)."""
    row = await caf.update_instance(db, instance_id, at=body.timestamp,
                                    mg=body.mg, quantity=body.quantity)
    if row is None:
        raise HTTPException(status_code=404, detail="instance not found")
    return caf._serialize(row)


@router.delete("/logs/{instance_id}")
async def delete_log(
    instance_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    day = await caf.delete_instance(db, instance_id)
    if day is None:
        raise HTTPException(status_code=404, detail="not found")
    # Remove the paired nutrition row, if this drink created one.
    from app.services import intake
    await intake.unsync_nutrition_entry(db, "caffeine", instance_id)
    return {"ok": True}


class AckIn(BaseModel):
    date: Date
    ack: str   # 'none' | 'done'


@router.post("/ack", status_code=201)
async def set_ack(
    body: AckIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    if body.ack not in ("none", "done"):
        raise HTTPException(status_code=422, detail="ack must be 'none' or 'done'")
    await caf.set_ack(db, body.date, body.ack)
    return {"ok": True, "date": body.date.isoformat(), "ack": body.ack}


@router.delete("/ack")
async def clear_ack(
    date: Date = Query(...),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    await caf.clear_ack(db, date)
    return {"ok": True}


@router.get("/incomplete")
async def list_incomplete(
    days: int = Query(default=7, ge=1, le=60),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    return {"incomplete": [d.isoformat() for d in await caf.recent_incomplete(db, days)]}
