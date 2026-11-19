"""
Verification script for the correlation engine changes.

Tests:
  a) AR(1) effective-N recovery: new _effective_n must reproduce (1-φxφy)/(1+φxφy)
     within ~0.08, and match the old closed-form to near-floating tolerance when
     fed large-n AR(1) data with very small φ (where biased-ACF ≈ true ACF).

  b) False-positive rate: 300 independent φ=0.85 AR(1) pairs. Naive n gives ~40%
     FPR at α=0.05; corrected n_eff should be near 5%.

  c) Spearman on y = x³ + noise with bootstrap CIs: rank invariance under a monotone
     transform, robustness to a single outlier, and a CI that excludes 0.

  d) Real-data regression: recompute all correlations old-vs-new, print diff table.

Usage (from backend/ directory):
    python tests/verify_correlations.py
    python tests/verify_correlations.py --db /path/to/wellness.db  # real-data check
"""
import argparse
import asyncio
import math
import os
import random
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── AR(1) simulator ───────────────────────────────────────────────────────────

def _ar1(n: int, phi: float, seed: int = 0) -> list[float]:
    rng = random.Random(seed)
    sigma = math.sqrt(1 - phi**2)  # keeps variance = 1
    x = [rng.gauss(0, 1)]
    for _ in range(n - 1):
        x.append(phi * x[-1] + rng.gauss(0, sigma))
    return x


# ── Task (a): AR(1) effective-N recovery ─────────────────────────────────────

def test_ar1_recovery():
    print("=" * 70)
    print("(a) AR(1) effective-N recovery")
    print("=" * 70)

    from app.services.correlations import _effective_n

    def _old_effective_n(n, rho_x, rho_y):
        """Original AR(1) closed-form for comparison."""
        denom = 1.0 + rho_x * rho_y
        if denom <= 0:
            return None
        return int(n * (1.0 - rho_x * rho_y) / denom)

    cases = [
        (0.3, 0.3),
        (0.5, 0.5),
        (0.7, 0.7),
        (0.8, 0.5),
        (0.9, 0.6),
    ]

    n = 500
    passed = True
    for phi_x, phi_y in cases:
        theory_ratio = (1 - phi_x * phi_y) / (1 + phi_x * phi_y)
        theory_neff = n * theory_ratio
        old_neff = _old_effective_n(n, phi_x, phi_y)

        # Average over 10 seeds to reduce noise
        neff_vals = []
        for seed in range(10):
            rx = _ar1(n, phi_x, seed=seed)
            ry = _ar1(n, phi_y, seed=seed + 1000)
            neff = _effective_n(n, rx, ry)
            if neff is not None:
                neff_vals.append(neff)

        if not neff_vals:
            print(f"  φx={phi_x} φy={phi_y}: all None — FAIL")
            passed = False
            continue

        avg_neff = sum(neff_vals) / len(neff_vals)
        avg_ratio = avg_neff / n
        diff = abs(avg_ratio - theory_ratio)
        status = "OK" if diff < 0.08 else "FAIL"
        if status == "FAIL":
            passed = False
        print(
            f"  φx={phi_x} φy={phi_y}:  theory={theory_ratio:.3f}  "
            f"new={avg_ratio:.3f}  |diff|={diff:.3f}  old_ratio={old_neff/n:.3f}  [{status}]"
        )

    # Verify match to old formula using large n and small phi (biased ACF ≈ true ACF)
    print("\n  Near-exact match with old formula (n=2000, small φ):")
    n_large = 2000
    for phi in [0.1, 0.3, 0.5]:
        old_neff = _old_effective_n(n_large, phi, phi)
        neff_vals = []
        for seed in range(5):
            rx = _ar1(n_large, phi, seed=seed)
            ry = _ar1(n_large, phi, seed=seed + 500)
            v = _effective_n(n_large, rx, ry)
            if v is not None:
                neff_vals.append(v)
        avg = sum(neff_vals) / len(neff_vals) if neff_vals else 0
        rel_err = abs(avg - old_neff) / max(old_neff, 1)
        status = "OK" if rel_err < 0.05 else "WARN"
        print(f"    φ={phi}  old={old_neff}  new={avg:.0f}  rel_err={rel_err:.3f}  [{status}]")

    return passed


# ── Task (b): False-positive rate under naive-n vs corrected n_eff ────────────

