"""ingestion._ingest_sleep — wake-date keying, re-export composites and fragments."""
import asyncio
from datetime import datetime

from sqlalchemy import text

from app import timeutils as tu


def _run(samples):
    from app.database import AsyncSessionLocal
    from app.services.ingestion import _ingest_sleep

    async def go():
        async with AsyncSessionLocal() as db:
            await db.execute(text("DELETE FROM sleep_sessions"))
            await db.commit()
            for s in samples:
                await _ingest_sleep(db, [s], set())
                await db.commit()
            return (await db.execute(text(
                "SELECT date, total_min FROM sleep_sessions ORDER BY date"))).all()
    return asyncio.run(go())


def test_keys_and_guards(db_schema):
    tu.set_segments([tu.AwaySegment(datetime(2026, 9, 7, 21), datetime(2026, 9, 18, 21), 180, "AWAY")])
    try:
        rows = _run([
            # home night, HAE dated by the EVENING (the 10-01 bug)
            {"date": "2026-09-04 00:00:00 -0400", "sleepStart": "2026-09-04 21:24:26 -0400",
             "sleepEnd": "2026-09-05 06:02:01 -0400", "totalSleep": 8.5},
            # A night abroad re-exported from home, stamped -0400 (export-time zone)
            {"date": "2026-09-12 00:00:00 -0400", "sleepStart": "2026-09-12 15:32:52 -0400",
             "sleepEnd": "2026-09-12 22:37:45 -0400", "totalSleep": 5.6},
            # 24 h two-night composite with MORE sleep — must not overwrite
            {"date": "2026-09-13 00:00:00 -0400", "sleepStart": "2026-09-12 17:52:00 -0400",
             "sleepEnd": "2026-09-13 17:53:00 -0400", "totalSleep": 7.0},
            # provisional → final growth on the home night
            {"date": "2026-09-04 00:00:00 -0400", "sleepStart": "2026-09-04 21:24:26 -0400",
             "sleepEnd": "2026-09-05 06:30:00 -0400", "totalSleep": 8.9},
        ])
    finally:
        tu.set_segments([])
    assert [(str(d), m) for d, m in rows] == [("2026-09-05", 534), ("2026-09-13", 336)]
