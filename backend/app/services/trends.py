"""
Temporal trend detection — "is a metric drifting up or down over time?"

This is a metric-vs-*time* analysis, deliberately separate from the metric-vs-metric
correlation engine. Method:
  - Theil–Sen slope (robust, median of pairwise slopes) → a per-week rate of change.
  - Mann–Kendall monotonic-trend test with the Hamed–Rao (1998) autocorrelation
    correction, because daily physiological series are heavily autocorrelated and an
    uncorrected MK p-value badly overstates confidence (same trap the correlation
    engine solved with Pyper–Peterman effective N).
  - Benjamini–Hochberg FDR across the metric panel, so testing ~12 series doesn't
    manufacture false "trends."

Runs on RAW values (weight, HRV ms, VO₂, …) — NOT z-scores, which are detrended by
construction (relative to a rolling baseline) and would hide a real multi-week drift.
Cheap enough (O(n²) MK, n ≤ ~365) to compute on demand; no cache.
"""
import logging
from datetime import date, datetime, timedelta

import numpy as np
from scipy.stats import norm, rankdata, theilslopes
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DailyMetric, HealthMetric, SleepSession
from app.timeutils import local_date_sql, local_day_bounds_utc, local_today

logger = logging.getLogger(__name__)

# key: (label, unit, higher_is_better, source, agg)
#   source: "dm:<col>" daily_metrics column | "hm:<type>" health_metrics | "sleep_h"
#   higher_is_better: True / False / None (neutral — direction reported, not judged)
_METRICS = [
    ("hrv",              "HRV",                 " ms",   True,  "dm:hrv_value",      None),
    ("rhr",              "Resting HR",          " bpm",  False, "dm:rhr_value",      None),
    ("walking_hr",       "Walking HR",          " bpm",  False, "hm:walking_heart_rate_avg", "avg"),
    ("readiness",        "Recovery score",      "",      True,  "dm:readiness_score",None),
    ("weight",           "Weight",              " lb",   None,  "hm:weight",         "avg"),
    ("body_fat",         "Body fat",            "%",     False, "hm:body_fat_percentage", "avg"),
    ("lean",             "Lean mass",           " lb",   True,  "hm:lean_body_mass", "avg"),
    ("vo2_max",          "VO₂ max",             "",      True,  "hm:vo2_max",        "avg"),
    ("resp_rate",        "Respiratory rate",    " brpm", False, "hm:respiratory_rate", "avg"),
    ("blood_oxygen",     "Blood oxygen",        "%",     True,  "hm:blood_oxygen",   "avg"),
    ("breathing",        "Breathing disturb.",  "",      False, "hm:breathing_disturbances", "avg"),
    ("sleep_hours",      "Sleep duration",      " h",    True,  "sleep_h",           None),
    ("exercise_minutes", "Exercise minutes",    " min",  True,  "hm:exercise_minutes", "sum"),
    ("steps",            "Steps",               "",      True,  "hm:steps",          "sum"),
    ("time_in_daylight", "Daylight",            " min",  True,  "hm:time_in_daylight", "sum"),
    ("bedroom_temp",     "Bedroom temp (night)","°",     None,  "hm:bedroom_temp",     "avg"),
    ("bedroom_humidity", "Bedroom humidity",    "%",     None,  "hm:bedroom_humidity", "avg"),
    # Key kept (Recovery reads trend.sleep_hr_elev); source switched 2026-10-28 to the
    # fixed-window metric. Experiment #3 reads sleep_hr_elevation directly, unaffected.
    ("sleep_hr_elev",    "Early-night HR",      " bpm",  False, "hm:sleep_hr_early_fixed","avg"),
]

MIN_POINTS = 12  # need enough observations for a meaningful trend test


async def _travel_days(db, start, end) -> set:
    """Travel + post-trip recovery days in [start, end]. Dropped from every trend and
    ITS series (2026-10-19): a fortnight abroad is a block of shifted steps/sleep/HRV
    that Theil–Sen and the changepoint search would otherwise read as a real change.
    Sick days are not dropped here (unchanged)."""
    from app.models import DayFlag
    from app.services.day_flags import AWAY_REASON, RECOVERY_REASON
    rows = (await db.execute(
        select(DayFlag.date).where(DayFlag.date >= start).where(DayFlag.date <= end)
        .where(DayFlag.reason.in_((AWAY_REASON, RECOVERY_REASON)))
    )).all()
    return {r[0] for r in rows}


