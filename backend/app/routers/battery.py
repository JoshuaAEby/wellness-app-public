"""Daily cognitive battery — KSS and finger tapping. JWT-authenticated.

PVT lives in routers/pvt.py: it predates the battery, has far richer per-trial data, and
is already deployed. These two share a router because they are lightweight and are always
taken together with it.

Administration order is FIXED (PVT → KSS → tapping) and must not be randomised.
Randomising is correct for between-subjects work and wrong for a longitudinal n-of-1: it
injects variance. A fixed order turns "fatigue from the earlier tests" into a constant
that cancels out of every day-to-day comparison.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models import KssLog, TappingSession
from app.services import tapping as tap_svc
from app.timeutils import local_today, local_now, current_tz

router = APIRouter(prefix="/api/battery", tags=["battery"])

TAPPING_PROTOCOL = 1


def _local_now(started_at: Optional[datetime]):
    tz = current_tz()      # a test just taken happened where the user is now
    start_local = started_at.astimezone(tz) if started_at else local_now()
    # Naive UTC to match every other timestamp in this DB (see the local-day bucketing
    # rule — never store offset-aware here).
    return start_local, start_local.astimezone(timezone.utc).replace(tzinfo=None)


# ── KSS ──────────────────────────────────────────────────────────────────────────
class KssIn(BaseModel):
    score: int = Field(ge=1, le=9)
    device: Optional[str] = None
    started_at: Optional[datetime] = None


@router.post("/kss")
async def submit_kss(
    body: KssIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    start_local, started_utc = _local_now(body.started_at)
    row = KssLog(
        date=start_local.date(),
        started_at=started_utc,
        local_minute=start_local.hour * 60 + start_local.minute,
        score=body.score,
        device=body.device,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _kss(row)


@router.get("/kss")
async def kss_history(
    days: int = Query(default=90, ge=1, le=730),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    since = local_today() - timedelta(days=days)
    rows = (await db.execute(
        select(KssLog).where(KssLog.date >= since).order_by(KssLog.started_at.desc())
    )).scalars().all()
    return {"sessions": [_kss(r) for r in rows], "n": len(rows)}


@router.get("/kss/today")
async def kss_today(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    today = local_today()
    row = (await db.execute(
        select(KssLog).where(KssLog.date == today)
        .order_by(KssLog.started_at.desc()).limit(1)
    )).scalars().first()
    return _kss(row) if row else None


# ── Tapping ──────────────────────────────────────────────────────────────────────
class TappingIn(BaseModel):
    # Raw tap offsets only; every metric is derived server-side so scoring stays in one
    # place and can be revised without re-collecting.
    taps: list[int] = Field(default_factory=list)   # ms offsets from block start
    duration_ms: int
    hand: Optional[str] = "dominant"
    device: Optional[str] = None
    started_at: Optional[datetime] = None
    interrupted: bool = False


@router.post("/tapping")
async def submit_tapping(
    body: TappingIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    start_local, started_utc = _local_now(body.started_at)
    metrics = tap_svc.score(body.taps, body.duration_ms)
    row = TappingSession(
        date=start_local.date(),
        started_at=started_utc,
        local_minute=start_local.hour * 60 + start_local.minute,
        protocol=TAPPING_PROTOCOL,
        hand=body.hand,
        taps=body.taps,
        device=body.device,
        interrupted=body.interrupted,
        **metrics,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _tap(row)


@router.get("/tapping")
async def tapping_history(
    days: int = Query(default=90, ge=1, le=730),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    since = local_today() - timedelta(days=days)
    rows = (await db.execute(
        select(TappingSession).where(TappingSession.date >= since)
        .order_by(TappingSession.started_at.desc())
    )).scalars().all()
    return {"sessions": [_tap(r) for r in rows], "n": len(rows),
            "current_protocol": TAPPING_PROTOCOL}


@router.get("/tapping/today")
async def tapping_today(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    today = local_today()
    row = (await db.execute(
        select(TappingSession).where(TappingSession.date == today)
        .order_by(TappingSession.started_at.desc()).limit(1)
    )).scalars().first()
    return _tap(row) if row else None


def _kss(r: KssLog) -> dict:
    return {
        "id": r.id,
        "date": r.date.isoformat(),
        "local_minute": r.local_minute,
        "local_time": f"{r.local_minute // 60:02d}:{r.local_minute % 60:02d}",
        "score": r.score,
        "label": tap_svc.kss_label(r.score),
        "device": r.device,
    }


def _tap(r: TappingSession) -> dict:
    return {
        "id": r.id,
        "date": r.date.isoformat(),
        "local_minute": r.local_minute,
        "local_time": f"{r.local_minute // 60:02d}:{r.local_minute % 60:02d}",
        "protocol": r.protocol,
        "duration_ms": r.duration_ms,
        "hand": r.hand,
        "n_taps": r.n_taps,
        "tap_rate": r.tap_rate,
        "mean_iti": r.mean_iti,
        "iti_sd": r.iti_sd,
        "iti_cv": r.iti_cv,
        "first_half_rate": r.first_half_rate,
        "second_half_rate": r.second_half_rate,
        "rate_decrement_pct": r.rate_decrement_pct,
        "artifacts": r.artifacts,
        "interrupted": r.interrupted,
        "device": r.device,
    }
