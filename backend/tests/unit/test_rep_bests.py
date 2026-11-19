"""Rep-range bests / PR tracker (services/strength.rep_bests_from_sets, 2026-10-29)."""
from datetime import date

from app.services.hevy_sync import epley_e1rm
from app.services.strength import E1RM_CARD_MAX_REPS, rank_e1rm_lifts, rep_bests_from_sets

D = date


def test_bests_start_values_and_prs():
    sets = {"Floor Press (Dumbbell)": [
        (D(2026, 8, 29), 90, 10), (D(2026, 9, 28), 80, 8), (D(2026, 10, 29), 90, 15),
        (D(2026, 10, 29), 80, 15)]}
    out = rep_bests_from_sets(sets, D(2026, 10, 1))
    b = {x["reps"]: x for x in out["lifts"][0]["brackets"]}
    assert b[10]["best_lb"] == 90 and b[10]["start_lb"] == 90           # 90×10 in August
    assert b[15]["best_lb"] == 90 and b[15]["start_lb"] is None         # first 15-rep set: no PR
    assert out["prs"] == []                                             # nothing BEATEN in window


def test_a_heavier_set_in_window_is_one_pr_listing_its_brackets():
    sets = {"Lateral Raise (Dumbbell)": [
        (D(2026, 9, 1), 30, 12), (D(2026, 9, 10), 30, 12), (D(2026, 10, 29), 40, 12)]}
    out = rep_bests_from_sets(sets, D(2026, 10, 1))
    assert len(out["prs"]) == 1
    pr = out["prs"][0]
    assert pr["lb"] == 40 and pr["brackets"] == [8, 10, 12] and pr["prev"][12] == 30


def test_new_lift_shows_and_recent_lifts_come_first():
    sets = {"Old Lift": [(D(2026, 5, 1), 50, 10)] * 3,
            "Bicep Curl (Dumbbell)": [(D(2026, 9, 1), 40, 10), (D(2026, 10, 20), 40, 10)],
            "Kettlebell Sumo Row": [(D(2026, 10, 29), 88, 15), (D(2026, 10, 29), 70, 10)]}
    out = rep_bests_from_sets(sets, D(2026, 10, 1))
    assert [l["title"] for l in out["lifts"]] == ["Kettlebell Sumo Row", "Bicep Curl (Dumbbell)"]
    row = out["lifts"][0]
    assert row["last_sets"][0] == {"lb": 88, "reps": 15}
    assert {b["reps"]: b["best_lb"] for b in row["brackets"]} == {8: 88, 10: 88, 12: 88, 15: 88}


def test_a_tie_is_recorded_as_matched_not_a_pr():
    sets = {"Lateral Raise (Dumbbell)": [(D(2026, 4, 6), 40, 10), (D(2026, 9, 1), 30, 10),
                                         (D(2026, 10, 29), 40, 10)]}
    out = rep_bests_from_sets(sets, D(2026, 10, 1))
    b10 = [b for b in out["lifts"][0]["brackets"] if b["reps"] == 10][0]
    assert b10["best_date"] == "2026-04-06" and b10["matched_date"] == "2026-10-29"
    assert out["prs"] == []


def _pts(*dates):
    return {str(i): {"date": d} for i, d in enumerate(dates)}


def test_e1rm_card_ranks_recent_lifts_first():
    best = {"old_staple": _pts("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"),
            "recent": _pts("2026-10-01", "2026-10-29"),
            "one_session": _pts("2026-10-30")}
    assert rank_e1rm_lifts(best, 5) == ["recent", "old_staple", "one_session"]
    assert rank_e1rm_lifts(best, 1) == ["recent"]


def test_e1rm_card_counts_15_reps_but_default_stays_10():
    # 90 lb-ish × 15 reps: the card sees it, the PR push's default does not.
    assert epley_e1rm(40.8, 15) is None
    assert epley_e1rm(40.8, 15, max_reps=E1RM_CARD_MAX_REPS) == 40.8 * 1.5
    assert epley_e1rm(40.8, 16, max_reps=E1RM_CARD_MAX_REPS) is None
