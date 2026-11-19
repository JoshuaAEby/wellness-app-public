"""Verification for the note-23 sick-flag sync (2026-09-07).

Two writers, one fact: the morning check-in sets `subjective_log.sick`, the Today tab
writes `DayFlag(reason='sick')`. `services/day_flags.sync_sick_flag` / `sync_sick_checkin`
keep them in step. The asymmetries below are the whole design and each is load-bearing:

  * a MANUAL flag must survive a check-in that says not-sick (the user's own statement
    about the day outranks a toggle they may simply have left alone)
  * `sick=None` must retract nothing (a partial submission — an iOS Shortcut sending only
    subscores — merges to None for every omitted field)
  * flagging sick in Today must NEVER create a check-in row (it would manufacture a
    morning check-in that never happened, and the weekly digest counts those rows)

Uses far-future scratch dates and deletes them afterwards, but point it at a COPY:

    docker run --rm -v /tmp/wellness-preflight/app:/app/app:ro -v /tmp/t.db:/data/wellness.db \
      -e DATABASE_URL=sqlite+aiosqlite:////data/wellness.db -e PYTHONPATH=/app \
      wellness-app-wellness-app:latest python /app/app/../tests/verify_sick_sync.py
"""
import asyncio, datetime as dt
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models import DayFlag, SubjectiveLog
from app.services.day_flags import sync_sick_flag, sync_sick_checkin, excluded_dates

D = dt.date(2029, 1, 5)          # far-future scratch dates, no real data
D2 = dt.date(2029, 1, 6)
D3 = dt.date(2029, 1, 7)
ok = True
def chk(cond, msg):
    global ok
    ok &= bool(cond)
    print(("PASS  " if cond else "FAIL  ") + msg)

async def flag(db, d):
    return (await db.execute(select(DayFlag).where(DayFlag.date == d)
            .where(DayFlag.reason == "sick"))).scalars().first()

async def main():
    async with AsyncSessionLocal() as db:
        # 1. check-in says sick -> flag created
        await sync_sick_flag(db, D, True); await db.commit()
        f = await flag(db, D)
        chk(f is not None and f.source == "checkin", "check-in sick=True creates a flag (source=checkin)")

        # 2. idempotent
        r = await sync_sick_flag(db, D, True); await db.commit()
        chk(r == "already-flagged", "re-submitting sick=True does not duplicate")

        # 3. check-in says not-sick -> our own flag is retracted
        await sync_sick_flag(db, D, False); await db.commit()
        chk(await flag(db, D) is None, "check-in sick=False clears the check-in's own flag")

        # 4. a MANUAL flag must survive a check-in that says not-sick
        db.add(DayFlag(date=D2, reason="sick", source="manual", note="typed in Today"))
        await db.commit()
        await sync_sick_flag(db, D2, False); await db.commit()
        f = await flag(db, D2)
        chk(f is not None and f.source == "manual", "a MANUAL flag survives check-in sick=False")

        # 5. None is a no-op (partial submissions must not retract anything)
        r = await sync_sick_flag(db, D2, None); await db.commit()
        chk(r == "noop" and await flag(db, D2) is not None, "sick=None is a no-op, retracts nothing")

        # 6. reverse: updates an EXISTING check-in row
        db.add(SubjectiveLog(date=D3, sick=False)); await db.commit()
        r = await sync_sick_checkin(db, D3, True); await db.commit()
        row = (await db.execute(select(SubjectiveLog).where(SubjectiveLog.date == D3))).scalars().first()
        chk(r == "updated" and row.sick is True, "flagging sick in Today updates an existing check-in")

        # 7. reverse: NEVER creates a check-in row (would fake a morning check-in)
        before = (await db.execute(select(SubjectiveLog))).scalars().all()
        r = await sync_sick_checkin(db, dt.date(2029, 1, 8), True); await db.commit()
        after = (await db.execute(select(SubjectiveLog))).scalars().all()
        chk(r == "no-checkin-row" and len(before) == len(after),
            "flagging sick with no check-in row creates NO phantom check-in")

        # 8. clearing in Today retracts the check-in too
        await sync_sick_checkin(db, D3, False); await db.commit()
        row = (await db.execute(select(SubjectiveLog).where(SubjectiveLog.date == D3))).scalars().first()
        chk(row.sick is False, "clearing the flag in Today retracts sick on the check-in")

        # 9. the union read still sees a legacy-only sick day
        db.add(SubjectiveLog(date=dt.date(2029, 2, 1), sick=True)); await db.commit()
        ex = await excluded_dates(db, dt.date(2029, 1, 1), dt.date(2029, 3, 1))
        chk(dt.date(2029, 2, 1) in ex, "legacy-only sick day still excluded by excluded_dates")

        # cleanup scratch rows
        for d in (D, D2, D3, dt.date(2029, 2, 1)):
            for r_ in (await db.execute(select(DayFlag).where(DayFlag.date == d))).scalars().all():
                await db.delete(r_)
            for r_ in (await db.execute(select(SubjectiveLog).where(SubjectiveLog.date == d))).scalars().all():
                await db.delete(r_)
        await db.commit()
    print("\n" + ("ALL PASS" if ok else "FAILURES"))
asyncio.run(main())
