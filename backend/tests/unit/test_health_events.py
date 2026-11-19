"""routers/health_events + services/health_events — wake-date alignment, baseline
exclusions, the day-before intake, and what the briefing is told."""
import asyncio
from datetime import date, datetime, timedelta

import httpx
from fastapi import FastAPI

from app.auth import get_current_user
from app.routers import health_events


def _app():
    app = FastAPI()
    app.include_router(health_events.router)
    app.dependency_overrides[get_current_user] = lambda: "owner"
    return app


def test_health_events(db_schema):
    from app.database import AsyncSessionLocal
    from app.models import DailyMetric, DayFlag, FoodLog, HealthMetric, SleepSession, SubjectiveLog
    from app.services.summary_context import today_flag_note

    shot = date(2024, 10, 30)

    async def seed():
        async with AsyncSessionLocal() as db:
            for i in range(1, 41):
                d = shot - timedelta(days=i + 1)
                db.add(DailyMetric(date=d, hrv_value=60 + i % 3, rhr_value=58 + i % 2))
                db.add(SubjectiveLog(date=d, energy=3.0 + (i % 2) * 0.5))
            # A travel day with a wild value must not enter the baseline.
            travel = shot - timedelta(days=5)
            await db.flush()
            db.add(DayFlag(date=travel, reason="travel"))
            (await db.get(DailyMetric, 4)).hrv_value = 500
            db.add(DailyMetric(date=shot + timedelta(days=1), hrv_value=40, rhr_value=59))
            db.add(SleepSession(date=shot + timedelta(days=1),
                                bedtime=datetime(2024, 10, 31, 3, 0),
                                wake_time=datetime(2024, 10, 31, 10, 0)))
            db.add(HealthMetric(metric_type="respiratory_rate",
                                timestamp=datetime(2024, 10, 31, 5, 0), value=16.0, source="t"))
            # Daytime sample, outside the night: not counted.
            db.add(HealthMetric(metric_type="respiratory_rate",
                                timestamp=datetime(2024, 10, 31, 15, 0), value=30.0, source="t"))
            db.add(HealthMetric(metric_type="alcohol",
                                timestamp=datetime(2024, 10, 30, 23, 0), value=2.8, source="t"))
            db.add(FoodLog(timestamp=datetime(2024, 10, 30, 19, 0), description="x", calories=3353))
            await db.commit()

    async def go():
        await seed()
        t = httpx.ASGITransport(app=_app())
        async with httpx.AsyncClient(transport=t, base_url="http://x") as c:
            r = await c.post("/api/health-events", json={
                "date": shot.isoformat(), "kind": "vaccine", "name": " Flu shot ", "notes": "  "})
            assert r.status_code == 200, r.text
            e = r.json()
            assert e["name"] == "Flu shot" and e["notes"] is None
            assert (await c.post("/api/health-events", json={
                "date": "2024-10-30", "kind": "sick", "name": "x"})).status_code == 422

            resp = (await c.get(f"/api/health-events/{e['id']}/response")).json()
            assert [x["offset"] for x in resp["rows"]] == [-1, 0, 1, 2, 3]
            night = resp["rows"][2]   # first night after the shot
            assert night["date"] == "2024-10-31"
            assert night["hrv"]["value"] == 40 and night["hrv"]["unusual"]
            assert night["breathing"]["value"] == 16.0        # the daytime 30 is out
            assert night["breathing"]["z"] is None            # no baseline nights
            assert night["drinks_day_before"] == 2.8 and night["kcal_day_before"] == 3353
            assert resp["rows"][3]["drinks_day_before"] == 0
            assert resp["rows"][3]["kcal_day_before"] is None # unlogged food is not a zero
            assert resp["baseline"]["hrv"]["mean"] < 70       # the travel day's 500 is out
            assert resp["baseline"]["hrv"]["n"] == 39

            assert (await c.get("/api/health-events/999/response")).status_code == 404
            assert [x["name"] for x in (await c.get("/api/health-events")).json()] == ["Flu shot"]

            async with AsyncSessionLocal() as db:
                assert await today_flag_note(db, shot) == ""   # that night predates it
                note = await today_flag_note(db, shot + timedelta(days=1))
                assert "Flu shot (vaccine) yesterday" in note and "not the proven cause" in note
                assert "2 days ago" in await today_flag_note(db, shot + timedelta(days=2))
                assert await today_flag_note(db, shot + timedelta(days=3)) == ""

            assert (await c.delete(f"/api/health-events/{e['id']}")).status_code == 204
            assert (await c.delete(f"/api/health-events/{e['id']}")).status_code == 404
    asyncio.run(go())


def test_sick_episodes(db_schema):
    from app.database import AsyncSessionLocal
    from app.models import DailyMetric, DayFlag, SubjectiveLog

    onset = date(2023, 3, 10)

    async def seed():
        async with AsyncSessionLocal() as db:
            # One episode across both stores, with a one-day gap; a separate one later.
            db.add(DayFlag(date=onset, reason="sick"))
            db.add(SubjectiveLog(date=onset + timedelta(days=1), sick=True))
            db.add(DayFlag(date=onset + timedelta(days=3), reason="sick"))
            db.add(DayFlag(date=onset + timedelta(days=30), reason="sick"))
            db.add(DayFlag(date=onset - timedelta(days=9), reason="travel"))  # not sickness
            for i in range(4, 30):
                db.add(DailyMetric(date=onset - timedelta(days=i), hrv_value=60 + i % 3))
            # Run-up value: shown in the table, must stay out of the baseline.
            db.add(DailyMetric(date=onset - timedelta(days=2), hrv_value=200))
            await db.commit()

    async def go():
        await seed()
        t = httpx.ASGITransport(app=_app())
        async with httpx.AsyncClient(transport=t, base_url="http://x") as c:
            eps = (await c.get("/api/health-events/sick-episodes")).json()
            old = [e for e in eps if e["start"] < "2024"]
            assert old == [{"start": "2023-04-09", "end": "2023-04-09", "days": 1},
                           {"start": "2023-03-10", "end": "2023-03-13", "days": 3}]
            r = (await c.get("/api/health-events/sick-episodes/2023-03-10/response")).json()
            assert [x["offset"] for x in r["rows"]] == [-3, -2, -1, 0, 1]
            assert r["rows"][1]["hrv"]["value"] == 200 and r["rows"][1]["hrv"]["unusual"]
            assert r["baseline"]["hrv"]["mean"] < 70
            assert (await c.get(
                "/api/health-events/sick-episodes/2023-03-11/response")).status_code == 404
    asyncio.run(go())
