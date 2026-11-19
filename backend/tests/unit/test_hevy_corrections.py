"""Hevy fixes of 2026-11-16: one-dumbbell weights logged combined, and exercise renames."""
import asyncio
from datetime import datetime


def test_concentration_curl_halved_only_before_cutoff():
    from app.services.hevy_sync import _correct_weights
    ex = [{"exercise_template_id": "724CDE60", "sets": [{"weight_kg": 18.0, "reps": 13}]},
          {"exercise_template_id": "OTHER", "sets": [{"weight_kg": 20.0, "reps": 10}]}]
    old = _correct_weights(ex, datetime(2026, 11, 2, 12))
    assert old[0]["sets"][0]["weight_kg"] == 9.0 and old[1]["sets"][0]["weight_kg"] == 20.0
    assert old[0]["sets"][0]["reps"] == 26                         # 13 per arm → 26
    assert ex[0]["sets"][0]["weight_kg"] == 18.0                   # payload untouched
    new = _correct_weights(ex, datetime(2026, 11, 16, 12))
    assert new[0]["sets"][0]["weight_kg"] == 18.0


def test_rename_reaches_sets_and_aliases(db_schema):
    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.models import ClassAlias, HevyExerciseTemplate, HevySet
    from app.services import hevy_sync

    class Resp:
        def __init__(self, title): self._t = title
        def raise_for_status(self): pass
        def json(self): return {"title": self._t}

    class Client:
        async def get(self, url): return Resp("Floor Tricep Press" if "tpl1" in url else "Stock Name")

    async def go():
        hevy_sync.REQUEST_DELAY_S = 0
        async with AsyncSessionLocal() as db:
            db.add_all([
                HevyExerciseTemplate(id="tpl1", title="Tate Press (tricep)", is_custom=True),
                HevySet(hevy_workout_id="w1", exercise_title="Tate Press (tricep)", exercise_template_id="tpl1"),
                ClassAlias(key="tate press", title="Tate Press (tricep)", source="log"),
            ])
            await db.commit()
            renamed = await hevy_sync.refresh_template_titles(db, Client())
            t = (await db.execute(select(HevyExerciseTemplate.title).where(HevyExerciseTemplate.id == "tpl1"))).scalar()
            s = (await db.execute(select(HevySet.exercise_title).where(HevySet.hevy_workout_id == "w1"))).scalar()
            a = (await db.execute(select(ClassAlias.title).where(ClassAlias.key == "tate press"))).scalar()
            return renamed, t, s, a

    renamed, t, s, a = asyncio.run(go())
    assert renamed == [("Tate Press (tricep)", "Floor Tricep Press")]
    assert t == s == a == "Floor Tricep Press"


def test_kickback_is_one_dumbbell():
    from app.services.class_board import step_lb
    from app.services.hevy_sync import _correct_weights
    assert step_lb("Triceps Kickback (Dumbbell)") == 5.0
    assert step_lb("Bicep Curl (Dumbbell)") == 10.0
    ex = [{"exercise_template_id": "6127A3AD", "sets": [{"weight_kg": 18.0, "reps": 13}]}]
    s = _correct_weights(ex, datetime(2026, 11, 2))[0]["sets"][0]
    assert (s["weight_kg"], s["reps"]) == (9.0, 26)                # 13 per arm → 26
