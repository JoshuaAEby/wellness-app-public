"""Input freshness: which streams are still arriving, and acknowledging the ones
whose silence is real. See services/metric_freshness for the reasoning."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.services import metric_freshness as MF

router = APIRouter(prefix="/api/freshness", tags=["freshness"])


class AckIn(BaseModel):
    note: str | None = None


@router.get("")
async def list_freshness(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Every watched stream, worst state first, with a summary for the header."""
    rows = await MF.compute_freshness(db)
    counts: dict[str, int] = {}
    for f in rows:
        counts[f.state] = counts.get(f.state, 0) + 1
    return {
        "streams": [f.to_dict() for f in rows],
        "counts": counts,
        # What the header badge should show: `never` outranks `stale` because a
        # stream that has produced nothing at all is a configuration failure,
        # not a lapse.
        "needs_attention": counts.get("never", 0) + counts.get("stale", 0),
    }


@router.post("/{metric}/ack")
async def ack_metric(
    metric: str,
    body: AckIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Confirm this silence is expected. Snoozes for one of the metric's own
    expected gaps — it will ask again if the stream is still quiet after that."""
    try:
        return await MF.acknowledge(db, metric, body.note)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.delete("/{metric}/ack")
async def unack_metric(
    metric: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Undo an acknowledgement so the stream can alert again immediately."""
    return await MF.clear_ack(db, metric)


@router.get("/citations")
async def citation_checks(
    days: int = 30,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Summary citation checks (services/citations.py) over the last `days`."""
    import json
    from datetime import datetime, timedelta
    from sqlalchemy import select
    from app.models import CitationCheck
    rows = (await db.execute(
        select(CitationCheck).where(CitationCheck.checked_at >= datetime.utcnow() - timedelta(days=days))
        .order_by(CitationCheck.checked_at.desc())
    )).scalars().all()
    bad_final = [r for r in rows if json.loads(r.bad_final or "[]")]
    return {
        "days": days, "total": len(rows),
        "clean": sum(1 for r in rows if not json.loads(r.bad_first or "[]")),
        "fixed": sum(1 for r in rows if r.retried and not json.loads(r.bad_final or "[]")),
        "unresolved": len(bad_final),
        "errors": sum(1 for r in rows if r.error),
        "recent_problems": [{"kind": r.kind, "period": r.period_key,
                             "bad_first": json.loads(r.bad_first or "[]"),
                             "bad_final": json.loads(r.bad_final or "[]"),
                             "checked_at": r.checked_at.isoformat()}
                            for r in rows if json.loads(r.bad_first or "[]")][:5],
    }


@router.get("/errors")
async def error_ledger_summary(
    days: int = 7,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """ERROR log lines by kind over the last `days` (app/error_ledger.py)."""
    from app import error_ledger
    await error_ledger.flush(db)
    return await error_ledger.summary(db, days)