async def _dm_series(db, start, end, col):
    rows = (await db.execute(
        select(DailyMetric.date, getattr(DailyMetric, col))
        .where(DailyMetric.date >= start).where(DailyMetric.date <= end)
        .order_by(DailyMetric.date)
    )).all()
    return [(r[0], float(r[1])) for r in rows if r[1] is not None]


async def _sleep_series(db, start, end):
    rows = (await db.execute(
        select(SleepSession.date, SleepSession.total_min)
        .where(SleepSession.date >= start).where(SleepSession.date <= end)
        .where(SleepSession.total_min.isnot(None))
        .order_by(SleepSession.date)
    )).all()
    return [(r[0], r[1] / 60.0) for r in rows if r[1]]


async def _hm_series(db, start, end, metric_type, agg):
    fn = func.sum if agg == "sum" else func.avg
    day_col = local_date_sql(HealthMetric.timestamp)
    rows = (await db.execute(
        select(day_col.label("d"), fn(HealthMetric.value).label("v"))
        .where(HealthMetric.metric_type == metric_type)
        .where(HealthMetric.timestamp >= local_day_bounds_utc(start)[0])
        .where(HealthMetric.timestamp < local_day_bounds_utc(end)[1])
        .group_by(day_col)
        .order_by(day_col)
    )).all()
    out = []
    for r in rows:
        if r.v is None:
            continue
        d = r.d if isinstance(r.d, date) else date.fromisoformat(str(r.d))
        out.append((d, float(r.v)))
    return out


def _mann_kendall_hr(t: np.ndarray, x: np.ndarray, slope: float) -> tuple[float, float]:
    """
    Mann–Kendall S/z with Hamed–Rao autocorrelation correction.
    Detrends by the Theil–Sen slope, ranks the residuals, inflates Var(S) by the
    autocorrelation of those ranks (significant lags only). Returns (z, two-sided p).
    """
    n = len(x)
    s = 0.0
    for k in range(n - 1):
        s += np.sum(np.sign(x[k + 1:] - x[k]))

    # tie-corrected variance under independence
    _, counts = np.unique(x, return_counts=True)
    tie = np.sum(counts * (counts - 1) * (2 * counts + 5))
    var_s = (n * (n - 1) * (2 * n + 5) - tie) / 18.0

    # Hamed–Rao correction from the ACF of the detrended ranks
    resid = x - slope * (t - t[0])
    ranks = rankdata(resid)
    rk = ranks - ranks.mean()
    denom = float(np.sum(rk ** 2))
    if denom > 0 and n > 3:
        bound = 1.96 / np.sqrt(n)
        contrib = 0.0
        for k in range(1, n - 2):
            r = float(np.sum(rk[:n - k] * rk[k:]) / denom)
            if abs(r) > bound:
                contrib += (n - k) * (n - k - 1) * (n - k - 2) * r
        n_factor = 1.0 + (2.0 / (n * (n - 1) * (n - 2))) * contrib
        var_s *= max(n_factor, 1e-6)

    if var_s <= 0:
        return 0.0, 1.0
    if s > 0:
        z = (s - 1) / np.sqrt(var_s)
    elif s < 0:
        z = (s + 1) / np.sqrt(var_s)
    else:
        z = 0.0
    p = 2.0 * (1.0 - norm.cdf(abs(z)))
    return float(z), float(p)


def _bh_adjust(pvals: list[float]) -> list[float]:
    """Benjamini–Hochberg FDR adjustment."""
    p = np.asarray(pvals, dtype=float)
    n = len(p)
    if n == 0:
        return []
    order = np.argsort(p)
    ranked = p[order] * n / np.arange(1, n + 1)
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    ranked = np.clip(ranked, 0, 1)
    out = np.empty(n)
    out[order] = ranked
    return out.tolist()


def _best_changepoint(y: np.ndarray, lo: int, hi: int) -> tuple[int | None, float]:
    """
    Single mean-shift changepoint by max SSE reduction (two segment means vs one).
    O(n) via prefix sums. Scans split index τ in [lo, hi] (segment2 starts at τ).
    """
    n = len(y)
    c1 = np.concatenate([[0.0], np.cumsum(y)])
    c2 = np.concatenate([[0.0], np.cumsum(y ** 2)])
    full = c2[n] - c1[n] * c1[n] / n if n > 0 else 0.0
    if hi < lo:
        return None, 0.0
    # Vectorised over every split τ (2026-10-21): same arithmetic in the same order as the
    # old per-τ Python loop, which ran ~1M times per Corroboration request (1.2 s of 1.6 s).
    # lo ≥ 5 and hi ≤ n-5 at every call site, so both segment lengths are > 0.
    tau = np.arange(lo, hi + 1)
    s_l, s_r = c1[tau], c1[n] - c1[tau]
    sse_l = c2[tau] - s_l * s_l / tau
    sse_r = (c2[n] - c2[tau]) - s_r * s_r / (n - tau)
    red = full - (sse_l + sse_r)
    i = int(np.argmax(red))                 # first maximum, as the loop's strict ">" kept
    if not red[i] > 0.0:
        return None, 0.0
    return int(tau[i]), float(red[i])


