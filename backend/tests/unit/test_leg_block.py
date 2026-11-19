"""Rho's leg block line (coach_plan.leg_line, 2026-11-01)."""
from datetime import date, timedelta

from app.services.coach_plan import (LEG_DORMANT_DAYS, LEG_START, due_home_blocks, is_home_block,
                                     leg_line)

SUNDAY = date(2026, 11, 8)
MONDAY = date(2026, 11, 9)


def line(day=SUNDAY, blocks=(), week=0, sets=12.0, fits=True, unwell=False, done=False):
    return leg_line(day, list(blocks), week, sets, fits, unwell, done)


def test_titles_keep_the_two_blocks_apart():
    assert is_home_block("HOME – Pull") and not is_home_block("HOME – Pull", legs=True)
    assert is_home_block("home legs", legs=True) and not is_home_block("Home – Legs")
    assert not is_home_block("Leg day", legs=True)
    assert not is_home_block(None)


def test_due_on_a_day_it_fits_with_the_moves_and_the_title():
    out = line()
    assert out["due"] and "HOME – Legs" in out["detail"] and "Bulgarian" in out["detail"]
    assert "12 building sets" in out["detail"]


def test_quiet_when_it_does_not_fit_or_when_unwell():
    assert line(fits=False) is None
    assert line(unwell=True) is None


def test_one_a_week_then_quiet():
    assert line(week=1, blocks=[SUNDAY - timedelta(days=1)]) is None


def test_done_today_is_credited():
    out = line(done=True, week=1, blocks=[SUNDAY])
    assert out["done"] and not out["due"]


def test_ignored_for_two_weeks_it_asks_once_on_mondays():
    day = LEG_START + timedelta(days=LEG_DORMANT_DAYS + 1)   # a Monday
    assert day.weekday() == 0
    out = line(day=day)
    assert out["dormant"] and not out["due"]
    assert line(day=day + timedelta(days=1)) is None


def test_a_recent_block_keeps_it_offering():
    day = LEG_START + timedelta(days=LEG_DORMANT_DAYS + 3)
    assert line(day=day, blocks=[day - timedelta(days=8)])["due"]


def test_briefing_gets_only_blocks_that_are_due():
    assert due_home_blocks({"legs": {"due": True}, "pull": None}) == ["leg block"]
    assert due_home_blocks({"legs": {"due": True}, "pull": {"due": True}}) == ["leg block", "pull block"]
    assert due_home_blocks({"legs": {"due": False, "dormant": True}, "pull": {"due": False}}) == []
    assert due_home_blocks({"label": "CLASS"}) == []   # an experiment day has no block lines
    assert due_home_blocks(None) == []
