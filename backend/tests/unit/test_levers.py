"""Ranked levers (services/levers.py, 2026-11-01): what gets in, what stays out, the order."""
from datetime import date, timedelta

from app.services.correlations import INPUTS
from app.services.levers import CIRCULAR, LEVERS, MAX_LEVERS, _clock, build_levers

TODAY = date(2026, 11, 1)


def detail(slope, span=1.0, unit="z", **kw):
    return {"slope": slope, "slope_lo": slope * 0.8, "slope_hi": slope * 1.2, "y_unit": unit,
            "span_effect_sd": span, "unstable": False, **kw}


def cached(a, b, lag=1, rho=0.4, d=None):
    return {"metric_a": a, "metric_b": b, "metric_b_label": b.upper(), "lag_days": lag,
            "spearman_r": rho, "n": 150, "survivor_details": d or detail(0.3)}


def row(a, b, lag=1, tier="confirmed"):
    return {"input_key": a, "outcome_key": b, "lag_days": lag, "tier": tier}


def run(less=(), more=(), rows=(), verdicts=None, series=None, alcohol=None):
    return build_levers({"do_less": list(less), "do_more": list(more)}, list(rows),
                        verdicts or {}, series or {}, alcohol or {}, TODAY)


def test_every_lever_is_a_real_input():
    assert set(LEVERS) <= set(INPUTS)
    assert MAX_LEVERS == 5


def test_effect_is_scaled_to_the_step_and_z_reads_as_sigma():
    rows = [cached("calories", "rhr_z_score", d=detail(0.0011))]
    e = run(less=[row("calories", "rhr_z_score")], rows=rows)["levers"][0]["effects"][0]
    assert e["effect"] == 0.55 and e["unit"] == "σ" and e["next_day"]


def test_weakening_finding_is_left_out_and_counted():
    k = ("alcohol_sdu", "subj_mood", 1)
    out = run(less=[row(*k)], rows=[cached(*k)], verdicts={k: "weakening"})
    assert out["levers"] == [] and out["left_out"] == {"weakening": 1}


def test_unstable_and_circular_findings_are_left_out():
    u = ("alcohol_sdu", "rhr_z_score", 1)
    c = next(iter(CIRCULAR)) + (0,)
    out = run(less=[row(*u)], more=[row(*c)],
              rows=[cached(*u, d=detail(0.3, unstable=True)), cached(*c)])
    assert out["levers"] == [] and out["left_out"] == {"unstable": 1, "circular": 1}


def test_a_surviving_input_that_is_not_a_lever_stays_out():
    k = ("screen_communication", "sleep_hr_early_fixed", 1)
    out = run(more=[row(*k)], rows=[cached(*k)])
    assert out["levers"] == [] and out["not_levers"] == 1


def test_a_lever_only_takes_rows_in_its_own_direction():
    k = ("sleep_total_h", "rhr_z_score", 1)
    assert run(less=[row(*k)], rows=[cached(*k)])["levers"] == []


def test_held_up_beats_a_bigger_effect_that_is_too_early():
    a, w = ("alcohol_sdu", "respiratory_rate", 1), ("water_oz", "subj_mood", 1)
    out = run(less=[row(*a)], more=[row(*w)],
              rows=[cached(*a, d=detail(0.2, 0.5)), cached(*w, d=detail(0.01, 2.0))],
              verdicts={a: "replicating"})
    assert [l["key"] for l in out["levers"]] == ["alcohol_sdu", "water_oz"]
    assert out["levers"][0]["held_up"] and not out["levers"][1]["held_up"]
    assert out["levers"][1]["effects"][0]["status"] == "too_early"


def test_exposure_needs_a_week_of_days_and_reports_how_many():
    k = ("calories", "rhr_z_score", 1)
    end = TODAY - timedelta(days=1)
    series = {end - timedelta(days=i): {"calories": 2500.0} for i in range(9)}
    lever = run(less=[row(*k)], rows=[cached(*k)], series=series)["levers"][0]
    assert (lever["now"], lever["now_days"]) == ("2,500 kcal/day", 9)
    assert lever["before"] is None


def test_alcohol_rate_counts_every_day_not_only_frame_days():
    # All of the drinking falls on the few days left in the frame, so the frame's own
    # mean more than doubles the weekly rate.
    k = ("alcohol_sdu", "rhr_z_score", 1)
    end = TODAY - timedelta(days=1)
    alcohol = {end - timedelta(days=i): 16.4 / 9 for i in range(9)}
    series = {end - timedelta(days=i): {"alcohol_sdu": 16.4 / 9} for i in range(9)}
    lever = run(less=[row(*k)], rows=[cached(*k)], series=series, alcohol=alcohol)["levers"][0]
    assert (lever["now"], lever["now_days"]) == ("4.1 SDU/wk", 28)
    assert lever["before"] == "0.0 SDU/wk"


def test_labels_are_shortened_for_the_card():
    k = ("calories", "rhr_z_score", 1)
    rows = [{**cached(*k), "metric_b_label": "RHR z-score"}]
    assert run(less=[row(*k)], rows=rows)["levers"][0]["effects"][0]["outcome"] == "RHR"


def test_bedtime_past_midnight_reads_as_a_clock():
    assert _clock(23.5) == "11:30 pm"
    assert _clock(24.25) == "12:15 am"
    assert _clock(25.5) == "1:30 am"