def _best_reductions(Y: np.ndarray, lo: int, hi: int) -> np.ndarray:
    """Row-wise `_best_changepoint(...)[1]` for a 2-D batch of series (bootstrap)."""
    n = Y.shape[1]
    z = np.zeros((Y.shape[0], 1))
    c1 = np.concatenate([z, np.cumsum(Y, axis=1)], axis=1)
    c2 = np.concatenate([z, np.cumsum(Y ** 2, axis=1)], axis=1)
    full = c2[:, n] - c1[:, n] * c1[:, n] / n
    tau = np.arange(lo, hi + 1)
    s_l, s_r = c1[:, tau], c1[:, [n]] - c1[:, tau]
    sse_l = c2[:, tau] - s_l * s_l / tau
    sse_r = (c2[:, [n]] - c2[:, tau]) - s_r * s_r / (n - tau)
    red = full[:, None] - (sse_l + sse_r)
    best = red.max(axis=1)
    return np.where(best > 0.0, best, 0.0)


def _detect_changepoint(dates, t, x, slope, intercept, scale, higher_better,
                        min_seg_days: int = 14, n_boot: int = 400):
    """
    Detect a step-change in the DETRENDED residuals (so a smooth trend alone does not
    register). Significance via moving-block bootstrap under H0 = no changepoint (which
    preserves autocorrelation — a plain permutation would manufacture false steps).
    Returns {date, p, shift, sentiment, delta} or None.
    """
    n = len(x)
    resid = x - (slope * (t - t[0]) + intercept)

    # valid split range: ≥5 points AND ≥min_seg_days of span on each side
    lo = 5
    while lo < n and (t[lo - 1] - t[0]) < min_seg_days:
        lo += 1
    hi = n - 5
    while hi > 0 and (t[n - 1] - t[hi]) < min_seg_days:
        hi -= 1
    if lo > hi:
        return None

    tau, red = _best_changepoint(resid, lo, hi)
    if tau is None:
        return None

    pre, post = float(resid[:tau].mean()), float(resid[tau:].mean())
    if abs(post - pre) < 0.25 * scale:   # magnitude gate
        return None

    rc = resid - resid.mean()
    block = max(3, int(round(n ** (1 / 3))))
    rng = np.random.default_rng(0)       # fixed seed → stable p across requests
    # All n_boot resamples at once (2026-10-21). One (n_boot, nb) draw consumes the
    # generator in the same order as n_boot draws of nb, so every resample — and the
    # p-value — is identical to the per-iteration loop it replaces (verified).
    nb = int(np.ceil(n / block))
    starts = rng.integers(0, n - block + 1, size=(n_boot, nb))
    boot = rc[(starts[:, :, None] + np.arange(block)).reshape(n_boot, nb * block)[:, :n]]
    count = int(np.count_nonzero(_best_reductions(boot, lo, hi) >= red))
    pval = (count + 1) / (n_boot + 1)
    if pval >= 0.05:
        return None

    up = post > pre
    sentiment = "neutral" if higher_better is None else ("good" if up == higher_better else "bad")
    return {
        "date": dates[tau].isoformat(),
        "p": round(pval, 4),
        "shift": "up" if up else "down",
        "sentiment": sentiment,
        "delta": round(post - pre, 3),
    }


