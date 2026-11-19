"""Screen-Time ingestion endpoint.

The Mac-side collector (scripts/screentime_collector.py) POSTs batches of
app-focus segments here on a timer (launchd, every 30 min). Authenticated with
the static WIDGET_TOKEN (Bearer) — same token the read-only widget endpoint
uses — so the launchd job doesn't have to manage JWT expiry.

Records arrive with offset-aware ISO timestamps; we convert to **naive UTC**
before storing to match the rest of the DB (so `local_date_sql` day-bucketing
works). Upsert is idempotent on (app, start_time, device): re-POSTing the same
window inserts nothing and is reported as "skipped".
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import bearer_matches
from app.config import settings
from app.database import get_db
from app.models import ScreentimeEvent
from app.screentime_categories import categorize_app

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/screentime", tags=["screentime"])


class ScreentimeRecord(BaseModel):
    app: str
    duration_sec: int = Field(ge=0)
    start_time: datetime
    end_time: datetime | None = None
    device: str


def _verify_widget_token(authorization: str = Header(default="")) -> None:
    """Static-token auth, mirroring routers/widgets.py semantics."""
    if not settings.widget_token or settings.widget_token == "changeme":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WIDGET_TOKEN not configured on server",
        )
    if not bearer_matches(authorization, settings.widget_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid widget token",
        )


def _to_naive_utc(dt: datetime) -> datetime:
    """Offset-aware → naive UTC. Naive input is assumed already-UTC."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


@router.post("/ingest")
async def ingest_screentime(
    records: list[ScreentimeRecord],
    _: None = Depends(_verify_widget_token),
    db: AsyncSession = Depends(get_db),
):
    """Batch-upsert screen-time records. Returns {"inserted": N, "skipped": N}."""
    inserted = 0
    skipped = 0

    for rec in records:
        start_utc = _to_naive_utc(rec.start_time)
        end_utc = _to_naive_utc(rec.end_time) if rec.end_time else None
        stmt = (
            sqlite_insert(ScreentimeEvent)
            .values(
                app=rec.app,
                duration_sec=rec.duration_sec,
                start_time=start_utc,
                end_time=end_utc,
                device=rec.device,
                category=categorize_app(rec.app),
                created_at=datetime.utcnow(),
            )
            # Idempotent: an identical (app, start_time, device) already stored
            # is a no-op — counted as skipped, not an error.
            .on_conflict_do_nothing(index_elements=["app", "start_time", "device"])
        )
        result = await db.execute(stmt)
        if result.rowcount and result.rowcount > 0:
            inserted += 1
        else:
            skipped += 1

    await db.commit()
    logger.info("Screentime ingest: %d inserted, %d skipped", inserted, skipped)
    return {"inserted": inserted, "skipped": skipped}


# ── Collector self-report (note 79, 2026-10-26) ─────────────────────────────────────
#
# The staleness alert could only say "stalled — check Share Across Devices and the
# collector". On 2026-10-22 a macOS update revoked Full Disk Access and the Mac
# collector ran every 30 min for days reading nothing; the cause sat in a log file on
# the Mac. Now every run reports each source's problem here, and:
#   - each (source, code) is logged at ERROR with the code IN THE TEMPLATE, so the error
#     ledger treats each cause as its own kind → one "new error" push when it first
#     appears, and the Data Health errors card shows it;
#   - the latest report is kept in memory for the staleness alert, which then names the
#     cause and the interpreter, or says the collector has stopped reporting at all
#     (Mac off/asleep, job unloaded) — a different fix. Lost on restart, refilled by the
#     next run within 30 min.

_STATUS: dict = {}

FIXES = {
    "no_full_disk_access": "System Settings → Privacy & Security → Full Disk Access: remove and "
                           "re-add /usr/bin/python3 (a macOS or Command Line Tools update "
                           "invalidates the old grant).",
    "no_biome_files": "Check Screen Time → Share Across Devices on the Mac and the iPhone "
                      "(the Mac only sees iPhone data when that is on).",
    "source_missing": "knowledgeC.db is missing — macOS may have moved it; check the collector.",
    "read_error": "The collector could not read its source; see ~/Library/Logs/wellness-screentime.log.",
    "parser_missing": "The collector's SEGB parser failed to import; see the Mac log.",
}


class CollectorProblem(BaseModel):
    code: str
    detail: str = ""


class CollectorStatus(BaseModel):
    problems: dict[str, CollectorProblem] = {}   # source ("mac"/"iphone") → problem
    interpreter: str | None = None
    collected: int = 0


@router.post("/status")
async def collector_status(body: CollectorStatus, _: None = Depends(_verify_widget_token)):
    _STATUS.clear()
    _STATUS.update({"at": datetime.utcnow(), "interpreter": body.interpreter,
                    "collected": body.collected,
                    "problems": {k: v.model_dump() for k, v in body.problems.items()}})
    for source, p in body.problems.items():
        logger.error(f"Screen-time collector {source}: {p.code} — %s", p.detail[:200])
    return {"ok": True, "problems": len(body.problems)}


def latest_status() -> dict:
    """The last collector report ({} if none since boot) — read by the staleness alert."""
    return dict(_STATUS)


def describe_status(now: datetime | None = None) -> str:
    """One or two sentences for an alert: the reported cause and its fix, or silence."""
    s = latest_status()
    if not s:
        return "The collector hasn't reported since the server restarted."
    age_h = ((now or datetime.utcnow()) - s["at"]).total_seconds() / 3600
    if age_h > 2:
        return (f"The collector last reported {age_h:.0f} h ago, so it isn't running — "
                f"is the Mac asleep or off, or the job unloaded (launchctl list | grep wellness)?")
    if not s["problems"]:
        return "The collector is running and reports no problem — Apple hasn't synced new iPhone data yet."
    by_code: dict = {}                      # one fix per cause, not per source
    for source, p in s["problems"].items():
        by_code.setdefault(p["code"], []).append(source)
    parts = [f"{' + '.join(srcs)}: {code.replace('_', ' ')}. {FIXES.get(code, '')}".strip()
             for code, srcs in by_code.items()]
    who = f" (interpreter {s['interpreter']})" if s.get("interpreter") else ""
    return "Collector reports — " + " ".join(parts) + who
