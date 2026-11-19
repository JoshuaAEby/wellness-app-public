"""A meal logged piece by piece becomes one My Food combination (2026-11-12)."""
import asyncio
from datetime import date

import pytest


def test_logged_entries_become_a_combination(db_schema):
    from fastapi import HTTPException
    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.models import FoodLog, MyFood
    from app.routers.food_log import LogBatchCreate, create_logs_batch
    from app.routers.my_foods import ComboFromItemsIn, combo_from_items
    from app.services.food_tags import item_key

    async def go():
        async with AsyncSessionLocal() as db:
            db.add(MyFood(name="Ranch", item_key=item_key("Ranch", None), role="topping",
                          serving_qty=2, serving_unit="tbsp", calories=140, fat_g=14, source="label"))
            await db.commit()
            out = await create_logs_batch(body=LogBatchCreate(date=date(2026, 11, 12), at_time="12:30",
                                                              meal_type="lunch", items=[
                {"name": "Spinach salad", "description": "spinach salad", "portion_quantity": 2,
                 "portion_unit": "cup", "calories": 40, "protein_g": 2},
                {"name": "Ground beef", "description": "4 oz ground beef", "portion_quantity": 4,
                 "portion_unit": "oz", "calories": 280, "protein_g": 22},
                {"name": "ranch", "description": "1 tbsp ranch", "calories": 70},
            ]), db=db, _="owner")
            ids = [r["id"] for r in out]
            res = await combo_from_items(body=ComboFromItemsIn(
                name="Taco salad", meal_type="lunch", items=[{"log_id": i} for i in ids]), db=db, _="owner")
            rows = (await db.execute(select(FoodLog).where(FoodLog.id.in_(ids)).order_by(FoodLog.id))).scalars().all()
            # Same name again is refused, nothing half-written.
            try:
                await combo_from_items(body=ComboFromItemsIn(
                    name="taco salad", items=[{"log_id": ids[0]}, {"log_id": ids[1]}]), db=db, _="owner")
                dup = None
            except HTTPException as e:
                dup = e.status_code
            return res, [(r.combo_name, r.combo_part, r.servings, r.my_food_id) for r in rows], dup

    res, rows, dup = asyncio.run(go())
    combo, items = res["combo"], res["items"]
    assert combo["role"] == "combo" and combo["calories"] == 390        # 40 + 280 + 0.5 × 140
    assert combo["source"] == "estimate"
    assert [i["created"] for i in items] == [True, True, False]
    assert items[2]["name"] == "Ranch" and items[2]["servings"] == 0.5
    assert [r[:2] for r in rows] == [("Taco salad", "base"), ("Taco salad", "topping"), ("Taco salad", "topping")]
    assert rows[2][2] == 0.5 and all(r[3] for r in rows)
    assert dup == 409


def test_one_food_is_not_a_combination(db_schema):
    from fastapi import HTTPException

    from app.database import AsyncSessionLocal
    from app.routers.my_foods import ComboFromItemsIn, combo_from_items

    async def go():
        async with AsyncSessionLocal() as db:
            await combo_from_items(body=ComboFromItemsIn(name="Two eggs", items=[
                {"name": "Egg", "calories": 70}, {"name": "egg", "calories": 70}]), db=db, _="owner")

    with pytest.raises(HTTPException) as e:
        asyncio.run(go())
    assert e.value.status_code == 422
