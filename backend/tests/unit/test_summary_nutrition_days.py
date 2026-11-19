"""Summary nutrition averages leave out incomplete-log days; trajectory weeks are dated (note 104)."""
from datetime import date, datetime
from types import SimpleNamespace as NS

from app.services.ai_summaries import _month_agg_str
from app.services.summary_context import _week_label


def _food(day: int, hour: int, kcal: float, prot: float):
    return NS(timestamp=datetime(2026, 10, day, hour, 0, 0), calories=kcal, protein_g=prot)


DAILY = [NS(readiness_score=None, hrv_z_score=None, ctl=None)]


def test_month_agg_leaves_out_incomplete_days():
    food = [_food(1, 12, 2600, 140), _food(2, 12, 2400, 130), _food(3, 12, 400, 20)]
    out = _month_agg_str("Oct 2026", DAILY, [], food, {date(2026, 10, 3)})
    assert "avg 2500 kcal/135g prot over 2 complete-log days (1 incomplete left out)" in out


def test_month_agg_without_exclusions_is_unchanged_in_value():
    food = [_food(1, 12, 2600, 140), _food(2, 12, 2400, 130)]
    out = _month_agg_str("Oct 2026", DAILY, [], food)
    assert "avg 2500 kcal/135g prot over 2 complete-log days" in out
    assert "left out" not in out


def test_month_agg_all_days_incomplete_gives_no_average():
    out = _month_agg_str("Oct 2026", DAILY, [], [_food(3, 12, 400, 20)], {date(2026, 10, 3)})
    assert "nutrition" not in out


def test_week_labels():
    assert _week_label(date(2026, 11, 2), date(2026, 11, 8)) == "Nov 2–8"
    assert _week_label(date(2026, 10, 27), date(2026, 11, 2)) == "Oct 27–Nov 2"
    assert _week_label(date(2026, 11, 15), date(2026, 11, 15)) == "Nov 15"
