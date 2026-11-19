"""Clock changes at home are not travel (2026-11-01: US DST ended, the app went to a fixed
UTC-04:00 and asked "where are you?" with the phone at home in Eastern)."""
import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import delete

from app.services import tz

CHANGE = datetime(2026, 11, 1, 6, 0)    # 02:00 EDT → 01:00 EST
AFTER = datetime(2026, 11, 1, 10, 47)


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return AFTER.replace(tzinfo=timezone.utc) if tz else AFTER


async def _seed(db, *obs):
    from app.models import TzObservation
    await db.execute(delete(TzObservation))
    for at, off, sample, seen in obs:
        db.add(TzObservation(observed_at=at, utc_offset_min=off, latest_sample_utc=sample,
                             offsets_seen=json.dumps({str(k): 1 for k in seen}), n_samples=1,
                             source="hae"))
    await db.commit()


def _state(*obs):
    from app.database import AsyncSessionLocal

    async def go():
        async with AsyncSessionLocal() as db:
            await _seed(db, *obs)
            with patch.object(tz, "datetime", _Clock):
                st = await tz._prompt_state(db)
            return st, await tz.tz_at(db, AFTER), await tz.build_segments(db)
    return asyncio.run(go())


def test_edt_observation_before_the_change(db_schema):
    st, z, segs = _state((datetime(2026, 11, 1, 0, 59), -240, datetime(2026, 10, 31, 19), [-240]))
    assert not st["needed"] and getattr(z, "key", None) == "America/New_York"
    assert not [s for s in segs if s.start >= datetime(2026, 10, 25)]


def test_old_samples_sent_after_the_change(db_schema):
    st, z, _ = _state((datetime(2026, 11, 1, 10, 0), -240, datetime(2026, 10, 31, 12), [-240]))
    assert not st["needed"] and getattr(z, "key", None) == "America/New_York"


def test_summer_history_resent_in_winter(db_schema):
    st, _, _ = _state((datetime(2026, 11, 1, 10, 0), -300, datetime(2026, 11, 1, 9), [-240, -300]))
    assert not st["needed"]


def test_real_travel_still_asks(db_schema):
    # A fresh sample on the old offset after the change is not home.
    st, z, _ = _state((datetime(2026, 11, 1, 10, 0), -240, datetime(2026, 11, 1, 9), [-240]))
    assert st["needed"] and st["reason"] == "offset" and getattr(z, "key", None) is None
    st, _, _ = _state((datetime(2026, 11, 1, 10, 0), -360, datetime(2026, 11, 1, 9), [-360]))
    assert st["needed"] and st["reason"] == "offset"
    st, _, _ = _state((datetime(2026, 11, 1, 10, 0), 180, datetime(2026, 11, 1, 9), [-300, 180]))
    assert st["needed"] and st["reason"] == "mixed"
