"""CRUD for day flags — mark/unmark nonstandard days (Today toggle + calendar backfill)."""
from datetime import date as Date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import DayFlag, SubjectiveLog
from app.services.day_flags import DAY_FLAG_REASONS, REASON_LABELS, sync_sick_checkin
from app.services.trip_gaps import ITEMS, all_gaps, gap_dates, save_gaps

router = APIRouter(prefix="/api/day-flags", tags=["day-flags"])


def _serialize(r: DayFlag) -> dict:
    return {
        "id": r.id,
        "date": r.date.isoformat(),
        "reason": r.reason,
        "label": REASON_LABELS.get(r.reason, r.reason),
        "source": r.source or "manual",
        "note": r.note,
    }


@router.get("")
async def list_flags(
    start: Date = Query(...),
    end: Date = Query(...),
    include_legacy_sick: bool = Query(
        default=False,
        description="Also return days marked sick on the morning check-in only "
                    "(subjective_log.sick) with no matching DayFlag row.",
    ),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """All flags in [start, end], keyed for the calendar/Today UI.

    TWO SOURCES OF TRUTH, RECONCILED HERE (2026-09-07). `services/day_flags` has always
    unioned `DayFlag` with the legacy `subjective_log.sick` — both `excluded_dates`
    (correlations + experiments) and `flags_in_range` (the AI summaries) see both. This
    endpoint saw only `DayFlag`, so chart bands drew a shorter illness than the briefing
    described: Sep 2-3 on screen against "Sick Sep 2-5" in the summary, because Sep 4-5
    were logged sick at the morning check-in and never separately flagged.

    OPT-IN rather than default, because the synthetic rows carry `id: null` — they are
    not deletable, and DayFlagControl's edit flow would need a special case for them.
    Read-only consumers (the chart bands) pass the flag; the editor does not.
    """
    rows = (await db.execute(
        select(DayFlag).where(DayFlag.date >= start).where(DayFlag.date <= end)
        .order_by(DayFlag.date, DayFlag.id)
    )).scalars().all()
    flags = [_serialize(r) for r in rows]

    if include_legacy_sick:
        already = {r.date for r in rows if r.reason == "sick"}
        legacy = (await db.execute(
            select(SubjectiveLog.date)
            .where(SubjectiveLog.date >= start).where(SubjectiveLog.date <= end)
            .where(SubjectiveLog.sick.is_(True))
        )).all()
        for (d,) in legacy:
            if d in already:
                continue
            flags.append({
                "id": None, "date": d.isoformat(), "reason": "sick",
                "label": REASON_LABELS["sick"], "source": "subjective_log",
                "note": "marked sick on the morning check-in",
            })
        flags.sort(key=lambda f: (f["date"], f["reason"]))

    return {"flags": flags, "reasons": list(DAY_FLAG_REASONS), "labels": REASON_LABELS}


class FlagIn(BaseModel):
    date: Date
    reason: str
    note: str | None = None


@router.post("", status_code=201)
async def set_flag(
    body: FlagIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Add a manual flag. Idempotent on (date, reason)."""
    if body.reason not in DAY_FLAG_REASONS:
        raise HTTPException(status_code=422, detail=f"unknown reason: {body.reason}")
    existing = (await db.execute(
        select(DayFlag).where(DayFlag.date == body.date).where(DayFlag.reason == body.reason)
    )).scalar_one_or_none()
    if existing is not None:
        if body.note is not None:
            existing.note = body.note
            existing.source = "manual"       # a manual re-set claims ownership
            await db.commit()
        return _serialize(existing)
    row = DayFlag(date=body.date, reason=body.reason, source="manual", note=body.note)
    db.add(row)
    # Reverse half of the note-23 sync: keep an EXISTING morning check-in in step, so the
    # two UIs stop contradicting each other. Never creates a check-in row — see the note
    # on `sync_sick_checkin`.
    if body.reason == "sick":
        await sync_sick_checkin(db, body.date, True)
    await db.commit()
    await db.refresh(row)
    return _serialize(row)


@router.delete("")
async def clear_flag(
    date: Date = Query(...),
    reason: str = Query(...),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Remove a flag (also how a user overrides a wrong auto flag)."""
    row = (await db.execute(
        select(DayFlag).where(DayFlag.date == date).where(DayFlag.reason == reason)
    )).scalar_one_or_none()
    if row is None:
        return {"ok": True, "removed": 0}
    await db.delete(row)
    # Clearing the flag in Today also retracts it on an existing check-in — otherwise
    # un-flagging a day left `subjective_log.sick` true and the correlation engine, which
    # unions both sources, would keep excluding the day the user just un-excluded.
    if reason == "sick":
        await sync_sick_checkin(db, date, False)
    await db.commit()
    return {"ok": True, "removed": 1}


# ── Trips (away periods) — note 72, 2026-10-19 ──────────────────────────────────────
#
# A trip is just a run of `travel` flags (see services/day_flags "Away periods"). These
# endpoints set/clear a whole range at once so a trip can be entered BEFORE leaving:
# pushes, check-in takeovers, meal reminders and experiment draws then stand down for
# those days, and the post-trip `travel_recovery` buffer follows automatically.

TRIP_MAX_DAYS = 120


class TripIn(BaseModel):
    start: Date
    end: Date
    note: str | None = None
    # Optional zone for the trip ("+03:00", "Europe/Istanbul") → a manual TzPeriod,
    # the override for tz awareness (services/tz.py). Managed under /api/tz after that.
    tz_name: str | None = None


def _span(start: Date, end: Date) -> list[Date]:
    if end < start:
        raise HTTPException(status_code=422, detail="end is before start")
    n = (end - start).days + 1
    if n > TRIP_MAX_DAYS:
        raise HTTPException(status_code=422, detail=f"trip longer than {TRIP_MAX_DAYS} days")
    return [start + timedelta(days=i) for i in range(n)]


async def _resync_nutrition(db: AsyncSession, days: list[Date]) -> None:
    from app.services.nutrition_completeness import sync_day
    for d in days:
        await sync_day(db, d)          # no-op for today/future


@router.get("/trips")
async def list_trips(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Runs of consecutive travel days, newest first, plus any recovery buffer."""
    from app.services.day_flags import AWAY_REASON, RECOVERY_REASON
    rows = (await db.execute(
        select(DayFlag.date, DayFlag.reason, DayFlag.source)
        .where(DayFlag.reason.in_((AWAY_REASON, RECOVERY_REASON)))
        .order_by(DayFlag.date)
    )).all()
    trips, recovery = [], {r.date for r in rows if r.reason == RECOVERY_REASON}
    gaps = {g.start_date: g for g in await all_gaps(db)}
    for d, reason, source in rows:
        if reason != AWAY_REASON:
            continue
        if trips and (d - trips[-1]["_end"]).days == 1:
            t = trips[-1]
        else:
            t = {"_start": d, "_end": d, "manual": 0, "auto": 0}
            trips.append(t)
        t["_end"] = d
        t["manual" if (source or "manual") == "manual" else "auto"] += 1
    out = []
    for t in reversed(trips):
        rec = []
        d = t["_end"] + timedelta(days=1)
        while d in recovery:
            rec.append(d.isoformat()); d += timedelta(days=1)
        g = gaps.get(t["_start"])
        out.append({"start": t["_start"].isoformat(), "end": t["_end"].isoformat(),
                    "days": (t["_end"] - t["_start"]).days + 1,
                    "manual_days": t["manual"], "auto_days": t["auto"],
                    "recovery_days": rec,
                    # Note 102: what wasn't logged on this trip; null = not answered yet.
                    "unlogged": list(g.items) if g is not None else None})
    return {"trips": out, "gap_items": list(ITEMS)}


class TripGapsIn(BaseModel):
    start: Date
    end: Date
    items: list[str] = []


@router.put("/trips/unlogged")
async def put_trip_gaps(body: TripGapsIn, db: AsyncSession = Depends(get_db),
                        _: str = Depends(get_current_user)):
    """Save what wasn't logged on the trip [start, end] (note 102)."""
    if body.end < body.start:
        raise HTTPException(status_code=422, detail="end before start")
    bad = [i for i in body.items if i not in ITEMS]
    if bad:
        raise HTTPException(status_code=422, detail=f"unknown items: {', '.join(bad)}")
    row = await save_gaps(db, body.start, body.end, body.items)
    return {"start": row.start_date.isoformat(), "end": row.end_date.isoformat(),
            "unlogged": list(row.items)}


@router.get("/trip-gaps")
async def get_trip_gaps(start: Date = Query(...), end: Date = Query(...),
                        db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)):
    """{item: [dates]} not logged in [start, end], for charts that draw them as gaps."""
    return {i: sorted(d.isoformat() for d in await gap_dates(db, i, start, end)) for i in ITEMS}


@router.post("/trips", status_code=201)
async def add_trip(
    body: TripIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Mark [start, end] as away. Idempotent; an existing auto travel flag in the range
    is claimed as manual so the slept_away sync can no longer retract it."""
    days = _span(body.start, body.end)
    note = body.note or "trip"
    if body.tz_name and body.tz_name.strip():
        from app.models import TzPeriod
        from app.services.tz import zone_from_name
        try:
            zone_from_name(body.tz_name)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        db.add(TzPeriod(start_date=body.start, end_date=body.end,
                        tz_name=body.tz_name.strip(), source="manual", note=note))
    existing = {r.date: r for r in (await db.execute(
        select(DayFlag).where(DayFlag.date >= body.start).where(DayFlag.date <= body.end)
        .where(DayFlag.reason == "travel")
    )).scalars().all()}
    for d in days:
        row = existing.get(d)
        if row is None:
            db.add(DayFlag(date=d, reason="travel", source="manual", note=note))
        else:
            row.source, row.note = "manual", note
    await db.commit()
    if body.tz_name and body.tz_name.strip():
        from app.services.tz import refresh_current_zone
        await refresh_current_zone(db)
    await _resync_nutrition(db, days)
    return {"ok": True, "start": body.start.isoformat(), "end": body.end.isoformat(),
            "days": len(days)}


@router.delete("/trips")
async def remove_trip(
    start: Date = Query(...),
    end: Date = Query(...),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Un-mark [start, end]: removes travel flags (manual and auto) in the range. The
    recovery buffer after it is left alone — delete those days individually if wanted."""
    days = _span(start, end)
    rows = (await db.execute(
        select(DayFlag).where(DayFlag.date >= start).where(DayFlag.date <= end)
        .where(DayFlag.reason == "travel")
    )).scalars().all()
    for r in rows:
        await db.delete(r)
    await db.commit()
    await _resync_nutrition(db, days)
    return {"ok": True, "removed": len(rows)}
