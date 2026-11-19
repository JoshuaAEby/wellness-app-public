"""routers/manual_entry body scans — derived fat/lean, validation, replace-on-date, and
the nearest home-scale reading the scan is compared against."""
import asyncio
from datetime import datetime

import httpx
from fastapi import FastAPI

from app.auth import get_current_user
from app.routers import manual_entry


def _app():
    app = FastAPI()
    app.include_router(manual_entry.router)
    app.dependency_overrides[get_current_user] = lambda: "owner"
    return app


def test_scans(db_schema):
    from app.database import AsyncSessionLocal
    from app.models import HealthMetric

    async def seed():
        async with AsyncSessionLocal() as db:
            # Home scale: one weigh-in 2 days before the scan, one 5 days before (outside ±3).
            for ts, w, bf, lean in ((datetime(2026, 5, 18, 14, 55), 179.4, 19.0, 149.0),
                                    (datetime(2026, 5, 15, 17, 13), 181.0, 19.5, 149.5)):
                for mt, v in (("weight", w), ("body_fat_percentage", bf), ("lean_body_mass", lean)):
                    db.add(HealthMetric(metric_type=mt, timestamp=ts, value=v, source="apple_health"))
            await db.commit()

    async def go():
        await seed()
        t = httpx.ASGITransport(app=_app())
        async with httpx.AsyncClient(transport=t, base_url="http://x") as c:
            r = await c.post("/api/manual/scans", json={
                "date": "2026-05-20", "time": "08:28", "weight_lb": 180.0, "body_fat_pct": 25.0,
                "smm_lb": 70.0, "segments": {"left_arm": 7.0}})
            assert r.status_code == 200, r.text
            s = r.json()
            assert s["fat_mass_lb"] == 45.0 and s["lean_mass_lb"] == 135.0   # derived
            h = s["home_scale"]
            assert h["date"] == "2026-05-18" and h["lean_gap_lb"] == 14.0 and h["bf_gap_pct"] == -6.0

            assert (await c.post("/api/manual/scans", json={
                "date": "2026-05-20", "weight_lb": 180.0, "body_fat_pct": 90})).status_code == 422
            assert (await c.post("/api/manual/scans", json={
                "date": "2026-05-20", "weight_lb": 180.0, "body_fat_pct": 25.0,
                "segments": {"neck": 3}})).status_code == 422

            # Same date replaces the whole printout (no merge with the earlier segments).
            r = await c.post("/api/manual/scans", json={
                "date": "2026-05-20", "weight_lb": 180.0, "body_fat_pct": 25.0, "lean_mass_lb": 134.0})
            assert r.json()["segments"] is None and r.json()["lean_mass_lb"] == 134.0

            # No home-scale reading within ±3 days.
            r = await c.post("/api/manual/scans", json={"date": "2025-11-12", "weight_lb": 184.0,
                                                        "body_fat_pct": 26.0})
            assert r.json()["home_scale"] is None

            rows = (await c.get("/api/manual/scans")).json()
            assert [x["date"] for x in rows] == ["2026-05-20", "2025-11-12"]
            assert (await c.delete("/api/manual/scans?date=2025-11-12")).status_code == 204
            assert (await c.delete("/api/manual/scans?date=2025-11-12")).status_code == 404
    asyncio.run(go())
