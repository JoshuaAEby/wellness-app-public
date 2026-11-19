"""timeutils away segments — one instant→local-day mapping for Python and SQL (tz phase 3)."""
from datetime import date, datetime, timedelta

from app import timeutils as tu

AWAY = tu.AwaySegment(datetime(2026, 9, 7, 21), datetime(2026, 9, 18, 21), 180, "Europe/Moscow")


def setup_function(_):
    tu.set_segments([AWAY])


def teardown_function(_):
    tu.set_segments([])


def test_days_contiguous_and_monotonic():
    prev = None
    for i in range(16):
        d = date(2026, 9, 4) + timedelta(days=i)
        a, b = tu.local_day_bounds_utc(d)
        assert b > a
        if prev is not None:
            assert a == prev, d                     # no gap, no overlap
        prev = b
    # flying east: departure day short; flying west: arrival day long
    a, b = tu.local_day_bounds_utc(date(2026, 9, 7))
    assert b - a == timedelta(hours=17)
    a, b = tu.local_day_bounds_utc(date(2026, 9, 18))
    assert b - a == timedelta(hours=31)


def test_instants_map_inside_their_own_day():
    t = datetime(2026, 9, 7, 20)
    while t < datetime(2026, 9, 20):
        d = tu.to_local_date(t)
        a, b = tu.local_day_bounds_utc(d)
        assert a <= t < b, t
        t += timedelta(minutes=37)


def test_local_time_abroad_and_home():
    assert tu.to_local(datetime(2026, 9, 11, 19, 22)).strftime("%H:%M") == "22:22"
    assert tu.to_local(datetime(2026, 9, 2, 1, 45)).strftime("%H:%M") == "21:45"   # EDT
    assert getattr(tu.zone_for_date(date(2026, 9, 25)), "key", None) == "America/New_York"


def test_fast_path_without_segments():
    """No segments: SQL bucketing is exactly the old 'localtime' expression (no CASE)."""
    from app.models import HealthMetric
    tu.set_segments([])
    sql = str(tu.local_date_sql(HealthMetric.timestamp).compile())
    assert "CASE" not in sql and "date(" in sql
    a, b = tu.local_day_bounds_utc(date(2026, 9, 12))
    assert b - a == timedelta(hours=24)
    tu.set_segments([AWAY])
    assert "CASE" in str(tu.local_date_sql(HealthMetric.timestamp).compile())