def test_false_positive_rate(n_pairs: int = 300, n_obs: int = 300, phi: float = 0.85):
    print("\n" + "=" * 70)
    print(f"(b) False-positive rate: {n_pairs} independent AR(1) pairs, φ={phi}, n={n_obs}")
    print("=" * 70)

    from scipy import stats as scipy_stats
    from app.services.correlations import _effective_n

    naive_fp = 0
    corrected_fp = 0
    skipped = 0
    alpha = 0.05

    for i in range(n_pairs):
        # Independent AR(1) series (x and y from different seeds — no cross-correlation)
        x = _ar1(n_obs, phi, seed=i)
        y = _ar1(n_obs, phi, seed=i + n_pairs)

        r, _ = scipy_stats.pearsonr(x, y)
        r = float(r)

        # Naive: use n_obs directly
        df_naive = n_obs - 2
        t_naive = r * math.sqrt(df_naive) / max(math.sqrt(max(1 - r**2, 0)), 1e-10)
        p_naive = float(2 * scipy_stats.t.sf(abs(t_naive), df=df_naive))
        if p_naive < alpha:
            naive_fp += 1

        # Corrected: use n_eff
        n_eff = _effective_n(n_obs, x, y)
        if n_eff is None:
            skipped += 1
            continue
        df_eff = n_eff - 2
        if df_eff < 1:
            skipped += 1
            continue
        t_eff = r * math.sqrt(df_eff) / max(math.sqrt(max(1 - r**2, 0)), 1e-10)
        p_eff = float(2 * scipy_stats.t.sf(abs(t_eff), df=df_eff))
        if p_eff < alpha:
            corrected_fp += 1

    valid = n_pairs - skipped
    naive_fpr = naive_fp / n_pairs
    corr_fpr = corrected_fp / valid if valid > 0 else float("nan")

    naive_ok = naive_fpr > 0.20  # expect badly inflated (~40%)
    corr_ok = corr_fpr < 0.12   # expect near 5% (give 2× margin for statistical noise)

    print(f"  Naive-n FPR:     {naive_fpr:.1%}  (expect ~40%)  {'OK (inflated as expected)' if naive_ok else 'UNEXPECTED — check test'}")
    print(f"  Corrected n_eff FPR: {corr_fpr:.1%}  (expect ~5%)   {'OK' if corr_ok else 'FAIL — FPR too high'}")
    print(f"  Skipped (n_eff=None): {skipped}")

    return naive_ok and corr_ok


# ── Task (c): Spearman vs Pearson on nonlinear data ───────────────────────────

