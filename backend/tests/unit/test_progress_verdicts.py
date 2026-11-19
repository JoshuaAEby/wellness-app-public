"""Progress verdicts (2026-11-02): last year decides, strength at equal reps."""
import random
from datetime import date, timedelta

from app.services.progress import (
    cardio_headline, combined_verdict, past_best, rep_adjusted_rate, rep_matched_lifts, strength_headline, top_sets,
    year_over_year, YOY_SHIFT,
)

START = date(2026, 4, 1)
EARLY = (date(2026, 4, 2), date(2026, 5, 31))
RECENT = (date(2026, 8, 3), date(2026, 11, 1))


def test_better_than_before_training_but_not_last_year_is_a_rebound():
    assert combined_verdict("improved", "unclear") == ("rebound", "last_year")


def test_last_year_decides_when_it_exists():
    assert combined_verdict("unclear", "improved") == ("improved", "last_year")
    assert combined_verdict("improved", "worsened") == ("worsened", "last_year")
    assert combined_verdict("unclear", "unclear") == ("unclear", "last_year")


def test_without_last_year_the_old_verdict_stands():
    assert combined_verdict("improved", "insufficient") == ("improved", "before")
    assert combined_verdict("improved", None) == ("improved", "before")


def _daily(now, last):
    out, d = {}, START
    while d <= date(2026, 8, 31):
        out[d], out[d - YOY_SHIFT] = now, last
        d += timedelta(days=1)
    return out


def test_a_gain_inside_the_past_range_is_not_an_improvement():
    end = date(2026, 8, 31)
    assert year_over_year(_daily(36.6, 34.7), START, end, "up", prior_best=36.9)["verdict"] == "within_range"
    assert year_over_year(_daily(37.6, 34.7), START, end, "up", prior_best=36.9)["verdict"] == "improved"


def test_past_best_needs_enough_days_and_stops_at_the_date():
    s = {date(2025, 3, d): 38.0 for d in (1, 2, 3)}
    s |= {date(2025, 12, 8): 40.0, date(2025, 12, 26): 40.0}      # two days: not a month
    s |= {date(2026, 5, d): 45.0 for d in (1, 2, 3)}              # after training began
    assert past_best(s, date(2026, 4, 11), "up") == 38.0
    assert past_best({}, date(2026, 4, 11), "up") is None


def _sessions(monthly, lifts=6, sessions=12, seed=1):
    """Each lift's load grows `monthly` per month; reps wobble and heavier reps cost load."""
    rng, rows = random.Random(seed), []
    for i in range(lifts):
        for k in range(sessions):
            day = date(2026, 4, 2) + timedelta(days=15 * k + i)
            reps = rng.choice((8, 10, 12, 15))
            months = (day - date(2026, 4, 2)).days / 30.44
            lb = (40 + 10 * i) * (1 + monthly) ** months * (1 - 0.02 * (reps - 10)) * rng.uniform(.97, 1.03)
            rows.append((f"lift{i}", day, lb, reps))
    return rows


def test_rate_recovers_a_known_gain_despite_changing_reps():
    out = rep_adjusted_rate(_sessions(0.05))
    assert out["verdict"] == "improved" and 4 < out["rate"] < 6
    assert out["ci_low"] > 0 and out["n_lifts"] == 6


def test_a_flat_series_is_not_called_a_gain():
    out = rep_adjusted_rate(_sessions(0.0))
    assert out["verdict"] == "unclear" and abs(out["rate"]) < 1


def test_rate_is_repeatable_on_the_same_data():
    rows = _sessions(0.02)
    assert rep_adjusted_rate(rows) == rep_adjusted_rate(rows)


def test_too_few_rows_makes_no_claim():
    out = rep_adjusted_rate(_sessions(0.05)[:8])
    assert out["verdict"] == "insufficient" and out["rate"] is None


def test_top_set_is_best_epley_and_rare_lifts_drop_out():
    a, b, c = date(2026, 5, 1), date(2026, 5, 8), date(2026, 5, 15)
    sets = [("Row", a, 40, 15), ("Row", a, 50, 5), ("Row", a, 100, 25),     # 25 reps: beyond the cap
            ("Row", b, 40, 10), ("Row", c, 45, 10), ("Once", a, 90, 10)]
    out = top_sets(sets)
    assert ("Row", a, 40, 15) in out and len(out) == 3
    assert all(r[0] != "Once" for r in out)


