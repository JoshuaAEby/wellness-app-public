"""app/error_ledger — ERROR lines grouped by template; warnings ignored; repeats accumulate."""
import asyncio
import logging


def test_grouping(db_schema):
    from app import error_ledger
    from app.database import AsyncSessionLocal
    error_ledger.install()
    log = logging.getLogger("app.unit.test")
    for i in range(3):
        log.error("unit: thing failed: %s", f"#{i}")
    log.warning("unit: a warning, not counted")

    async def go():
        async with AsyncSessionLocal() as db:
            await error_ledger.flush(db)
            return await error_ledger.summary(db, 7)
    s = asyncio.run(go())
    mine = [e for e in s["errors"] if e["logger"] == "unit.test"]
    assert len(mine) == 1 and mine[0]["count"] == 3 and mine[0]["sample"].endswith("#2")