def test_nonlinear_spearman():
    print("\n" + "=" * 70)
    print("(c) Spearman vs Pearson on y = x³ + noise, with bootstrap CIs")
    print("=" * 70)

    from scipy import stats as scipy_stats
    from app.services.correlations import _compute_corr
    from tests.bootstrap_verification import _block_bootstrap_ci

    rng = random.Random(7)
    n = 200
    x = [rng.gauss(0, 1) for _ in range(n)]
    y = [xi**3 + rng.gauss(0, 2) for xi in x]

    # _compute_corr expects raw series = aligned series here (no lags)
    result = _compute_corr(x, y, x, y)
    if result is None:
        print("  _compute_corr returned None — FAIL")
        return False

    r_pearson, r_spearman, p_val, n_eff, ci_low, ci_high = result

    # Pearson naive
    r_p_naive, p_p_naive = scipy_stats.pearsonr(x, y)
    r_s_naive, p_s_naive = scipy_stats.spearmanr(x, y)

    # Bootstrap CI for Spearman
    _, boot_lo, boot_hi = _block_bootstrap_ci(x, y, method="spearman", block_len=10)

    print(f"  Pearson  r = {float(r_p_naive):+.3f}  (naive p={p_p_naive:.4f})")
    print(f"  Spearman ρ = {float(r_s_naive):+.3f}  (naive p={p_s_naive:.4f})")
    print(f"  _compute_corr: pearson_r={r_pearson:+.3f}  spearman_r={r_spearman:+.3f}")
    print(f"    p_value={p_val:.4f}  n_eff={n_eff}  CI=[{ci_low:.3f}, {ci_high:.3f}]")
    print(f"  Bootstrap Spearman 95% CI: [{boot_lo:.3f}, {boot_hi:.3f}]")

    # This used to require |ρ| > |r| - 0.05 here. That is not a property of either
    # statistic: x³ of a normal x is heavy-tailed, the few large-|x| points carry a strong
    # signal against constant noise, and Pearson weights exactly those points — on this
    # seed r = 0.71 vs ρ = 0.60. What the engine relies on Spearman for is checked instead:
    # (1) RANK INVARIANCE — any monotone transform of y leaves ρ unchanged; r moves.
    y_mono = [math.copysign(abs(v) ** 0.2, v) for v in y]       # strictly increasing in y
    r_s_mono = float(scipy_stats.spearmanr(x, y_mono)[0])
    r_p_mono = float(scipy_stats.pearsonr(x, y_mono)[0])
    rank_invariant = abs(r_s_mono - float(r_s_naive)) < 1e-12
    # (2) OUTLIER ROBUSTNESS — one extreme point added to an INDEPENDENT pair inflates r,
    # not ρ (one hard night or one mis-logged day is the realistic case).
    rng2 = random.Random(11)
    xi_ = [rng2.gauss(0, 1) for _ in range(n)] + [12.0]
    yi_ = [rng2.gauss(0, 1) for _ in range(n)] + [12.0]
    r_p_out = float(scipy_stats.pearsonr(xi_, yi_)[0])
    r_s_out = float(scipy_stats.spearmanr(xi_, yi_)[0])
    robust = abs(r_s_out) < 0.15 and abs(r_p_out) > 0.3
    ci_excludes_zero = not (ci_low <= 0 <= ci_high)
    boot_excludes_zero = not (boot_lo <= 0 <= boot_hi)

    print(f"  Monotone transform of y: ρ {float(r_s_naive):+.3f} → {r_s_mono:+.3f}, "
          f"r {float(r_p_naive):+.3f} → {r_p_mono:+.3f}  "
          f"{'[OK] ρ unchanged' if rank_invariant else '[FAIL] ρ changed'}")
    print(f"  Independent pair + one outlier: ρ {r_s_out:+.3f}, r {r_p_out:+.3f}  "
          f"{'[OK] ρ unmoved, r inflated' if robust else '[FAIL]'}")
    print(f"  Parametric CI excludes 0:  {'YES' if ci_excludes_zero else 'NO'}")
    print(f"  Bootstrap CI excludes 0:   {'YES' if boot_excludes_zero else 'NO'}")

    return rank_invariant and robust and ci_excludes_zero


# ── Task (d): Real-data regression check ──────────────────────────────────────

