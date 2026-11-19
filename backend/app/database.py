import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

# Convert sqlite:/// to sqlite+aiosqlite:/// for async driver
_raw_url = settings.database_url
if _raw_url.startswith("sqlite:///") and "+aiosqlite" not in _raw_url:
    _async_url = _raw_url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
else:
    _async_url = _raw_url

engine = create_async_engine(_async_url, echo=False)

if _async_url.startswith("sqlite"):
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        """WAL + NORMAL sync + busy timeout (2026-10-21).

        The DB ran in the default rollback-journal mode, where a writer (the scheduler,
        a webhook ingest) blocks every reader for the length of its transaction. WAL lets
        reads proceed during writes; journal_mode=WAL persists in the file, the other two
        are per-connection. NORMAL is the standard pairing with WAL (durable across app
        crashes; only a power cut can lose the last commit). The nightly
        _wal_checkpoint job folds the -wal file back in before the 02:00/03:00 backups,
        so a raw file copy (restic) sees a complete database.
        """
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
