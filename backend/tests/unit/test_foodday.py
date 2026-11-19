"""The food day — 04:00 → 04:00 local (app/foodday.py, 2026-10-21)."""
import sqlite3
from datetime import date, datetime, time, timedelta

from sqlalchemy import Column, DateTime, Integer, MetaData, Table, select

from app import foodday as fd
from app import timeutils as tu


def test_food_day_boundary():
    assert fd.food_day(datetime(2026, 10, 21, 1, 30)) == date(2026, 10, 20)   # after midnight
    assert fd.food_day(datetime(2026, 10, 21, 3, 59, 59)) == date(2026, 10, 20)
    assert fd.food_day(datetime(2026, 10, 21, 4, 0)) == date(2026, 10, 21)    # boundary is the new day
    assert fd.food_day(datetime(2026, 10, 21, 23, 59)) == date(2026, 10, 21)


def test_timestamp_round_trips_to_its_food_day():
    d = date(2026, 10, 20)
    for minutes in range(0, 24 * 60, 7):
        t = time(minutes // 60, minutes % 60)
        ts = fd.food_timestamp(d, t)
        assert fd.food_day(ts) == d, t                      # every clock time stays on day d
        assert fd.food_day_start(d) <= ts < fd.food_day_end(d)
    assert fd.food_timestamp(d, time(1, 30)) == datetime(2026, 10, 21, 1, 30)   # real moment
    assert fd.food_timestamp(d, time(8, 0)) == datetime(2026, 10, 20, 8, 0)


def test_sql_matches_python():
    """food_date_sql must bucket exactly like food_day, edges included, at the storage
    format food_log uses (26-char, microseconds)."""
    md = MetaData()
    t = Table("f", md, Column("id", Integer, primary_key=True), Column("ts", DateTime))
    con = sqlite3.connect(":memory:")
    con.execute("create table f (id integer primary key, ts text)")
    base = datetime(2026, 10, 19)
    stamps = [base + timedelta(minutes=m) for m in range(0, 4 * 24 * 60, 13)]
    stamps += [datetime(2026, 10, 20, 4, 0), datetime(2026, 10, 20, 3, 59, 59), datetime(2026, 10, 21, 0, 0)]
    con.executemany("insert into f (ts) values (?)", [(s.strftime("%Y-%m-%d %H:%M:%S.%f"),) for s in stamps])
    q = select(t.c.ts, fd.food_date_sql(t.c.ts)).compile(compile_kwargs={"literal_binds": True})
    for raw, d in con.execute(str(q)):
        assert date.fromisoformat(d) == fd.food_day(datetime.fromisoformat(raw)), raw


def test_utc_bounds_are_local_four_am_across_dst():
    # 2026-11-01 is the US fall-back day: 04:00 EDT the day before, 04:00 EST after.
    a, b = fd.food_day_bounds_utc(date(2026, 10, 31))
    assert a == datetime(2026, 10, 31, 8, 0)        # 04:00 EDT = 08:00 UTC
    assert b == datetime(2026, 11, 1, 9, 0)         # 04:00 EST = 09:00 UTC (a 25 h day)
    tu.set_segments([])


def test_alcohol_food_day_sql_matches_python_with_and_without_segments():
    """local_food_date_sql (SQL) == food_day_of_utc (Python) for naive-UTC rows, at home
    and inside an away segment — the alcohol re-key (2026-10-21) depends on it."""
    con = sqlite3.connect(":memory:")
    con.execute("create table h (ts text)")
    start = datetime(2026, 9, 4)
    stamps = [start + timedelta(minutes=m) for m in range(0, 18 * 24 * 60, 17)]
    con.executemany("insert into h values (?)", [(s.strftime("%Y-%m-%d %H:%M:%S"),) for s in stamps])
    md = MetaData()
    h = Table("h", md, Column("ts", DateTime))
    away = tu.AwaySegment(datetime(2026, 9, 7, 21), datetime(2026, 9, 18, 21), 180, "Europe/Moscow")
    for segs in ([], [away]):
        tu.set_segments(segs)
        q = select(h.c.ts, tu.local_food_date_sql(h.c.ts)).compile(compile_kwargs={"literal_binds": True})
        for raw, d in con.execute(str(q)):
            assert date.fromisoformat(d) == fd.food_day_of_utc(datetime.fromisoformat(raw)), (segs, raw)
    tu.set_segments([])
    # 00:30 EDT on the 21st (= 04:30 UTC) is a drink on the night of the 20th
    assert fd.food_day_of_utc(datetime(2026, 10, 21, 4, 30)) == date(2026, 10, 20)
