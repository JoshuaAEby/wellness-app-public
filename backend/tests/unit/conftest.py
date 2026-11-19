"""Unit tests that need no production data — safe for CI (2026-10-21).

Each test that touches a database gets a fresh SQLite file via DATABASE_URL, set BEFORE
any `app.*` import, because app.database builds its engine at import time.
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="wellness-unit-")
# ASSIGNED, never setdefault (2026-10-27): with setdefault, running the suite where
# DATABASE_URL is already set — a throwaway container pointed at a prod copy, or the prod
# container itself — ran the tests against THAT database. test_sleep_ingest deletes
# sleep_sessions: a prod copy went from 852 rows to 2. Same for NTFY_URL / the API key,
# so a test can never push or spend.
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/unit.db"
os.environ.setdefault("TZ", "America/New_York")
os.environ["NTFY_URL"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import asyncio  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def db_schema():
    """Create every table once (metadata, not Alembic — tests need the shape, not history)."""
    from app.database import engine
    from app.models import Base

    async def _create():
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
            from sqlalchemy import text
            for ddl in (
                "CREATE TABLE IF NOT EXISTS idempotency_keys (key TEXT PRIMARY KEY, path TEXT, "
                "status INTEGER NOT NULL, body TEXT, content_type TEXT, created_at DATETIME NOT NULL)",
                "CREATE TABLE IF NOT EXISTS error_events (key TEXT NOT NULL, day TEXT NOT NULL, logger TEXT, "
                "template TEXT, sample TEXT, count INTEGER NOT NULL DEFAULT 0, first_at DATETIME, "
                "last_at DATETIME, PRIMARY KEY (key, day))",
            ):
                await c.execute(text(ddl))
    asyncio.run(_create())
    return True