def _sess(*rows):
    return [(date(2026, m, d), lb, reps) for m, d, lb, reps in rows]


def test_same_reps_compared_and_low_rep_sets_ignored():
    sets = {("Row", False): _sess((4, 13, 40, 12), (5, 5, 37.5, 12), (8, 4, 70, 12), (9, 22, 110, 3), (10, 23, 50, 12))}
    r = rep_matched_lifts(sets, EARLY, RECENT)[0]
    assert (r["early_lb"], r["recent_lb"], r["verdict"]) == (40, 70, "up")          # 110 × 3 doesn't count
    assert r["change_pct"] == 75


def test_one_session_in_a_window_is_not_comparable():
    sets = {("Lunge", False): _sess((4, 13, 20, 16), (9, 1, 50, 10), (9, 20, 50, 12))}
    assert rep_matched_lifts(sets, EARLY, RECENT) == []


def test_unchanged_and_down_are_labelled():
    sets = {("Curl", False): _sess((4, 15, 50, 10), (5, 7, 50, 12), (9, 24, 50, 10), (10, 22, 40, 15)),
            ("Hammer", False): _sess((4, 15, 50, 10), (5, 15, 40, 10), (9, 17, 40, 10), (10, 22, 40, 12))}
    out = {r["exercise"]: r for r in rep_matched_lifts(sets, EARLY, RECENT)}
    assert out["Curl"]["verdict"] == "same" and out["Hammer"]["verdict"] == "down"


def test_assisted_lifts_count_less_help_as_better():
    sets = {("Pull Up (Band)", True): _sess((4, 13, 90, 10), (4, 27, 90, 8), (9, 14, 60, 5), (9, 22, 150, 5))}
    r = rep_matched_lifts(sets, EARLY, RECENT)[0]
    assert (r["early_lb"], r["recent_lb"], r["verdict"]) == (90, 60, "up") and r["reps"] == 5


def _strength(overall, recent, overall_rate=3.6, recent_rate=0.0):
    return {"from": "2026-04-02",
            "overall": {"verdict": overall, "rate": overall_rate},
            "recent_rate": {"verdict": recent, "rate": recent_rate, "days": 150},
            "groups": [{"label": "Chest and back", "verdict": "improved"},
                       {"label": "Arms and shoulders", "verdict": "unclear"}]}


def test_early_gains_with_a_flat_recent_stretch_are_not_called_improving():
    h = strength_headline(_strength("improved", "unclear"))
    assert h["title"] == "Stronger than at the start, no clear gain lately" and h["state"] == "neutral"
    assert h["detail"] == ("+3.6% a month at equal reps since Apr 2, 0% over the last 5 months."
                           " Up: chest and back. No clear change: arms and shoulders.")


def test_strength_headline_follows_the_recent_rate():
    assert strength_headline(_strength("improved", "improved"))["state"] == "improved"
    assert strength_headline(_strength("improved", "worsened"))["state"] == "worsened"
    assert strength_headline(_strength("unclear", "unclear"))["title"] == "No clear change in strength"


def test_no_strength_headline_without_enough_data():
    assert strength_headline(_strength("insufficient", "insufficient")) is None


def _metrics(walk="improved", rhr="unclear", hrr="insufficient"):
    return [{"key": "walk_hr", "verdict": walk, "year": {"now": 98.5, "last_year": 104.0}},
            {"key": "rhr", "verdict": rhr}, {"key": "hrr", "verdict": hrr}]


def test_cardio_counts_only_markers_that_have_a_reading():
    h = cardio_headline(_metrics(), {"vo2": {"verdict": "within_range"}})
    assert h["title"] == "1 of 3 fitness markers clearly better" and h["state"] == "improved"
    assert h["detail"] == ("Walking HR 98.5 vs 104 bpm a year ago, better; resting HR no clear change; "
                           "VO₂ within its past range. Too little recent data: HR recovery.")


def test_cardio_state_reflects_a_worse_marker():
    assert cardio_headline(_metrics(walk="worsened"), {"vo2": {"verdict": "unclear"}})["state"] == "worsened"
    assert cardio_headline(_metrics(rhr="worsened"), {"vo2": {"verdict": "unclear"}})["state"] == "neutral"


def test_no_cardio_headline_without_any_reading():
    m = _metrics(walk="insufficient", rhr="insufficient")
    assert cardio_headline(m, {"vo2": {"verdict": "insufficient"}}) is None