def _weekly_series(start_date, t, x, slope, intercept, lo_s, hi_s) -> list[dict]:
    """
    Weekly means + the Theil–Sen fit + a slope-CI fan for the drill-down plot.
    The CI band pivots on the series midpoint (narrow in the middle, wider at the
    ends), which is the honest visual for uncertainty in a *slope*.
    """
    bins: dict[int, list] = {}
    for ti, xi in zip(t, x):
        bins.setdefault(int(ti // 7), []).append((ti, xi))
    t_mid = float(np.median(t))
    y_pivot = slope * t_mid + intercept
    out = []
    for w in sorted(bins):
        pts = bins[w]
        tc = float(np.mean([p[0] for p in pts]))
        d = start_date + timedelta(days=w * 7)
        a = y_pivot + lo_s * (tc - t_mid)
        b = y_pivot + hi_s * (tc - t_mid)
        out.append({
            "label": f"{d.strftime('%b')} {d.day}",
            "value": round(float(np.mean([p[1] for p in pts])), 2),
            "fit": round(slope * tc + intercept, 2),
            "lo": round(min(a, b), 2),
            "hi": round(max(a, b), 2),
        })
    return out


def _analyze_intervention_metric(t, x, T_day, washout, higher_better, scale, n_boot=400):
    """
    Segmented (interrupted time-series) regression at split T_day:
        y = β0 + β1·t + β2·D + β3·(t−T)·D        (D = 1 after the intervention)
      β2 = immediate level change, β3 = slope change (per day). β1 absorbs any pre-existing
      trend, so we don't credit the intervention for a rise already underway. The washout
      window [T, T+washout) is dropped from the fit (adaptations take time). Significance of
      β2/β3 comes from a moving-block bootstrap under H0 (reduced model = β0+β1·t), which
      keeps the autocorrelation an ordinary t-test would ignore.
    """
    mask = ~((t >= T_day) & (t < T_day + washout))
    tt, xx = t[mask], x[mask]
    if int(np.sum(tt < T_day)) < 8 or int(np.sum(tt >= T_day + washout)) < 8:
        return None
    D = (tt >= T_day).astype(float)
    X  = np.column_stack([np.ones_like(tt), tt, D, (tt - T_day) * D])
    Xr = np.column_stack([np.ones_like(tt), tt])
    beta, *_ = np.linalg.lstsq(X, xx, rcond=None)
    level, slope_ch = float(beta[2]), float(beta[3])

    br, *_ = np.linalg.lstsq(Xr, xx, rcond=None)
    fitted_r = Xr @ br
    rc = (xx - fitted_r) - (xx - fitted_r).mean()
    n = len(xx)
    block = max(3, int(round(n ** (1 / 3))))
    rng = np.random.default_rng(0)
    cl = cs = 0
    for _ in range(n_boot):
        nb = int(np.ceil(n / block))
        starts = rng.integers(0, n - block + 1, size=nb)
        y = fitted_r + np.concatenate([rc[s:s + block] for s in starts])[:n]
        b, *_ = np.linalg.lstsq(X, y, rcond=None)
        if abs(b[2]) >= abs(level):    cl += 1
        if abs(b[3]) >= abs(slope_ch): cs += 1
    p_level = (cl + 1) / (n_boot + 1)
    p_slope = (cs + 1) / (n_boot + 1)

    pre_mean  = float(np.mean(x[t < T_day]))
    post_mean = float(np.mean(x[t >= T_day + washout]))
    # A meaningful result: a significant level or slope change that's also materially large.
    level_ok = p_level < 0.05 and abs(level) >= 0.25 * scale
    slope_ok = p_slope < 0.05 and abs(slope_ch * 30) >= 0.25 * scale
    if not (level_ok or slope_ok):
        return {"significant": False, "level": round(level, 2), "p_level": round(p_level, 3)}
    up = (level if level_ok else slope_ch) > 0
    sentiment = "neutral" if higher_better is None else ("good" if up == higher_better else "bad")
    return {
        "significant": True,
        "level": round(level, 2), "p_level": round(p_level, 3),
        "slope_per_week": round(slope_ch * 7, 3), "p_slope": round(p_slope, 3),
        "pre_mean": round(pre_mean, 1), "post_mean": round(post_mean, 1),
        "sentiment": sentiment, "effect": "level" if level_ok else "slope",
    }


async def analyze_intervention(db: AsyncSession, T_date: date, washout: int = 7, pre_days: int = 90) -> list[dict]:
    """Interrupted-time-series report for every metric around a flagged intervention date."""
    start = T_date - timedelta(days=pre_days)
    end = local_today()
    skip = await _travel_days(db, start, end)
    out = []
    for key, label, unit, higher_better, source, agg in _METRICS:
        try:
            if source.startswith("dm:"):
                series = await _dm_series(db, start, end, source[3:])
            elif source.startswith("hm:"):
                series = await _hm_series(db, start, end, source[3:], agg)
            elif source == "sleep_h":
                series = await _sleep_series(db, start, end)
            else:
                continue
        except Exception as exc:
            logger.error("ITS fetch failed for %s: %s", key, exc)
            continue
        series = [(d, v) for d, v in series if d not in skip]
        if len(series) < 20:
            continue
        t = np.array([(d - series[0][0]).days for d, _ in series], dtype=float)
        x = np.array([v for _, v in series], dtype=float)
        if np.ptp(x) == 0:
            continue
        T_day = float((T_date - series[0][0]).days)
        scale = float(np.subtract(*np.percentile(x, [75, 25]))) or float(np.std(x)) or 1.0
        r = _analyze_intervention_metric(t, x, T_day, washout, higher_better, scale)
        if r is None:
            continue
        out.append({"key": key, "label": label, "unit": unit, **r})
    # significant movers first, by strongest (smallest) p
    out.sort(key=lambda r: (not r["significant"], r.get("p_level", 1)))
    return out


async def compute_trends(db: AsyncSession, days: int = 90) -> list[dict]:
    """Per-metric temporal trend over the last `days`. Returns a ranked list."""
    end = local_today()
    start = end - timedelta(days=days)

    skip = await _travel_days(db, start, end)
    raw: list[dict] = []
    for key, label, unit, higher_better, source, agg in _METRICS:
        try:
            if source.startswith("dm:"):
                series = await _dm_series(db, start, end, source[3:])
            elif source.startswith("hm:"):
                series = await _hm_series(db, start, end, source[3:], agg)
            elif source == "sleep_h":
                series = await _sleep_series(db, start, end)
            else:
                continue
        except Exception as exc:
            logger.warning("trend fetch failed for %s: %s", key, exc)
            continue
        series = [(d, v) for d, v in series if d not in skip]

        if len(series) < MIN_POINTS:
            continue

        t = np.array([(d - series[0][0]).days for d, _ in series], dtype=float)
        x = np.array([v for _, v in series], dtype=float)
        if np.ptp(x) == 0:  # flat / no variance
            continue

        ts = theilslopes(x, t)
        slope, intercept = float(ts[0]), float(ts[1])   # per day
        lo_s, hi_s = float(ts[2]), float(ts[3])          # slope CI
        z, p = _mann_kendall_hr(t, x, slope)

        # Magnitude gate: the modeled change across the whole window must be a real
        # fraction of the series' spread (IQR), else it's significant-but-trivial
        # (e.g. MK flags a monotonic tendency in noisy exercise minutes while the
        # Theil–Sen slope is ~0). Unit-free, mirrors the |r|≥0.3 rule for correlations.
        scale = float(np.subtract(*np.percentile(x, [75, 25]))) or float(np.std(x)) or 1.0
        change_total = slope * (t[-1] - t[0])

        # Changepoint: a *step* in the detrended residuals (separate from a smooth trend).
        cp = _detect_changepoint([d for d, _ in series], t, x, slope, intercept, scale, higher_better)

        raw.append({
            "key": key, "label": label, "unit": unit,
            "higher_better": higher_better,
            "per_week": round(slope * 7, 3),
            "change_total": round(change_total, 2),
            "n": len(series),
            "latest": round(x[-1], 1),
            "p_value": round(p, 4),
            "_p": p,
            "_meaningful": abs(change_total) >= 0.25 * scale,
            "changepoint": cp,
            "series": _weekly_series(series[0][0], t, x, slope, intercept, lo_s, hi_s),
        })

    p_adj = _bh_adjust([r["_p"] for r in raw])
    results = []
    for r, pa in zip(raw, p_adj):
        # A trend must clear BOTH the FDR threshold AND the magnitude gate.
        if pa < 0.20 and r["_meaningful"]:
            tier = "confirmed" if pa < 0.05 else "suggestive"
            rising = r["per_week"] > 0
            direction = "rising" if rising else "falling"
            hb = r["higher_better"]
            sentiment = "neutral" if hb is None else ("good" if rising == hb else "bad")
        else:
            tier, direction, sentiment = "flat", "steady", "neutral"
        results.append({
            "key": r["key"], "label": r["label"], "unit": r["unit"],
            "direction": direction, "sentiment": sentiment,
            "per_week": r["per_week"], "change_total": r["change_total"], "latest": r["latest"],
            "n": r["n"], "p_value": r["p_value"], "p_adj": round(pa, 4),
            "tier": tier, "days": days, "changepoint": r["changepoint"],
            "series": r["series"],
        })

    # Movers first (confirmed, then suggestive), each by smallest p_adj; steady last.
    tier_rank = {"confirmed": 0, "suggestive": 1, "flat": 2}
    results.sort(key=lambda r: (tier_rank[r["tier"]], r["p_adj"]))
    return results
