"""Note 84: restaurant tag on food entries and the restaurant_meals input."""
import asyncio
from datetime import date


def test_normaliser_keeps_only_booleans():
    from app.services.ai_summaries import _normalise_item
    base = {"name": "x", "calories": 500, "protein_g": 20, "carbs_g": 50, "fat_g": 20, "confidence": "high"}
    assert _normalise_item({**base, "restaurant": True}, "lunch")["restaurant"] is True
    assert _normalise_item({**base, "restaurant": False}, "lunch")["restaurant"] is False
    assert _normalise_item({**base, "restaurant": "yes"}, "lunch")["restaurant"] is None
    assert _normalise_item(dict(base), "lunch")["restaurant"] is None


def test_relog_inherits_and_series_counts_meals(db_schema):
    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.models import DailyMetric, FoodLog
    from app.routers.food_log import LogBatchCreate, create_logs_batch
    from app.services.correlations import _fetch_daily_series_uncached

    async def go():
        async with AsyncSessionLocal() as db:
            for d in (1, 2, 3):          # the series has a row only where daily_metrics does
                db.add(DailyMetric(date=date(2026, 11, d)))
            await db.commit()
            await create_logs_batch(body=LogBatchCreate(date=date(2026, 11, 2), at_time="12:30", meal_type="lunch", items=[
                {"name": "Chicken burrito bowl", "brand": "Chipotle", "description": "Chipotle bowl",
                 "calories": 900, "restaurant": True},
            ]), db=db, _="owner")
            # A re-log with no answer reuses the last one for the same name + brand.
            await create_logs_batch(body=LogBatchCreate(date=date(2026, 11, 3), at_time="12:30", meal_type="lunch", items=[
                {"name": "chicken burrito bowl", "brand": "Chipotle", "description": "bowl", "calories": 900},
                {"name": "Chips", "description": "chips", "calories": 200},
            ]), db=db, _="owner")
            await create_logs_batch(body=LogBatchCreate(date=date(2026, 11, 3), at_time="19:00", meal_type="dinner", items=[
                {"name": "Pasta", "description": "home pasta", "calories": 700, "restaurant": False},
            ]), db=db, _="owner")
            await create_logs_batch(body=LogBatchCreate(date=date(2026, 11, 3), at_time="20:00", meal_type="drinks", items=[
                {"name": "IPA", "description": "IPA at the pub", "calories": 200, "restaurant": True},
            ]), db=db, _="owner")
            rows = (await db.execute(select(FoodLog.name, FoodLog.restaurant).where(
                FoodLog.timestamp >= "2026-11-03"))).all()
            series, _raw = await _fetch_daily_series_uncached(db, date(2026, 11, 1), date(2026, 11, 3))
            return {n: r for n, r in rows}, {d: v.get("restaurant_meals") for d, v in series.items()}

    rows, series = asyncio.run(go())
    assert rows["chicken burrito bowl"] is True
    assert rows["Chips"] is None and rows["Pasta"] is False
    # Before the first tagged day: unknown. Drinks don't count; lunch does.
    assert series.get(date(2026, 11, 1)) is None
    assert series.get(date(2026, 11, 3)) == 1.0
