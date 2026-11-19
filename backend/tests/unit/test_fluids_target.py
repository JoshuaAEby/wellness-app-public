"""services/fluids target from 2026-11-01: flat, plus wine/spirits, nothing for beer."""
import asyncio
from datetime import date, datetime

from app.services import fluids


def test_is_watery():
    assert fluids.is_watery("beer", None) and fluids.is_watery("seltzer", "Wine-ish")
    assert fluids.is_watery("ipa", None) and fluids.is_watery("beer_light", None)
    assert not fluids.is_watery("wine", "Beer glass")   # a stored kind wins over the label
    assert not fluids.is_watery("cocktail", None) and not fluids.is_watery("liquor", None)
    assert fluids.is_watery(None, "Lime lager") and fluids.is_watery(None, "High Noon")
    assert not fluids.is_watery(None, "Alcoholic drink") and not fluids.is_watery(None, None)
    assert not fluids.is_watery(None, "Pale wine")      # " ale" needs a word start


def test_targets(db_schema):
    from app.database import AsyncSessionLocal
    from app.models import FoodLog, HealthMetric

    day = date(2027, 2, 10)

    async def drink(db, hour, sdu, kind, name):
        m = HealthMetric(metric_type="alcohol", timestamp=datetime(2027, 2, 10, hour), value=sdu,
                         source="wellness_app")
        db.add(m)
        await db.flush()
        if name:
            db.add(FoodLog(timestamp=datetime(2027, 2, 10, hour), description=name, name=name,
                           source_kind="alcohol", source_id=m.id, drink_kind=kind))

    async def go():
        async with AsyncSessionLocal() as db:
            assert (await fluids.targets(db, day, day))[day]["target"] == 100
            await drink(db, 18, 1.0, "beer", "Beer")
            await drink(db, 19, 2.0, "wine", "Wine")
            await drink(db, 20, 0.5, None, None)         # Apple Health: no linked row
            await db.commit()
            t = (await fluids.targets(db, day, day))[day]
            assert t == {"target": 110, "base": 100, "training_min": 0, "alcohol_sdu": 2.5,
                         "flat": True}
            # Before the start date the old rule stands, wine or not.
            old = date(2026, 10, 31)
            db.add(HealthMetric(metric_type="weight", timestamp=datetime(2026, 10, 30, 12), value=200,
                                source="t"))
            await db.commit()
            assert (await fluids.targets(db, old, old))[old]["target"] == 86
    asyncio.run(go())


def test_goal_met_days(db_schema):
    from unittest.mock import patch

    from app.database import AsyncSessionLocal
    from app.models import HealthMetric
    from app.services.adherence import adherence_from_calendar
    from app.services.game import _habit_weights

    days = [date(2027, 5, d) for d in (10, 11, 12, 13)]

    async def go():
        async with AsyncSessionLocal() as db:
            await db.execute(HealthMetric.__table__.delete().where(HealthMetric.metric_type == "water"))
            for d, oz in ((10, 100), (11, 80), (13, 60)):        # 12th: no water logged
                db.add(HealthMetric(metric_type="water", timestamp=datetime(2027, 5, d, 16), value=oz,
                                    source="t"))
            await db.commit()
            with patch.object(fluids, "food_today", lambda: days[3]):
                got = await fluids.goal_met_days(db, days[0], days[3])
                assert got == {days[0]: True, days[1]: False, days[3]: None}  # today isn't a miss
                assert not await fluids.goal_met_days(db, date(2020, 1, 1), date(2020, 1, 2))
    asyncio.run(go())

    cal = [{"date": "2027-05-10", "fluids_goal": True}, {"date": "2027-05-11", "fluids_goal": False},
           {"date": "2027-05-12", "fluids_goal": None}]
    row = next(r for r in adherence_from_calendar(cal) if r["key"] == "fluids_goal")
    assert (row["num"], row["den"]) == (1, 2)                # unlogged is out of the rate
    assert "fluids_goal" not in _habit_weights(cal, set())   # and Momentum never sees it
