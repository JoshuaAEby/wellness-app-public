"""services/switch_play — a day's total is the counter's highest reading."""
from datetime import date

from datetime import datetime

from app.services.switch_play import daily_max, steps


def _p(t, v):
    return {"last_changed": t, "state": v}


def test_daily_max():
    got = daily_max([
        _p("2026-10-31T20:00:00+00:00", "50"),          # before the first day
        _p("2026-11-01T12:04:26+00:00", "0"), _p("2026-11-01T12:35:36+00:00", "4"),
        _p("2026-11-02T03:00:00+00:00", "40"),          # 22:00 EST, still 11-01
        _p("2026-11-02T03:30:00+00:00", "unavailable"),
        _p("2026-11-02T05:10:00+00:00", "0"),           # after local midnight: reset
        _p("2026-11-03T12:00:00+00:00", "0"),           # played nothing: a real zero
    ])
    assert got == {date(2026, 11, 1): 40.0, date(2026, 11, 2): 0.0, date(2026, 11, 3): 0.0}


def test_steps():
    got = steps([_p("2026-11-01T12:04:00+00:00", "0"), _p("2026-11-01T12:32:00+00:00", "4"),
                 _p("2026-11-01T12:35:00+00:00", "unavailable"), _p("2026-11-01T12:35:30+00:00", "4"),
                 _p("2026-11-01T12:49:00+00:00", "5")])
    assert got == [(datetime(2026, 11, 1, 12, 32), 4.0), (datetime(2026, 11, 1, 12, 49), 5.0)]
