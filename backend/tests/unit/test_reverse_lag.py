"""Reverse-lag placebo (correlations.REVERSE_BOOT_REPS, 2026-10-27).

Synthetic series only: an acute X(D) → Y(D+1) effect must pass, a shared slow
confounder with no causal link must fail, and the vectorised bootstrap pieces must
agree with their plain definitions.
"""
from datetime import date, timedelta

import numpy as np
from scipy.stats import spearmanr

from app.services.correlations import (
    REVERSE_BLOCK_DAYS, _block_counts, _build_pairs, _forward_reverse_interval,
    _reverse_lag, _weighted_rank_r,
)
from scipy.stats import rankdata

N = 365
START = date(2025, 1, 1)


def _series(x, y, drop=()):
    return {START + timedelta(days=i): {"x": float(x[i]), "y": float(y[i])}
            for i in range(len(x)) if i not in drop}


def _interval(series, lag=1):
    first, last = min(series), max(series)
    cal = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    xs, ys = _build_pairs(series, "x", "y", lag)
    sign = 1.0 if spearmanr(xs, ys)[0] > 0 else -1.0
    return _forward_reverse_interval(series, cal, _block_counts(len(cal)), "x", "y", lag, sign)


def test_reverse_lag_mapping():
    assert _reverse_lag(0) == -1
    assert _reverse_lag(1) == -1
    assert _reverse_lag(2) == -2


def test_block_counts_resample_whole_calendar_reproducibly():
    c = _block_counts(100, reps=50)
    assert c.shape == (50, 100)
    assert np.all(c.sum(axis=1) == 100)
    assert np.array_equal(c, _block_counts(100, reps=50))
    # Blocks, not single days: some day appears with its successor far more often than
    # independent draws would give.
    assert REVERSE_BLOCK_DAYS > 1


def test_unit_weights_reproduce_spearman():
    rng = np.random.default_rng(1)
    x, y = rng.normal(size=80), rng.normal(size=80)
    y += 0.5 * x
    r = _weighted_rank_r(np.ones((1, 80)), rankdata(x), rankdata(y))[0]
    assert abs(r - spearmanr(x, y)[0]) < 1e-9


def test_acute_effect_passes_even_with_autocorrelated_input():
    rng = np.random.default_rng(7)
    x = np.zeros(N)
    for t in range(1, N):
        x[t] = 0.4 * x[t - 1] + rng.normal()
    y = rng.normal(size=N)
    y[1:] += 0.6 * x[:-1]
    lo, hi = _interval(_series(x, y, drop=set(range(0, N, 11))))
    assert lo > 0, (lo, hi)


def test_shared_slow_confounder_fails():
    rng = np.random.default_rng(11)
    c = np.cumsum(rng.normal(size=N)) * 0.3          # slow drift, no causal path X → Y
    x = c + rng.normal(size=N)
    y = c + rng.normal(size=N)
    lo, hi = _interval(_series(x, y))
    assert lo <= 0 <= hi, (lo, hi)


def test_too_few_aligned_days_returns_none():
    s = _series(np.arange(4.0), np.arange(4.0))
    assert _interval(s) is None


def test_reverse_check_label():
    from app.services.correlations import reverse_check_label as lab
    base = {"metric_a": "alcohol_sdu", "spearman_r": 0.28}
    assert lab({**base, "forward_exceeds_reverse": None}) is None
    assert lab({**base, "forward_exceeds_reverse": True, "reverse_spearman_r": 0.05}) == "passes"
    # CONFOUNDED needs the reverse ρ itself to be real (CI excludes 0), same sign, ≥ half.
    assert lab({**base, "forward_exceeds_reverse": False, "reverse_spearman_r": 0.29,
                "reverse_ci_low": 0.09, "reverse_ci_high": 0.47}) == "confounded"
    assert lab({**base, "forward_exceeds_reverse": False, "reverse_spearman_r": 0.18,
                "reverse_ci_low": -0.03, "reverse_ci_high": 0.38}) == "inconclusive"
    assert lab({"metric_a": "sugar_g", "spearman_r": -0.26, "forward_exceeds_reverse": False,
                "reverse_spearman_r": -0.14, "reverse_ci_low": -0.33, "reverse_ci_high": 0.06}) == "inconclusive"
    assert lab({**base, "forward_exceeds_reverse": False, "reverse_spearman_r": 0.01}) == "inconclusive"
    # Opposite-sign reverse is never "similar".
    assert lab({**base, "forward_exceeds_reverse": False, "reverse_spearman_r": -0.2}) == "inconclusive"
    assert lab({"metric_a": "zone2_pct_7d", "spearman_r": 0.28, "forward_exceeds_reverse": False,
                "reverse_spearman_r": 0.28}) == "not_testable"


def test_confounded_rows_are_never_directives():
    from app.services.correlations import build_actionable_insights
    def row(a, b, r, check):
        return {"metric_a": a, "metric_b": b, "lag_days": 1, "spearman_r": r, "n": 150,
                "significant": True, "suggestive": False, "reverse_check": check}
    out = build_actionable_insights([
        row("wfh_day", "subj_energy", 0.28, "confounded"),
        row("alcohol_sdu", "rhr_z_score", 0.41, "passes"),
    ], min_abs_r=0.2, min_n=30, max_per_section=None)
    assert [i["input_key"] for i in out["confounded"]] == ["wfh_day"]
    assert out["confounded"][0]["sentence"] == "WFH day → higher Energy (next day)"
    lever_keys = {i["input_key"] for k in ("do_more", "do_less", "do_more_suggestive",
                                           "do_less_suggestive") for i in out[k]}
    assert lever_keys == {"alcohol_sdu"} and not out["trade_offs"]
    assert {(r["metric_a"], r["route"]) for r in out["routes"]} == {
        ("wfh_day", "confounded"), ("alcohol_sdu", "act")}


def test_redefinition_crossings_are_not_news():
    from datetime import date as d
    from app.services.correlations import is_redefinition_crossing as redef
    assert redef("rhr_z_score", d(2026, 10, 27)) and redef("rhr_z_score", d(2026, 10, 28))
    assert not redef("rhr_z_score", d(2026, 11, 1))          # a later crossing is real news
    assert not redef("rhr_z_score", d(2026, 10, 26))
    assert redef("sleep_hr_early_fixed", d(2026, 10, 27))
    assert not redef("subj_mood", d(2026, 10, 27))             # water → mood stays news
    assert not redef("rhr_z_score", None)
