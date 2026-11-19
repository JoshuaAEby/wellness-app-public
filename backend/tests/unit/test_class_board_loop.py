"""Class board loop: wording keys, pairing board lines with logged titles, verdicts (0085)."""
from app.services.class_board import alias_key, pair_unclaimed, verdict


def test_alias_key_strips_numbering_reps_and_plurals():
    assert alias_key("4. KB Sumo Rows x15") == "kb sumo row"
    assert alias_key("2. Wall Bicep Curls x 5-5-5") == "wall bicep curl"
    assert alias_key("7) Chin UPS x MAX") == "chin up"
    assert alias_key("Farmers Carry x 3 laps") == "farmer carry"


def test_pairing_by_shared_words_each_title_once():
    items = [{"name": "Kettlebell Sumo Squat High Pull"},
             {"name": "Floor Glute Bridge Tricep Press"},
             {"name": "Stability Ball Stir the Pot"}]
    logged = ["Kettlebell High Pull", "Glute Bridge Tricep Press", "Stability Ball Stir the Pot",
              "Side Plank"]
    got = pair_unclaimed(items, logged)
    assert got == {0: "Kettlebell High Pull", 1: "Glute Bridge Tricep Press",
                   2: "Stability Ball Stir the Pot"}


def test_one_shared_word_is_not_enough():
    assert pair_unclaimed([{"name": "Skater Hops"}], ["Box Hops Lateral"]) == {}


def test_verdicts():
    rec = {"target_lb": 80, "unit": "lb"}
    assert verdict(rec, [(80, 15), (90, 15), (90, 15)])["verdict"] == "beat"
    assert verdict(rec, [(80, 15), (80, 15)])["verdict"] == "hit"
    assert verdict(rec, [(70, 15), (70, 15)])["verdict"] == "under"
    band = {"target_lb": 60, "unit": "lb assist"}
    assert verdict(band, [(60, 5), (60, 5)])["verdict"] == "hit"
    assert verdict(band, [(40, 5), (40, 5)])["verdict"] == "beat"      # lighter band = better
    assert verdict({}, [(0, 10)])["verdict"] == "logged"
    assert verdict(rec, [])["verdict"] == "not_logged"


def test_gym_today(db_schema):
    """Row hides on a Hevy session or an Apple gym session, not on a walk or another day."""
    import asyncio
    from datetime import timedelta

    import httpx
    from fastapi import FastAPI

    from app.auth import get_current_user
    from app.database import AsyncSessionLocal
    from app.models import HevyWorkout, Workout
    from app.routers import class_board
    from app.timeutils import local_day_bounds_utc, local_today

    app = FastAPI()
    app.include_router(class_board.router)
    app.dependency_overrides[get_current_user] = lambda: "owner"
    noon = local_day_bounds_utc(local_today())[0] + timedelta(hours=12)

    async def add(row):
        async with AsyncSessionLocal() as db:
            db.add(row)
            await db.commit()

    async def done(c):
        return (await c.get("/api/class-board/gym-today")).json()["done"]

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as c:
            assert not await done(c)
            await add(Workout(apple_uuid="walk", activity_type="Outdoor Walk", start_time=noon,
                              end_time=noon))
            await add(Workout(apple_uuid="old", activity_type="High Intensity Interval Training",
                              start_time=noon - timedelta(days=1), end_time=noon))
            await add(HevyWorkout(id="old", title="x", start_time=noon - timedelta(days=1)))
            assert not await done(c)
            await add(Workout(apple_uuid="hiit", activity_type="High Intensity Interval Training",
                              start_time=noon, end_time=noon))
            assert await done(c)
            async with AsyncSessionLocal() as db:
                row = (await db.execute(Workout.__table__.select().where(
                    Workout.apple_uuid == "hiit"))).first()
                await db.delete(await db.get(Workout, row.id))
                await db.commit()
            assert not await done(c)
            await add(HevyWorkout(id="now", title="x", start_time=noon))
            assert await done(c)
    asyncio.run(go())



def test_board_read_75_min_ago_counts_as_class_done():
    from datetime import datetime, timedelta
    from app.routers.class_board import board_says_done
    now = datetime(2026, 11, 10, 12, 0)
    assert board_says_done(None, now) is False
    assert board_says_done(now - timedelta(minutes=10), now) is False
    assert board_says_done(now - timedelta(minutes=75), now) is True
