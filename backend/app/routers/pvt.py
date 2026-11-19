"""Psychomotor Vigilance Task — submit a run, read history. JWT-authenticated."""
from datetime import date as Date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models import PvtSession
from app.services import pvt
from app.timeutils import local_today, local_now, current_tz

router = APIRouter(prefix="/api/pvt", tags=["pvt"])


class Trial(BaseModel):
    isi_ms: int                            # response-to-stimulus (v2: feedback inclusive)
    blank_ms: Optional[int] = None         # v2: blank portion of that interval
    rt_ms: Optional[int] = None
    # v2: absolute offsets from test start. These are what make clock corruption
    # detectable structurally (monotonic onsets, rt == t_resp - t_stim) instead of by
    # RT magnitude, which cannot distinguish a broken stopwatch from a microsleep.
    t_stim_ms: Optional[int] = None
    t_resp_ms: Optional[int] = None
    false_start: bool = False
    timed_out: bool = False                # v2: no response before the client timeout


class PvtIn(BaseModel):
    # Client sends raw trials only; every summary metric is derived server-side so
    # scoring stays in one place and can be revised without re-collecting.
    trials: list[Trial] = Field(default_factory=list)
    duration_s: Optional[float] = None
    device: Optional[str] = None
    started_at: Optional[datetime] = None   # offset-aware ISO from the client
    # Collection protocol the client used. Defaults to 1 so an un-upgraded client
    # cannot silently have its data scored under rules it did not collect against.
    protocol: int = 1
    interrupted: bool = False               # tab was backgrounded mid-test


@router.post("")
async def submit_pvt(
    body: PvtIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    tz = current_tz()      # a test just taken happened where the user is now
    start_local = (body.started_at.astimezone(tz) if body.started_at
                   else local_now())
    # Naive UTC to match every other timestamp in this DB (see the local-day bucketing
    # rule — never store offset-aware here).
    started_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)

    trials = [t.model_dump() for t in body.trials]
    metrics = pvt.score(trials, protocol=body.protocol)

    row = PvtSession(
        interrupted=body.interrupted,
        date=start_local.date(),
        started_at=started_utc,
        local_minute=start_local.hour * 60 + start_local.minute,
        duration_s=body.duration_s,
        trials=trials,
        device=body.device,
        **metrics,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _serialize(row)


@router.get("")
async def get_pvt_history(
    days: int = Query(default=90, ge=1, le=730),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Runs over the last N days, newest first, plus a light summary."""
    since = local_today() - timedelta(days=days)
    rows = (await db.execute(
        select(PvtSession).where(PvtSession.date >= since)
        .order_by(PvtSession.started_at.desc())
    )).scalars().all()

    out = [_serialize(r) for r in rows]
    # v2 sessions re-scored on their first 90 s and labelled v3, so the v3 baseline
    # starts with history rather than empty (note 52). The originals stay in `out`;
    # the copies carry `rescored_from` and a string id so they never collide.
    for r in rows:
        if r.protocol == 2 and r.trials:
            import json as _json
            trials = r.trials if isinstance(r.trials, list) else _json.loads(r.trials)
            m = pvt.rescore_first(trials)
            out.append({**_serialize(r), **{k: m.get(k) for k in (
                "n_trials", "median_rt", "mean_rt", "mean_reciprocal_rt", "slowest10_mean",
                "fastest10_mean", "lapses_355", "lapses_500", "false_starts", "perf_score",
                "tot_slope", "slowest10_speed", "timeouts")},
                "id": f"{r.id}-v3", "protocol": 3, "duration_s": 90.0, "rescored_from": 2})
    times = [r.local_minute for r in rows]
    # Protocol mix is reported so the client can refuse to compare across it. v1 and
    # v2 differ in trial count (~37 vs ~64) and therefore in the precision of every
    # metric; a baseline pooled over both would be silently heterogeneous.
    protocols = sorted({r.protocol for r in rows})
    return {
        "sessions": out,
        "n": len(rows),                     # real runs; re-scored copies are not runs
        "n_rescored": len(out) - len(rows),
        "protocols": protocols,
        "current_protocol": pvt.PROTOCOL,
        # Spread of test times — the thing that determines whether circadian effects
        # can be separated from real ones. Tight spread = clean comparisons.
        "time_spread_min": (max(times) - min(times)) if len(times) > 1 else 0,
        "median_time_min": (sorted(times)[len(times) // 2] if times else None),
    }


@router.get("/today")
async def get_today(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    today = local_today()
    row = (await db.execute(
        select(PvtSession).where(PvtSession.date == today)
        .order_by(PvtSession.started_at.desc()).limit(1)
    )).scalars().first()
    return _serialize(row) if row else None


def _serialize(r: PvtSession) -> dict:
    return {
        "id": r.id,
        "date": r.date.isoformat(),
        "local_minute": r.local_minute,
        "local_time": f"{r.local_minute // 60:02d}:{r.local_minute % 60:02d}",
        "circadian_note": pvt.circadian_note(r.local_minute),
        "duration_s": r.duration_s,
        "n_trials": r.n_trials,
        "median_rt": r.median_rt,
        "mean_rt": r.mean_rt,
        "mean_reciprocal_rt": r.mean_reciprocal_rt,
        "slowest10_mean": r.slowest10_mean,
        "fastest10_mean": r.fastest10_mean,
        "lapses_355": r.lapses_355,
        "lapses_500": r.lapses_500,
        "false_starts": r.false_starts,
        "invalid_trials": r.invalid_trials,
        "device": r.device,
        "protocol": r.protocol,
        "timeouts": r.timeouts,
        "timing_suspect": r.timing_suspect,
        "timing_problems": r.timing_problems,
        "perf_score": r.perf_score,
        "tot_slope": r.tot_slope,
        "slowest10_speed": r.slowest10_speed,
        "interrupted": r.interrupted,
    }
