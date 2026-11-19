"""Drink brand (note 98): a preset's or typed drink's brand lands on the Drinks row."""
import asyncio
from datetime import datetime


def test_brand_is_written_and_kept_on_edit(db_schema):
    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.models import FoodLog
    from app.services.intake import sync_nutrition_entry

    async def go():
        async with AsyncSessionLocal() as db:
            ts = datetime(2026, 11, 3, 23, 0)
            await sync_nutrition_entry(db, source_kind="alcohol", source_id=9001, label="Lime lager",
                                       kcal=194, ts_utc=ts, drink_kind="beer", brand="Floyd's")
            await sync_nutrition_entry(db, source_kind="alcohol", source_id=9001, label="Lime lager",
                                       kcal=190, ts_utc=ts)          # an edit sends no brand
            row = (await db.execute(select(FoodLog).where(FoodLog.source_id == 9001))).scalar_one()
            return row.brand, row.calories

    assert asyncio.run(go()) == ("Floyd's", 190.0)
