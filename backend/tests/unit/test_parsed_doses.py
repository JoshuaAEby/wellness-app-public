"""Note 100: a parsed drink logs its caffeine / alcohol as a linked entry."""
import asyncio
from datetime import date


def test_normaliser_keeps_only_usable_doses():
    from app.services.ai_summaries import _normalise_item
    base = {"name": "x", "calories": 5, "protein_g": 0, "carbs_g": 1, "fat_g": 0, "confidence": "high"}
    a = _normalise_item({**base, "caffeine_mg": "140"}, "breakfast")
    assert a["caffeine_mg"] == 140.0 and a["alcohol_abv"] is None
    b = _normalise_item({**base, "caffeine_mg": 0, "alcohol_abv": 5, "alcohol_oz": None}, "dinner")
    assert b["caffeine_mg"] is None and (b["alcohol_abv"], b["alcohol_oz"]) == (None, None)
    c = _normalise_item({**base, "alcohol_abv": 5, "alcohol_oz": 24}, "dinner")
    assert (c["alcohol_abv"], c["alcohol_oz"]) == (5.0, 24.0)


def test_batch_links_doses_and_delete_removes_both(db_schema):
    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.models import CaffeineLog, FoodLog, HealthMetric
    from app.routers.food_log import LogBatchCreate, create_logs_batch, delete_log

    async def go():
        async with AsyncSessionLocal() as db:
            body = LogBatchCreate(date=date(2026, 11, 3), at_time="08:00", meal_type="breakfast", items=[
                {"description": "12 oz drip coffee", "portion_quantity": 12, "portion_unit": "oz",
                 "calories": 5, "caffeine_mg": 140},
                {"description": "two lagers", "portion_quantity": 24, "portion_unit": "oz",
                 "calories": 300, "alcohol_abv": 5, "alcohol_oz": 24},
                {"description": "toast", "calories": 80},
            ])
            out = await create_logs_batch(body=body, db=db, _="owner")
            rows = {r["description"]: r["id"] for r in out}
            coffee = (await db.execute(select(FoodLog).where(FoodLog.id == rows["12 oz drip coffee"]))).scalar_one()
            beer = (await db.execute(select(FoodLog).where(FoodLog.id == rows["two lagers"]))).scalar_one()
            toast = (await db.execute(select(FoodLog).where(FoodLog.id == rows["toast"]))).scalar_one()
            dose = (await db.execute(select(CaffeineLog).where(CaffeineLog.id == coffee.source_id))).scalar_one()
            sdu = (await db.execute(select(HealthMetric.value).where(HealthMetric.id == beer.source_id))).scalar_one()
            linked = (coffee.source_kind, beer.source_kind, toast.source_kind, dose.mg, round(sdu, 2))
            await delete_log(log_id=coffee.id, db=db, _="owner")
            gone = (await db.execute(select(CaffeineLog).where(CaffeineLog.id == dose.id))).scalar_one_or_none()
            return linked, gone

    linked, gone = asyncio.run(go())
    assert linked == ("caffeine", "alcohol", None, 140.0, 2.0)
    assert gone is None
