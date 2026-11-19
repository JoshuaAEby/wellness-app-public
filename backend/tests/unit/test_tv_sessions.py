"""services/tv — building sessions from HA history, and tv_prebed_min from the answers."""
import asyncio
from datetime import date, datetime, timedelta

import httpx
from fastapi import FastAPI

from app.auth import get_current_user
from app.routers import tv as tv_router
from app.services import tv


def _pt(t, state, app=None):
    return {"last_changed": t.isoformat() + "+00:00", "state": state,
            "attributes": {"app_name": app} if app else {}}


T = datetime(2026, 11, 1, 0, 0)
WIN = T - timedelta(days=1)
NOW = T + timedelta(hours=3)


def test_build_sessions():
    m = lambda n: T + timedelta(minutes=n)
    pts = [_pt(WIN, "playing", "Hulu"),                 # cut by the window: unknown start
           _pt(WIN + timedelta(minutes=30), "off"),
           _pt(m(0), "playing", "Hulu"), _pt(m(20), "unavailable"),   # 6 min gap merges
           _pt(m(26), "paused", "Hulu"), _pt(m(30), "playing", "Netflix"), _pt(m(40), "off"),
           _pt(m(60), "playing", "Hulu"), _pt(m(63), "off"),          # under 5 min
           _pt(m(100), "playing", "Hulu"), _pt(m(120), "idle"),       # the idle state ends it
           _pt(NOW - timedelta(minutes=8), "off")]
    out = tv.build_sessions(pts, WIN, NOW)
    assert [(s["start"], s["minutes"]) for s in out] == [(m(0), 34.0), (m(100), 20.0)]
    assert [x[2] for x in out[0]["segments"]] == ["Hulu", "Hulu", "Netflix"]   # the gap stays out
    assert tv.app_minutes(out[0]["segments"]) == {"Hulu": 24, "Netflix": 10}
    # Still running, or ended just now: wait.
    assert not tv.build_sessions([_pt(m(0), "playing", "Hulu")], WIN, NOW)
    assert not tv.build_sessions([_pt(NOW - timedelta(minutes=30), "playing", "Hulu"),
                                  _pt(NOW - timedelta(minutes=5), "off")], WIN, NOW)


def test_long_pause_stops_counting():
    m = lambda n: T + timedelta(minutes=n)
    # Walked away for over 2 h: 20 min played + 5 of the pause, and the session closes
    # even though the TV still reads paused.
    out = tv.build_sessions([_pt(m(0), "playing", "Hulu"), _pt(m(20), "paused", "Hulu")], WIN, m(150))
    assert [s["minutes"] for s in out] == [25.0]
    # Came back after 30 min: the rest is a new session, not one 80 min long.
    out = tv.build_sessions([_pt(m(0), "playing", "Hulu"), _pt(m(20), "paused", "Hulu"),
                             _pt(m(50), "playing", "Hulu"), _pt(m(80), "off")], WIN, NOW)
    assert [s["minutes"] for s in out] == [25.0, 30.0]
    # A short pause counts in full and does not split anything.
    out = tv.build_sessions([_pt(m(0), "playing", "Hulu"), _pt(m(20), "paused", "Hulu"),
                             _pt(m(23), "playing", "Hulu"), _pt(m(40), "off")], WIN, NOW)
    assert [s["minutes"] for s in out] == [40.0]


def test_prebed_from_answers(db_schema):
    from app.database import AsyncSessionLocal
    from app.models import HealthMetric, SleepSession, TvSession
    from sqlalchemy import select

    wake = date(2027, 3, 2)
    bed = datetime(2027, 3, 2, 3, 0)

    def row(start_min, mins, day=date(2027, 3, 1)):
        a, b = bed + timedelta(minutes=start_min), bed + timedelta(minutes=start_min + mins)
        return TvSession(start_time=a, end_time=b, date=day, minutes=mins,
                         segments=[[a.isoformat(), b.isoformat(), "Hulu"]])

    async def stored(db):
        return (await db.execute(select(HealthMetric.value).where(
            HealthMetric.metric_type == "tv_prebed_min"))).scalars().all()

    app = FastAPI()
    app.include_router(tv_router.router)
    app.dependency_overrides[get_current_user] = lambda: "owner"

    async def go():
        async with AsyncSessionLocal() as db:
            db.add(SleepSession(date=wake, bedtime=bed, wake_time=bed + timedelta(hours=8)))
            a, b, far = row(-40, 30), row(-130, 20), row(-300, 30)
            db.add_all([a, b, far])
            await db.commit()
            ids = a.id, b.id
            assert await tv.refresh_prebed(db, wake) is None and await stored(db) == []
        t = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=t, base_url="http://x") as c:
            post = lambda i, w: c.post(f"/api/tv/sessions/{i}/answer", json={"watcher": w})
            r = await post(ids[0], "me")
            assert r.json()["watcher"] == "me"
            async with AsyncSessionLocal() as db:
                assert await stored(db) == []          # the other session is still unknown
            await post(ids[1], "family")
            async with AsyncSessionLocal() as db:
                assert await stored(db) == [30.0]      # only his, inside the 2 h window
            r = (await c.get("/api/tv/range?start=2027-03-01&end=2027-03-02")).json()
            assert r["days"] == [{"date": "2027-03-01", "me": 30, "family": 20, "other": 0}]
            assert r["apps"]["family"] == {"Hulu": 20} and r["unanswered_min"] == 30
            await post(ids[1], "me")
            async with AsyncSessionLocal() as db:
                assert await stored(db) == [40.0]      # 10 of its 20 min were inside
            await post(ids[0], None)
            async with AsyncSessionLocal() as db:
                assert await stored(db) == []          # undo returns it to unknown
            assert (await post(ids[0], "nobody")).status_code == 422
            assert (await post(999, "me")).status_code == 404
    asyncio.run(go())
