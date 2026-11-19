"""Fixed-window early-night HR (sleep_processing.compute_sleep_hr_early_fixed, 2026-10-27).

The point of the variant: for the SAME physiology, the thirds-based sleep_hr_elevation
moves with night length, the fixed window does not.
"""
import asyncio
from datetime import date, datetime, timedelta

from sqlalchemy import text

from app.services.sleep_processing import early_minus_floor


def test_early_minus_floor_basics():
    night = [50.0] * 10 + [60.0] * 90           # p10 rule → 50
    assert early_minus_floor([62.0, 64.0, 66.0, 68.0, 70.0], night) == 16.0
    assert early_minus_floor([70.0] * 4, night) is None          # thin early window
    assert early_minus_floor([70.0] * 5, [60.0] * 19) is None    # thin night


def _hr(minute: float) -> float:
    """Same physiology every night: 15 bpm above floor at onset, decaying over ~90 min,
    a flat 52 floor, then a pre-wake rise in the final hour handled by the caller."""
    return 52.0 + 15.0 * max(0.0, 1.0 - minute / 90.0)


def _run(nights: list[tuple[date, float]]):
    from app.database import AsyncSessionLocal
    from app.services.sleep_processing import (
        compute_sleep_hr_early_fixed, compute_sleep_hr_elevation)

    async def go():
        async with AsyncSessionLocal() as db:
            for t in ("sleep_sessions", "health_metrics"):
                await db.execute(text(f"DELETE FROM {t}"))
            for wake_day, hours in nights:
                onset = datetime.combine(wake_day, datetime.min.time()) - timedelta(hours=2)
                wake = onset + timedelta(hours=hours)
                await db.execute(text(
                    "INSERT INTO sleep_sessions (date, bedtime, wake_time, total_min, source) "
                    "VALUES (:d, :b, :w, :m, 'test')"),
                    {"d": wake_day, "b": onset, "w": wake, "m": int(hours * 60)})
                for i in range(int(hours * 12)):              # one sample every 5 min
                    m = i * 5
                    hr = _hr(m) + (6.0 if m >= hours * 60 - 60 else 0.0)   # pre-wake rise
                    await db.execute(text(
                        "INSERT INTO health_metrics (metric_type, timestamp, value, unit, source) "
                        "VALUES ('heart_rate', :t, :v, 'bpm', 'test')"),
                        {"t": (onset + timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S"), "v": hr})
            await db.commit()
            days = [d for d, _ in nights]
            await compute_sleep_hr_elevation(db, days)
            await compute_sleep_hr_early_fixed(db, days)
            rows = (await db.execute(text(
                "SELECT metric_type, value FROM health_metrics WHERE source = 'derived' "
                "ORDER BY timestamp"))).all()
            return rows
    return asyncio.run(go())


def test_fixed_window_ignores_night_length_where_thirds_do_not(db_schema):
    rows = _run([(date(2026, 9, 1), 6.0), (date(2026, 9, 3), 9.0)])
    fixed = [v for k, v in rows if k == "sleep_hr_early_fixed"]
    thirds = [v for k, v in rows if k == "sleep_hr_elevation"]
    assert len(fixed) == 2 and len(thirds) == 2
    assert fixed[0] == fixed[1]                     # same physiology → same value
    # Thirds-based value moved with length alone (this profile: 1.5 → 2.2 bpm).
    assert abs(thirds[0] - thirds[1]) >= 0.5


def test_short_night_is_skipped(db_schema):
    rows = _run([(date(2026, 9, 5), 2.5)])
    assert not [r for r in rows if r[0] == "sleep_hr_early_fixed"]
