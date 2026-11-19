"""Build the demo database: python -m demo.seed [path/to/demo.db]

Creates a fresh SQLite database with the Alembic baseline, writes one year of the
synthetic person in demo/model.py through the app's own ingestion functions and intake
services, then runs the same pipeline production runs: daily-metric recompute, post-trip
recovery flags, the correlation refresh and the game. Nothing is written into derived
tables by hand, so every chart and finding in the demo comes from the real code.

Deterministic for a given end date: the same day always produces the same database.
Data ends at the current moment, so the Today tab shows this morning's numbers.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent

UNITS = {"heart_rate": "count/min", "heart_rate_variability": "ms", "respiratory_rate": "count/min",
         "apple_sleeping_wrist_temperature": "degC", "step_count": "count", "active_energy": "kcal",
         "basal_energy_burned": "kcal", "apple_stand_hour": "count", "apple_exercise_time": "min",
         "time_in_daylight": "min", "weight_body_mass": "lb", "vo2_max": "ml/(kg·min)",
         "walking_heart_rate_average": "count/min", "walking_running_distance": "mi"}


def _prepare(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
    env = dict(os.environ, DATABASE_URL=f"sqlite+aiosqlite:///{db_path}")
    # The `alembic` console script, not `python -m alembic`: with backend/ as the working
    # directory, -m would import the local alembic/ folder instead of the package.
    env.pop("PYTHONPATH", None)
    exe = shutil.which("alembic") or str(Path(sys.executable).with_name("alembic"))
    subprocess.run([exe, "upgrade", "head"], cwd=BACKEND, env=env, check=True, capture_output=True)


def _utc_naive(dt_aware: datetime) -> datetime:
    return dt_aware.astimezone(timezone.utc).replace(tzinfo=None)


async def seed(db_path: Path) -> None:
    from demo.model import HOME_TZ, generate
    from app.database import AsyncSessionLocal
    from app.models import DayFlag, FoodLog, HabitLog, SubjectiveLog, TzPeriod, UserSettings
    from app.services import caffeine as caffeine_svc
    from app.services import intake
    from app.services.correlations import refresh_correlation_cache
    from app.services.day_flags import sync_travel_recovery
    from app.services.game import recompute_and_store
    from app.services.ingestion import (METRIC_NAME_MAP, _ingest_scalar_metric, _ingest_sleep,
                                        _ingest_workouts)
    from app.services.metrics_engine import recompute_dates
    from app.services.tz import refresh_current_zone

    now = datetime.now(ZoneInfo(HOME_TZ))
    w = generate(now.date())
    now_utc = _utc_naive(now)
    t0 = _time.monotonic()

    def past(hae_str: str) -> bool:
        return datetime.strptime(hae_str, "%Y-%m-%d %H:%M:%S %z").astimezone(timezone.utc).replace(tzinfo=None) <= now_utc

    async with AsyncSessionLocal() as db:
        db.add(UserSettings(target_sleep_hours=7.5, max_hr=186, training_goal="building",
                            steps_goal=8000, move_goal=450, exercise_goal=30, ai_enabled=False,
                            calorie_goal_mode="balance", weight_change_goal_lbs_per_week=0.0,
                            height_cm=168.0, birth_year=now.year - 36, biological_sex="female",
                            last_webhook_at=now_utc))
        for a, b, tz in w.tz_periods:
            db.add(TzPeriod(start_date=a, end_date=b, tz_name=tz, source="manual", note="demo trip"))
        for d, reason, source in w.flags:
            db.add(DayFlag(date=d, reason=reason, source=source, note="demo"))
        await db.commit()
        await refresh_current_zone(db)

        # ── Apple Health, through the ingestion functions the webhook uses ─────
        seen: set = set()
        n = await _ingest_sleep(db, w.sleep, seen)
        n += await _ingest_workouts(db, [x for x in w.workouts if past(x["end"])], seen)
        for name, samples in w.metrics.items():
            n += await _ingest_scalar_metric(db, METRIC_NAME_MAP[name], UNITS.get(name),
                                             [s for s in samples if past(s["date"])], seen)
        await db.commit()
        print(f"ingested {n:,} Apple Health records ({_time.monotonic() - t0:.0f} s)")

        # ── Manual entries, through the intake services where one exists ───────
        for c in w.checkins:
            db.add(SubjectiveLog(date=c["date"], energy=c["energy"], mood=c["mood"], stress=c["stress"],
                                 sleep_quality=c["sleep_quality"], sick=c["sick"],
                                 day_score=c["day_score"] if c["date"] < now.date() else None))
        for h in w.habits:
            db.add(HabitLog(**h))
        for when, meal, name, kcal, p, c, fat, fib in w.food:
            if when > now.replace(tzinfo=None):
                continue
            db.add(FoodLog(timestamp=when, name=name, description=name, meal_type=meal, calories=kcal,
                           protein_g=p, carbs_g=c, fat_g=fat, fiber_g=fib, confidence="high"))
        await db.commit()
        for day, at, mg in w.caffeine:
            if at <= now:
                await caffeine_svc.add_instance(db, day, "custom", mg, 1.0, _utc_naive(at))
        for day, hhmm, sdu, label, kcal in w.alcohol:
            row = await intake.log_alcohol(db, day, sdu, hhmm)
            if row.timestamp > now_utc:
                await db.delete(row)
                continue
            hh, mm = (int(x) for x in hhmm.split(":"))
            local = datetime.combine(day + timedelta(days=1 if hh < 4 else 0), datetime.min.time()) \
                + timedelta(hours=hh, minutes=mm)
            db.add(FoodLog(timestamp=local, name=label, description=label, meal_type="drinks",
                           calories=kcal, protein_g=1.0, carbs_g=round(kcal * 0.04, 1), fat_g=0.0,
                           confidence="high", source_kind="alcohol", source_id=row.id))
        for day, hhmm, oz in w.water:
            row = await intake.log_water(db, day, oz, hhmm)
            if row.timestamp > now_utc:
                await db.delete(row)
        await db.commit()
        print(f"logged manual entries ({_time.monotonic() - t0:.0f} s)")

        # ── The production pipeline ─────────────────────────────────────────────
        trip_end = max(d for d, r, _ in w.flags if r == "travel")
        await sync_travel_recovery(db, trip_end + timedelta(days=2))
        await refresh_current_zone(db)
        dates = [w.start + timedelta(days=i) for i in range((w.end - w.start).days + 1)]
        await recompute_dates(db, dates)
        print(f"recomputed {len(dates)} days ({_time.monotonic() - t0:.0f} s)")

    await refresh_correlation_cache(365)
    print(f"correlation refresh done ({_time.monotonic() - t0:.0f} s)")
    async with AsyncSessionLocal() as db:
        await recompute_and_store(db)
        try:
            from app.services.energy import maybe_refit_activity_factor
            await maybe_refit_activity_factor(db, force=True)
        except Exception as exc:                       # calibration is optional for the demo
            print(f"energy calibration skipped: {exc}")
    print(f"done in {_time.monotonic() - t0:.0f} s")


def main() -> None:
    db_path = Path(sys.argv[1] if len(sys.argv) > 1 else BACKEND / "data" / "demo.db").resolve()
    _prepare(db_path)
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    os.environ.setdefault("NTFY_URL", "")
    sys.path.insert(0, str(BACKEND))
    asyncio.run(seed(db_path))
    print(f"demo database: {db_path}")


if __name__ == "__main__":
    main()
