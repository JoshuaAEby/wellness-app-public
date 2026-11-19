"""Screen-view counting (2026-09-13).

Added before a UI redesign so it is decided on what is actually opened: the logging
data says what gets USED (food 60/60 days, the sleepiness scale 9 days ever), but
nothing recorded which screens are LOOKED AT. One row per tab / sub-tab change; the
client fires and forgets, so a failure here can never affect the app.
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import UiView

router = APIRouter(prefix="/api/telemetry", tags=["telemetry"])


class ViewIn(BaseModel):
    view: str = Field(min_length=1, max_length=64)


@router.post("/view", status_code=204)
async def log_view(
    body: ViewIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    db.add(UiView(view=body.view.strip()[:64], ts=datetime.utcnow()))
    await db.commit()
    return Response(status_code=204)


@router.get("/views")
async def view_counts(
    days: int = Query(default=14, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    since = datetime.utcnow() - timedelta(days=days)
    rows = (await db.execute(
        select(UiView.view, func.count(), func.max(UiView.ts))
        .where(UiView.ts >= since).group_by(UiView.view)
        .order_by(func.count().desc())
    )).all()
    return {"days": days,
            "views": [{"view": v, "count": n, "last": t.isoformat() if t else None}
                      for v, n, t in rows]}
