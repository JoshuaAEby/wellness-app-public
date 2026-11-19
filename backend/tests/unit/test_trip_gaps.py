"""Note 102: trip days marked not logged read as unknown."""
import asyncio
from datetime import date, timedelta


def test_gap_dates_and_save(db_schema):
    from app.database import AsyncSessionLocal
    from app.services.trip_gaps import gap_dates, save_gaps

    async def go():
        async with AsyncSessionLocal() as db:
            await save_gaps(db, date(2026, 11, 6), date(2026, 11, 9), ["alcohol", "bogus", "water"])
            alc = await gap_dates(db, "alcohol", date(2026, 11, 1), date(2026, 11, 7))
            caf = await gap_dates(db, "caffeine", date(2026, 11, 1), date(2026, 11, 30))
            await save_gaps(db, date(2026, 11, 6), date(2026, 11, 9), [])     # "logged everything"
            after = await gap_dates(db, "alcohol", date(2026, 11, 1), date(2026, 11, 30))
            return alc, caf, after

    alc, caf, after = asyncio.run(go())
    assert alc == {date(2026, 11, 6), date(2026, 11, 7)}
    assert caf == set() and after == set()


def test_dry_streak_skips_unlogged_trip_days():
    from app.routers.streaks import _no_alcohol_streak
    today = date(2026, 11, 20)
    d = lambda n: date(2026, 11, n)
    drinks = {d(5)}
    plain = _no_alcohol_streak(drinks, d(1), today)
    held = _no_alcohol_streak(drinks, d(1), today, frozenset({d(6), d(7), d(8), d(9)}))
    assert plain["streak"] == 14 and held["streak"] == 10
    # A drink logged on a held day still breaks it.
    assert _no_alcohol_streak(drinks | {d(8)}, d(1), today, frozenset({d(8)}))["streak"] == 11


def test_alcohol_lever_leaves_gap_days_out():
    from app.services.levers import LEVERS, _alcohol_exposure
    days = [date(2026, 11, 1) + timedelta(days=i) for i in range(14)]
    by_day = {days[0]: 7.0, days[1]: None, days[2]: None}
    text, n = _alcohol_exposure(LEVERS["alcohol_sdu"], by_day, days)
    assert n == 12


def test_recovery_buffer_only_after_a_zone_shift(db_schema):
    from datetime import date as D
    from app.database import AsyncSessionLocal
    from app.models import TzPeriod
    from app.services.day_flags import trip_shifted_zones

    async def go():
        async with AsyncSessionLocal() as db:
            home_trip = await trip_shifted_zones(db, [D(2026, 3, 2), D(2026, 3, 3), D(2026, 3, 4)])
            db.add(TzPeriod(start_date=D(2026, 3, 10), end_date=D(2026, 3, 12), tz_name="Europe/Moscow",
                            source="manual", note="t"))
            db.add(TzPeriod(start_date=D(2026, 3, 20), end_date=D(2026, 3, 22), tz_name="America/Chicago",
                            source="manual", note="t"))
            await db.commit()
            far = await trip_shifted_zones(db, [D(2026, 3, 10), D(2026, 3, 11)])
            near = await trip_shifted_zones(db, [D(2026, 3, 20), D(2026, 3, 21)])
            return home_trip, far, near

    assert asyncio.run(go()) == (False, True, False)
