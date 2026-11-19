"""Same dates a year apart (progress.year_over_year, 2026-11-01)."""
from datetime import date, timedelta

from app.services.progress import YOY_SHIFT, year_over_year

START, END = date(2026, 4, 1), date(2026, 8, 31)


def series(now, last):
    out = {}
    d = START
    while d <= END:
        out[d] = now
        out[d - YOY_SHIFT] = last
        d += timedelta(days=1)
    return out


def test_a_clear_fall_in_walking_hr_is_improved():
    out = year_over_year(series(100.0, 107.0), START, END, "down")
    assert out["change"] == -7.0 and out["verdict"] == "improved"
    assert (out["last_year"], out["now"]) == (107.0, 100.0)


def test_direction_follows_what_counts_as_better():
    assert year_over_year(series(100.0, 107.0), START, END, "up")["verdict"] == "worsened"
    assert year_over_year(series(38.0, 35.0), START, END, "up")["verdict"] == "improved"


def test_no_change_is_unclear():
    assert year_over_year(series(100.0, 100.0), START, END, "down")["verdict"] == "unclear"


def test_too_little_last_year_makes_no_claim():
    s = {d: v for d, v in series(100.0, 107.0).items() if d >= START}
    out = year_over_year(s, START, END, "down")
    assert out["verdict"] == "insufficient" and "change" not in out


def test_months_line_up_by_calendar_and_need_enough_days():
    out = year_over_year(series(100.0, 107.0), START, END, "down")
    months = {m["month"]: m for m in out["months"]}
    assert len(out["months"]) == 12 and out["months"][-1]["month"] == "Aug"
    assert months["Jun"]["this"] == 100.0 and months["Jun"]["last"] == 107.0
    assert months["Sep"]["this"] is None
