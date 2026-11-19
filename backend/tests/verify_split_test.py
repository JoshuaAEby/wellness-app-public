"""
Verify the corroboration split test (services/corroboration._split_test).

WHY THIS EXISTS AS A SYNTHETIC TEST: the check cannot be exercised on real data
today. It only runs when an input and an outcome both have a detected
changepoint within CP_MAX_GAP_DAYS, and as of 2026-08-25 the five detected
changepoints (hrv 2025-10-29, rhr 2025-11-14, blood_oxygen 2025-11-03,
time_in_daylight 2025-09-23, exercise_minutes 2026-03-25) are all further apart
than that — the closest pair, hrv/rhr, misses by 2 days. So live output shows
17 pairs, all "correlation-only", and proves nothing about this code path.

The three cases below are the ones the logic has to get right:

  A. CONFOUNDED — input and outcome step together, but are independent inside
     each era. This is the case the whole change exists for: the old code
     scored it "corroborated", i.e. it UPGRADED a confounded pair.
  B. GENUINE   — they step together AND covary within each era. Must survive;
     the fix must not throw away real findings to catch case A.
  C. AMBIGUOUS — too few days either side to judge. Must abstain, NOT report a
     collapse: a short segment has a small rho by construction, and calling
     that "confounded" would be the same error in the opposite direction.

Run:  python backend/tests/verify_split_test.py
"""
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.corroboration import (  # noqa: E402
    SEGMENT_MIN_N, SEGMENT_RETAIN_FRAC, _split_test,
)

A, B = "steps", "hrv_z_score"
START = date(2026, 1, 1)


def _series(xs, ys):
    return {START + timedelta(days=i): {A: float(x), B: float(y)}
            for i, (x, y) in enumerate(zip(xs, ys))}


def _pair(series):
    """The pooled pair dict build_corroboration would hand to _split_test."""
    days = sorted(series)
    xs = [series[d][A] for d in days]
    ys = [series[d][B] for d in days]
    rho, _ = stats.spearmanr(xs, ys)
    return {"metric_a": A, "metric_b": B, "lag_days": 0, "spearman_r": float(rho)}


def case_confounded(rng, n=60):
    """Independent within each era; both jump at the cut. Classic common cause."""
    xs = np.concatenate([rng.normal(0, 1, n), rng.normal(4, 1, n)])
    ys = np.concatenate([rng.normal(0, 1, n), rng.normal(4, 1, n)])
    return _series(xs, ys), START + timedelta(days=n)


def case_genuine(rng, n=60):
    """Real within-era covariation, plus a step in both."""
    x_pre, x_post = rng.normal(0, 1, n), rng.normal(4, 1, n)
    xs = np.concatenate([x_pre, x_post])
    ys = np.concatenate([x_pre * 0.9 + rng.normal(0, 0.4, n),
                         x_post * 0.9 + rng.normal(0, 0.4, n)])
    return _series(xs, ys), START + timedelta(days=n)


def case_ambiguous(rng, n=8):
    """Same shape as the confounded case but too short to judge either side."""
    xs = np.concatenate([rng.normal(0, 1, n), rng.normal(4, 1, n)])
    ys = np.concatenate([rng.normal(0, 1, n), rng.normal(4, 1, n)])
    return _series(xs, ys), START + timedelta(days=n)


def main() -> int:
    rng = np.random.default_rng(20260825)
    print(f"SEGMENT_MIN_N={SEGMENT_MIN_N}  SEGMENT_RETAIN_FRAC={SEGMENT_RETAIN_FRAC}\n")

    cases = [
        ("A independent within era, both step", case_confounded, "step_driven"),
        ("B genuine covariation + step",        case_genuine,    "survives"),
        ("C segments too short",                case_ambiguous,  "ambiguous"),
    ]

    failures = 0
    for label, build, expected in cases:
        series, cut = build(rng)
        c = _pair(series)
        got = _split_test(series, c, cut)
        ok = got["verdict"] == expected
        failures += not ok
        print(f"  {label}")
        print(f"    pooled rho = {c['spearman_r']:+.3f}   "
              f"pre = {got['rho_pre']} (n={got['n_pre']})   "
              f"post = {got['rho_post']} (n={got['n_post']})")
        print(f"    verdict {got['verdict']!r}, expected {expected!r}   "
              f"[{'OK' if ok else 'FAIL'}]\n")

    # The regression this whole change is about: case A must NOT be treated as
    # supporting evidence. Under the old code its concordance counted as
    # agreement and the pair sorted to the top as "corroborated".
    series, cut = case_confounded(rng)
    verdict = _split_test(series, _pair(series), cut)["verdict"]
    counts_as_support = verdict == "survives"
    print(f"  REGRESSION: confounded pair counted as support? "
          f"{counts_as_support}  [{'FAIL' if counts_as_support else 'OK'}]")
    failures += counts_as_support

    print("\n" + ("FAILURES" if failures else "All split-test cases pass"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