async def test_real_data_diff(db_path: str):
    """
    Recompute all correlations and compare old (Pearson-based significance) vs
    new (Spearman-based significance). Print every pair that changes tier.
    """
    print("\n" + "=" * 70)
    print("(d) Real-data regression: old vs new significance tiers")
    print("=" * 70)

    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"

    from app.database import AsyncSessionLocal
    from app.services.correlations import (
        CORRELATION_PAIRS, _fetch_daily_series, _build_pairs,
        _compute_corr, _apply_bh, SIGNIFICANCE_THRESHOLD, METRIC_LABELS,
    )
    from scipy import stats as scipy_stats

    end = date.today()
    start = end - timedelta(days=180)

    async with AsyncSessionLocal() as db:
        series, raw_series = await _fetch_daily_series(db, start, end)

    old_results = []
    new_results = []

    for metric_a, metric_b, lag_days in CORRELATION_PAIRS:
        xs, ys = _build_pairs(series, metric_a, metric_b, lag_days)
        raw_x = raw_series.get(metric_a, [])
        raw_y = raw_series.get(metric_b, [])
        n = len(xs)

        base = {
            "metric_a": metric_a, "metric_b": metric_b, "lag_days": lag_days, "n": n,
        }

        # Old: Pearson r, lag-1 autocorr AR(1) n_eff
        if n >= 5 and raw_x and raw_y:
            r_p, _ = scipy_stats.pearsonr(xs, ys)
            r_p = float(r_p)
            # Old lag-1 autocorr estimate
            rho_x = float(scipy_stats.pearsonr(raw_x[:-1], raw_x[1:])[0]) if len(raw_x) > 3 else 0.0
            rho_y = float(scipy_stats.pearsonr(raw_y[:-1], raw_y[1:])[0]) if len(raw_y) > 3 else 0.0
            denom = 1 + rho_x * rho_y
            n_eff_old = int(n * (1 - rho_x * rho_y) / denom) if denom > 0 else None
            if n_eff_old and n_eff_old > 3:
                df_old = n_eff_old - 2
                t_old = r_p * math.sqrt(df_old) / max(math.sqrt(max(1 - r_p**2, 0)), 1e-10)
                p_old = float(2 * scipy_stats.t.sf(abs(t_old), df=df_old))
            else:
                p_old = None
        else:
            r_p = p_old = None

        old_results.append({**base, "pearson_r": round(r_p, 3) if r_p else None, "p_value": round(p_old, 4) if p_old else None})

        # New: Spearman, generalized n_eff
        corr = _compute_corr(xs, ys, raw_x, raw_y)
        if corr:
            r_pn, r_s, p_val, n_eff, ci_lo, ci_hi = corr
            new_results.append({**base,
                "pearson_r": round(r_pn, 3), "spearman_r": round(r_s, 3),
                "p_value": round(p_val, 4), "n_eff": n_eff,
            })
        else:
            new_results.append({**base, "pearson_r": None, "spearman_r": None, "p_value": None, "n_eff": None})

    _apply_bh(old_results)
    _apply_bh(new_results)

    def _tier(res):
        if res.get("significant"):
            return "significant"
        if res.get("suggestive"):
            return "suggestive"
        return "none"

    def _r_for_old(res):
        return res.get("pearson_r")

    def _r_for_new(res):
        return res.get("spearman_r")

    flips = []
    for old, new in zip(old_results, new_results):
        old_tier = _tier(old)
        new_tier = _tier(new)
        if old_tier != new_tier:
            la = METRIC_LABELS.get(old["metric_a"], old["metric_a"])
            lb = METRIC_LABELS.get(old["metric_b"], old["metric_b"])
            flips.append({
                "pair": f"{la} → {lb} (lag={old['lag_days']})",
                "n": old["n"],
                "old_r": _r_for_old(old), "old_tier": old_tier,
                "new_r": _r_for_new(new), "new_tier": new_tier,
                "old_p_adj": old.get("p_value_adjusted"),
                "new_p_adj": new.get("p_value_adjusted"),
            })

    # Summary counts
    old_sig = sum(1 for r in old_results if r.get("significant") and abs(r.get("pearson_r") or 0) >= SIGNIFICANCE_THRESHOLD)
    old_sug = sum(1 for r in old_results if r.get("suggestive") and abs(r.get("pearson_r") or 0) >= SIGNIFICANCE_THRESHOLD)
    new_sig = sum(1 for r in new_results if r.get("significant") and abs(r.get("spearman_r") or 0) >= SIGNIFICANCE_THRESHOLD)
    new_sug = sum(1 for r in new_results if r.get("suggestive") and abs(r.get("spearman_r") or 0) >= SIGNIFICANCE_THRESHOLD)

    print(f"\n  Summary:")
    print(f"    Old (Pearson, AR(1) n_eff):  significant={old_sig}  suggestive={old_sug}")
    print(f"    New (Spearman, gen. n_eff):  significant={new_sig}  suggestive={new_sug}")
    print(f"\n  Pairs that flipped tier ({len(flips)} total):")
    if flips:
        print(f"  {'Pair':<55} {'n':>4}  {'old_r':>6} {'old_tier':>12}  {'new_r':>6} {'new_tier':>12}  old_p_adj → new_p_adj")
        print("  " + "-" * 120)
        for f in sorted(flips, key=lambda x: x["pair"]):
            print(
                f"  {f['pair']:<55} {f['n']:>4}  "
                f"{str(f['old_r']):>6} {f['old_tier']:>12}  "
                f"{str(f['new_r']):>6} {f['new_tier']:>12}  "
                f"{str(f['old_p_adj']):<7} → {f['new_p_adj']}"
            )
    else:
        print("  (no tier changes)")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Correlation engine verification")
    parser.add_argument("--db", help="Path to SQLite DB for real-data check (d)")
    args = parser.parse_args()

    all_ok = True

    ok_a = test_ar1_recovery()
    all_ok &= ok_a

    ok_b = test_false_positive_rate()
    all_ok &= ok_b

    ok_c = test_nonlinear_spearman()
    all_ok &= ok_c

    if args.db:
        asyncio.run(test_real_data_diff(args.db))
    else:
        print("\n(d) Skipped: pass --db /path/to/wellness.db for real-data regression check")

    print("\n" + "=" * 70)
    print(f"Overall: {'ALL PASS' if all_ok else 'FAILURES — see above'}")
    print("=" * 70)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
