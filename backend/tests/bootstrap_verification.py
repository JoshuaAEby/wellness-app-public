"""
Bootstrap verification gate for the correlation engine.

NOT part of the production path. Run against a copy of your database to flag
cases where the parametric Spearman test and the bootstrap disagree.

Usage:
    DATABASE_URL=sqlite+aiosqlite:////path/to/wellness.db python bootstrap_verification.py

Only runs on pairs where BOTH sides are highly persistent EWMA/autocorrelated
series: atl, ctl, acwr, daily_load, hrv_z_score, rhr_z_score.
These are the series most likely to have inflated n_eff even after Pyper-Peterman
correction (CTL φ≈0.976 saturates the truncated ACF sum at L=n//5).
"""
import asyncio
import math
import os
import random
import sys
from datetime import date, timedelta
from typing import Optional

# Add parent to path when run as a script from the tests/ directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Block bootstrap and permutation utilities ─────────────────────────────────

def _block_bootstrap_ci(
    xs: list[float],
    ys: list[float],
    method: str = "spearman",
    n_bootstrap: int = 1000,
    block_len: Optional[int] = None,
    seed: int = 42,
) -> tuple[float, float, float]:
    """
    Moving-block bootstrap 95% CI for the correlation between xs and ys.
    Returns (observed_r, ci_low, ci_high).
    Block length: max(30, n//12) unless overridden.
    """
    from scipy.stats import spearmanr, pearsonr, rankdata

    n = len(xs)
    if n < 10:
        raise ValueError(f"Need at least 10 observations, got {n}")
    L = block_len if block_len is not None else max(30, n // 12)

    if method == "spearman":
        obs_r = float(spearmanr(xs, ys).statistic)
        def _corr(a, b):
            return float(spearmanr(a, b).statistic)
    else:
        obs_r = float(pearsonr(xs, ys).statistic)
        def _corr(a, b):
            return float(pearsonr(a, b).statistic)

    rng = random.Random(seed)
    pairs = list(zip(xs, ys))
    n_blocks = math.ceil(n / L)
    starts = list(range(n - L + 1))  # valid block start positions

    boot_rs = []
    for _ in range(n_bootstrap):
        chosen = [rng.choice(starts) for _ in range(n_blocks)]
        sample = []
        for s in chosen:
            sample.extend(pairs[s : s + L])
        sample = sample[:n]
        bx = [p[0] for p in sample]
        by = [p[1] for p in sample]
        try:
            boot_rs.append(_corr(bx, by))
        except Exception:
            pass

    boot_rs.sort()
    lo = boot_rs[int(0.025 * len(boot_rs))]
    hi = boot_rs[int(0.975 * len(boot_rs))]
    return obs_r, lo, hi


def _circular_block_perm_pvalue(
    xs: list[float],
    ys: list[float],
    method: str = "spearman",
    n_perm: int = 1000,
    block_len: Optional[int] = None,
    seed: int = 42,
) -> tuple[float, float]:
    """
    Circular-block permutation p-value: permute y with circular block shifts,
    preserving y's autocorrelation structure while breaking cross-correlation.
    Returns (observed_r, p_value).
    Block length: max(30, n//12) unless overridden.
    """
    from scipy.stats import spearmanr, pearsonr

    n = len(xs)
    L = block_len if block_len is not None else max(30, n // 12)

    if method == "spearman":
        obs_r = float(spearmanr(xs, ys).statistic)
        def _corr(a, b):
            return float(spearmanr(a, b).statistic)
    else:
        obs_r = float(pearsonr(xs, ys).statistic)
        def _corr(a, b):
            return float(pearsonr(a, b).statistic)

    rng = random.Random(seed)
    abs_obs = abs(obs_r)
    count_extreme = 0

    for _ in range(n_perm):
        # Random circular shift by a multiple of block_len to preserve block structure
        shift = rng.randint(0, (n // L) - 1) * L if n >= 2 * L else rng.randint(1, n - 1)
        y_shifted = ys[shift:] + ys[:shift]
        try:
            perm_r = _corr(xs, y_shifted)
            if abs(perm_r) >= abs_obs:
                count_extreme += 1
        except Exception:
            pass

    return obs_r, (count_extreme + 1) / (n_perm + 1)  # +1 includes observed value


# ── Persistent-series keys (EWMA or derived from EWMA) ───────────────────────

PERSISTENT_KEYS = frozenset({
    "daily_load",    # TRIMP (input to ATL/CTL EWMA)
    "acwr",          # ATL/CTL ratio
    "hrv_z_score",   # derived from 30d EWMA baseline
    "rhr_z_score",   # derived from 30d EWMA baseline
})

# Also include ATL/CTL/baselines if they appear in the series dict
# (they're not direct correlation keys currently, but check anyway)
_EXTRA_PERSISTENT = frozenset({"atl", "ctl", "hrv_baseline", "rhr_baseline"})


def _is_persistent(key: str) -> bool:
    return key in PERSISTENT_KEYS or key in _EXTRA_PERSISTENT


# ── Main verification runner ──────────────────────────────────────────────────

async def run_verification(db_url: Optional[str] = None) -> None:
    """
    Load production correlation data, run bootstrap on persistent pairs, and
    print a table of any disagreements between parametric Spearman and bootstrap.
    """
    if db_url:
        os.environ.setdefault("DATABASE_URL", db_url)

    try:
        from app.database import AsyncSessionLocal
        from app.services.correlations import (
            CORRELATION_PAIRS, _fetch_daily_series, _build_pairs,
            _compute_corr, _apply_bh, METRIC_LABELS,
        )
    except ImportError as e:
        print(f"[bootstrap_verification] Import error: {e}")
        print("Run from the backend/ directory: python tests/bootstrap_verification.py")
        return

    end = date.today()
    start = end - timedelta(days=180)

    print(f"Loading daily series from {start} to {end}...")
    async with AsyncSessionLocal() as db:
        series, raw_series = await _fetch_daily_series(db, start, end)

    persistent_pairs = [
        (a, b, lag) for a, b, lag in CORRELATION_PAIRS
        if _is_persistent(a) or _is_persistent(b)
    ]
    print(f"Evaluating {len(persistent_pairs)} persistent-input pairs with bootstrap...\n")

    disagreements = []
    results_for_bh = []

    for metric_a, metric_b, lag_days in persistent_pairs:
        xs, ys = _build_pairs(series, metric_a, metric_b, lag_days)
        raw_x = raw_series.get(metric_a, [])
        raw_y = raw_series.get(metric_b, [])

        if len(xs) < 20:
            continue

        corr = _compute_corr(xs, ys, raw_x, raw_y)
        if corr is None:
            continue
        _, r_spearman, p_val, n_eff, ci_low, ci_high = corr
        result = {
            "metric_a": metric_a, "metric_b": metric_b, "lag_days": lag_days,
            "spearman_r": round(r_spearman, 3), "p_value": round(p_val, 4),
            "n_eff": n_eff, "ci_low": round(ci_low, 3), "ci_high": round(ci_high, 3),
            "n": len(xs),
        }
        results_for_bh.append(result)

    _apply_bh(results_for_bh)

    for result in results_for_bh:
        metric_a = result["metric_a"]
        metric_b = result["metric_b"]
        lag_days = result["lag_days"]
        xs, ys = _build_pairs(series, metric_a, metric_b, lag_days)

        _, boot_lo, boot_hi = _block_bootstrap_ci(xs, ys, method="spearman")
        _, perm_p = _circular_block_perm_pvalue(xs, ys, method="spearman")

        boot_significant = not (boot_lo <= 0 <= boot_hi)  # CI excludes 0
        param_significant = result.get("significant", False)

        la = METRIC_LABELS.get(metric_a, metric_a)
        lb = METRIC_LABELS.get(metric_b, metric_b)
        pair_str = f"{la} → {lb} (lag={lag_days})"

        if boot_significant != param_significant:
            direction = "PARAM_SIG/BOOT_NOT" if param_significant else "BOOT_SIG/PARAM_NOT"
            disagreements.append({
                "pair":      pair_str,
                "direction": direction,
                "rho":       result["spearman_r"],
                "p_param":   result["p_value"],
                "p_adj":     result.get("p_value_adjusted"),
                "p_perm":    round(perm_p, 4),
                "boot_ci":   f"[{boot_lo:.3f}, {boot_hi:.3f}]",
                "n":         result["n"],
                "n_eff":     result["n_eff"],
            })
            flag = f"⚠  {direction}"
        else:
            flag = "ok"

        print(
            f"{flag:30s}  ρ={result['spearman_r']:+.3f}  "
            f"p_adj={result.get('p_value_adjusted', '?'):<6}  "
            f"boot_CI={boot_lo:+.3f}..{boot_hi:+.3f}  "
            f"p_perm={perm_p:.3f}  n={result['n']}/n_eff={result['n_eff']}  "
            f"{pair_str}"
        )

    print(f"\n{'='*80}")
    if not disagreements:
        print("No disagreements between parametric Spearman and bootstrap.")
    else:
        print(f"{len(disagreements)} disagreement(s) — trust the bootstrap for these pairs:\n")
        for d in disagreements:
            print(f"  [{d['direction']}] {d['pair']}")
            print(f"    ρ={d['rho']:+.3f}  p_param={d['p_param']}  p_adj={d['p_adj']}"
                  f"  p_perm={d['perm_p']}  boot_CI={d['boot_ci']}"
                  f"  n={d['n']}/n_eff={d['n_eff']}\n")


if __name__ == "__main__":
    db_url = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(run_verification(db_url))
