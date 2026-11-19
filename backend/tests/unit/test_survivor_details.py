"""Survivor details (correlations.survivor_details, 2026-10-28)."""
from datetime import date, timedelta

import numpy as np

from app.services.correlations import survivor_details

START = date(2026, 1, 1)


def _series(xs, ys, a="alcohol_sdu", b="respiratory_rate"):
    return {START + timedelta(days=i): {a: float(x), b: float(y)} for i, (x, y) in enumerate(zip(xs, ys))}


def test_slope_in_natural_units_and_range():
    rng = np.random.default_rng(3)
    x = rng.uniform(0, 4, 200)
    y = 14 + 0.5 * x + rng.normal(0, 0.3, 200)
    d = survivor_details(_series(x, y), "alcohol_sdu", "respiratory_rate", 0)
    assert d["x_unit"] == "SDU" and d["y_unit"] == "brpm" and not d["binary"]
    assert d["slope_lo"] <= 0.5 <= d["slope_hi"] and abs(d["slope"] - 0.5) < 0.1
    assert 0 <= d["x_p05"] < d["x_p95"] <= 4
    assert not d["unstable"] and len(d["influential"]) == 5


def test_binary_input_is_a_difference():
    rng = np.random.default_rng(5)
    x = np.array([0, 1] * 60, dtype=float)
    y = 3 + 0.8 * x + rng.normal(0, 0.2, 120)
    d = survivor_details(_series(x, y, "wfh_day", "subj_energy"), "wfh_day", "subj_energy", 0)
    assert d["binary"] and d["n_yes"] == 60 and d["x_p05"] is None
    assert abs(d["slope"] - 0.8) < 0.15 and d["span_effect"] == d["slope"]


def test_one_day_driving_the_result_is_flagged():
    rng = np.random.default_rng(9)
    x = rng.normal(0, 1, 40)
    y = rng.normal(0, 1, 40)
    x[17], y[17] = 25.0, 25.0                    # a single extreme day
    x[23], y[23] = 24.0, 24.0                    # and a second one
    d = survivor_details(_series(x, y), "alcohol_sdu", "respiratory_rate", 0)
    top = {e["date"] for e in d["influential"][:2]}
    assert top == {(START + timedelta(days=17)).isoformat(), (START + timedelta(days=23)).isoformat()}


def test_lag_aligns_input_day_with_next_outcome_day():
    x = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    s = {START + timedelta(days=i): {"alcohol_sdu": float(v), "respiratory_rate": 0.0} for i, v in enumerate(x)}
    for i, v in enumerate(x[:-1]):
        s[START + timedelta(days=i + 1)]["respiratory_rate"] = float(2 * v)   # y(D+1) = 2·x(D)
    d = survivor_details(s, "alcohol_sdu", "respiratory_rate", 1)
    assert abs(d["slope"] - 2.0) < 1e-9 and d["n"] == 11
