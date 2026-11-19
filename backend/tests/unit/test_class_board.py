"""Class-board targets (services/class_board.recommend_from_sessions, note 91).

Pure rule only — the vision parse is checked by hand against real board photos.
"""
from datetime import date

from app.services.class_board import carry_target, recommend_from_sessions, step_lb

TODAY = date(2026, 10, 29)


def test_double_progression_up_when_every_set_hit_target():
    s = {date(2026, 10, 1): [(40, 12), (40, 12), (40, 12)]}
    r = recommend_from_sessions("Bicep Curl (Dumbbell)", "weight_reps", s, 12, TODAY)
    assert r["status"] == "progress" and r["target_lb"] == 50   # pair step = 10 combined
    assert r["easy_lb"] == 40                                     # Easy day: no step up


def test_hold_when_a_set_fell_short():
    s = {date(2026, 10, 1): [(40, 12), (40, 10), (40, 9)]}
    r = recommend_from_sessions("Bicep Curl (Dumbbell)", "weight_reps", s, 12, TODAY)
    assert r["status"] == "hold" and r["target_lb"] == 40


def test_other_rep_range_never_below_last_working_weight():
    # 80×8 → Epley says 60 for 15 reps, but class sets are rarely near failure, and a real
    # session beat that estimate by a wide margin off exactly this history. The estimate is floored at the last working load.
    s = {date(2026, 10, 20): [(80, 8), (80, 8), (80, 8)]}
    r = recommend_from_sessions("Floor Press (Dumbbell)", "weight_reps", s, 15, TODAY)
    assert r["status"] == "best" and r["target_lb"] == 80 and "rule said 60" in r["text"]


def test_recent_best_beats_a_light_latest_session():
    # Lateral raise: 40×10 is the usual; the latest similar session was a light 30.
    s = {date(2026, 9, 17): [(40, 10), (40, 10)], date(2026, 9, 28): [(30, 8), (30, 10), (30, 10)]}
    r = recommend_from_sessions("Lateral Raise (Dumbbell)", "weight_reps", s, 10, TODAY)
    assert r["target_lb"] == 40 and r["basis"] == "recent_best" and r["easy_lb"] == 30


def test_best_older_than_window_is_ignored():
    s = {date(2026, 7, 1): [(90, 12)], date(2026, 10, 20): [(50, 12), (50, 12)]}
    r = recommend_from_sessions("Bent Over Row (Dumbbell)", "weight_reps", s, 12, TODAY)
    assert r["target_lb"] == 60      # +10 from the 10-20 anchor; the July 90 is outside 60 days


def test_anchor_prefers_session_at_similar_reps_over_latest():
    s = {date(2026, 9, 1): [(60, 15), (60, 15)], date(2026, 10, 20): [(80, 8), (80, 8)]}
    r = recommend_from_sessions("Floor Press (Dumbbell)", "weight_reps", s, 15, TODAY)
    assert r["basis"] == "anchor" and r["anchor_date"] == "2026-09-01" and r["target_lb"] == 70


def test_assisted_moves_to_next_lighter_band():
    s = {date(2026, 9, 1): [(90, 8), (90, 8)],
         date(2026, 10, 1): [(60, 8), (60, 9), (90, 10)]}
    r = recommend_from_sessions("Chin Up (Assisted)", "bodyweight_assisted", s, None, TODAY)
    assert r["status"] == "progress" and r["target_lb"] == 0      # no lighter band used → unassisted


def test_assisted_holds_below_goal():
    s = {date(2026, 10, 1): [(60, 4), (60, 3), (90, 5)]}
    r = recommend_from_sessions("Chin Up (Assisted)", "bodyweight_assisted", s, None, TODAY)
    assert r["status"] == "hold" and r["target_lb"] == 60


def test_bodyweight_and_unknown_history():
    s = {date(2026, 10, 24): [(0, 16), (0, 22)]}
    assert recommend_from_sessions("Skater Jump", "reps_only", s, None, TODAY)["status"] == "info"
    assert recommend_from_sessions("X", "weight_reps", {}, 10, TODAY)["status"] == "no_history"


def test_medicine_ball_steps_to_a_ball_that_exists():
    # 10-27: weighted sit-up at 40 on 10/2 → the old rule said 45; with the rack's sizes
    # and the 10-27 session (30 × 15 ×3) it is the next ball up.
    sizes = (15.0, 20.0, 25.0, 30.0, 35.0, 40.0)
    s = {date(2026, 10, 27): [(30, 15), (30, 15), (30, 15)]}
    r = recommend_from_sessions("Sit Up (Weighted)", "bodyweight_weighted", s, 15, TODAY, sizes)
    assert r["status"] == "progress" and r["target_lb"] == 35
    top = {date(2026, 10, 27): [(40, 15), (40, 15)]}
    r = recommend_from_sessions("Sit Up (Weighted)", "bodyweight_weighted", top, 15, TODAY, sizes)
    assert r["status"] == "hold" and r["target_lb"] == 40       # nothing heavier exists


def test_carry_owns_top_load_before_adding():
    one = {date(2026, 10, 27): [(80, 75, None), (90, 75, None), (100, 75, None)]}
    r = carry_target("Farmers Walk", one)
    assert r["status"] == "hold" and r["target_lb"] == 100 and r["easy_lb"] == 100
    two = {date(2026, 10, 27): [(100, 75, None), (100, 75, None)]}
    r = carry_target("Farmers Walk", two)
    assert r["status"] == "progress" and r["target_lb"] == 110   # pair: +10 combined
    assert carry_target("Farmers Walk", {})["status"] == "no_history"


def test_step_single_implement():
    assert step_lb("Pullover (Dumbbell)") == 5
    assert step_lb("Hammer Curl (Dumbbell)") == 10
    assert step_lb("Kettlebell Swing") == 5
