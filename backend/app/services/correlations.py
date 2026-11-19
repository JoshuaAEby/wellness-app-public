"""
Cross-metric correlation engine.

Computes Pearson r between pairs of wellness metrics over a rolling window.
Results are cached in the insight_cache table and refreshed weekly.
An on-demand path re-queries live data for arbitrary windows.
"""
import logging
import math
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
from scipy import stats
from scipy.stats import rankdata
from statsmodels.stats.multitest import multipletests
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings as app_settings
from app.models import (
    BodyWeightLog, DailyMetric, FindingLedger, FoodLog, HabitLog, HealthMetric,
    HevyWorkout, InsightCache, ScreentimeEvent, SleepSession, SubjectiveLog,
    UserSettings, WeatherDaily, Workout,
)
from app.workout_categories import categorize_activity
from app.timeutils import local_date_sql, local_day_bounds_utc, to_local_date, local_today, to_local, local_food_date_sql
from app.foodday import food_day_of_utc

logger = logging.getLogger(__name__)

# Truncate the Pyper-Peterman ACF sum at L = n // ACF_MAX_LAG_FRAC.
# Under-captures the tail for highly persistent EWMA inputs (CTL φ≈0.976);
# that case is covered by the bootstrap verification gate (tests/bootstrap_verification.py).
ACF_MAX_LAG_FRAC = 5

# ── Metric definitions ────────────────────────────────────────────────────────
#
# Each entry: (key, human_label, source, extractor)
# source: "dm" = daily_metrics column, "sleep" = sleep_sessions column,
#         "hm:<type>" = health_metrics aggregated by day (sum)
#
# We define the pairs to correlate separately below.

METRIC_LABELS = {
    # ── Outcomes (measured / independent of the inputs) ───────────────────────
    "hrv_z_score":          "HRV z-score",
    "rhr_z_score":          "RHR z-score",
    "wrist_temp_deviation": "Wrist temp deviation",
    "respiratory_rate":     "Respiratory rate",
    "walking_hr":           "Walking HR",
    "breathing_disturbances": "Breathing disturbances",
    "subj_energy":          "Energy (subjective)",
    "subj_mood":            "Mood (subjective)",
    "subj_sleep":           "Sleep quality (subjective)",
    "subj_stress":          "Stress (subjective)",
    "subj_day":             "Day quality (subjective)",
    "sleep_hr_elevation":   "Early-night HR elevation",
    "sleep_hr_early_fixed": "Early-night HR (first 2 h)",
    # ── Night-of inputs (the sleep that ended this morning) ───────────────────
    "sleep_total_h":        "Sleep duration (h)",
    "sleep_quality_pct":    "Deep+REM %",
    # NOT "Bedtime (later → higher)": rendered as "<label> → <outcome>" in the briefing
    # context, that read as "later bedtime → higher energy" — the OPPOSITE of the real
    # finding (a negative rho; note 55). Labels state units, never arrows.
    "bedtime_hour":         "Bedtime (clock hour)",
    "bedroom_temp":         "Bedroom temp (night)",
    "bedroom_humidity":     "Bedroom humidity (night)",
    "slept_away":           "Away night",
    "partner_present":      "Partner present (night)",
    "tv_prebed_min":        "TV before bed (min)",
    "wfh_day":              "WFH day",
    "weather_temp_mean":     "Outdoor temp (mean °F)",
    "weather_aqi_mean":      "Air quality (US AQI)",
    "weather_uv_max":        "UV index (max)",
    "weather_pressure_mean": "Barometric pressure (hPa)",
    "weather_pressure_delta": "Pressure swing (hPa)",
    "screen_total":         "Phone screen time (min)",
    "screen_social":        "Social-media screen (min)",
    "screen_communication": "Messaging screen (min)",
    "screen_entertainment": "Entertainment screen (min)",
    "screen_evening":       "Phone screen before bed (min)",
    # ── Activity / training inputs ────────────────────────────────────────────
    "steps":                "Steps",
    "active_calories":      "Active calories",
    "exercise_minutes":     "Exercise minutes",
    "stand_hours":          "Stand hours",
    "time_in_daylight":     "Daylight (min)",
    "walking_speed":        "Walking speed",
    "walk_distance":        "Walking distance",
    "first_light_hour":     "First daylight (clock h)",
    "morning_light_min":    "Morning daylight (min)",
    "light_centroid_hour":  "Daylight midpoint (clock h)",
    "daily_load":           "Training load (TRIMP)",
    "zone2_pct_7d":         "Zone 2 % (7-day)",
    "zone3_pct_7d":         "Zone 3 % (7-day)",
    "high_intensity_pct_7d": "High-intensity % (7-day)",
    "hard_minutes":         "Hard minutes (zones 4–5)",
    "acwr":                 "ACWR",
    # ── Strength inputs (Hevy set-level data) ─────────────────────────────────
    "tonnage":              "Strength tonnage (kg)",
    "hard_sets":            "Hard sets (strength)",
    # ── Nutrition inputs ──────────────────────────────────────────────────────
    "calories":             "Calorie intake",
    "protein_g":            "Protein (g)",
    "carbs_g":              "Carbs (g)",
    "fat_g":                "Fat (g)",
    "fiber_g":              "Fiber (g)",
    "sugar_g":              "Sugar (g)",
    "sodium_mg":            "Sodium (mg)",
    "restaurant_meals":     "Restaurant meals",
    "energy_balance":       "Energy balance (kcal)",
    # ── Workout-type inputs (binary day flags) ────────────────────────────────
    "workout_strength":     "Strength day",
    "workout_hiit":         "Gym class (any)",
    "workout_conditioning": "Conditioning class",
    "workout_lifting":      "Lifting class",
    "workout_gym_class":    "Gym class (any)",
    "workout_walk":         "Walk day",
    # ── Other behavioral inputs ───────────────────────────────────────────────
    "alcohol_sdu":          "Alcohol (SDU)",
    "nap_min":              "Nap (min)",
    "water_oz":             "Water (oz)",
    "mindful_min":          "Mindful minutes",
    "habit_caffeine":       "No late caffeine",
    "habit_light":          "Outdoor light AM",
    "habit_supplements":    "Supplements",
    "habit_creatine":       "Creatine",
    "habit_meditation":     "Meditation",
    "caffeine_last_hour":   "Last caffeine (hour)",
    "meditation_hour":      "Meditation time (hour)",
    "caffeine_total_mg":    "Caffeine dose (mg)",
    "caffeine_onboard_bed": "Caffeine at bedtime (mg)",
    "alcohol_onboard_bed":  "Alcohol at bedtime (SDU)",
    # ── Negative controls (known-null by construction — pipeline calibration) ──
    "ctrl_date_parity":     "Control: date parity",
    "ctrl_random_walk":     "Control: random walk",
    # ── Excluded as outcomes (circular with their own formulas) — labels only ──
    "readiness_score":      "Readiness",
    "sleep_debt_hours":     "Sleep debt (h)",
}

# ── Pair generation ───────────────────────────────────────────────────────────
# Outcomes are measured markers, never computed from the inputs. readiness_score and
# sleep_debt_hours are intentionally NOT outcomes — they are algebraic functions of the
# inputs (HRV/RHR/sleep/ACWR), so correlating against them is circular by construction.
# Early-night HR SWAPPED 2026-10-27 (#6): sleep_hr_elevation (first third − last third of
# the night) → sleep_hr_early_fixed (first 2 h after onset − night p10). The thirds scale
# with night length, so sleep duration → elevation (+0.27) was arithmetic: +0.07 under the
# fixed window, while bedtime / alcohol / calories / messaging held. Swapped, not added —
# at ρ 0.86 a second copy would widen the BH family by ~80 near-duplicate tests. The old
# SERIES is still built (row dict below) so the finding ledger keeps re-testing what it
# registered, and experiment #3's pre-registered outcome is untouched.
_OUTCOME_KEYS = [
    "hrv_z_score", "rhr_z_score", "wrist_temp_deviation", "respiratory_rate",
    "walking_hr", "breathing_disturbances", "sleep_hr_early_fixed",
    "subj_energy", "subj_mood", "subj_sleep", "subj_stress",
]

# Outcomes whose MEASUREMENT changed, and when. Every pair on such an outcome re-crosses the
# finding-ledger tiers at the first refresh after the change — a change of measure, not new
# evidence — and the briefing's "WHAT'S NEW" announced exactly those (2026-10-28: calories /
# alcohol → RHR after note 90's overnight RHR; messaging → early-night HR after the #6 swap,
# already confirmed under the old key). The ledger is never edited (pre-registration), so
# the briefing skips crossings dated within REDEFINITION_GRACE_DAYS of the change instead.
OUTCOME_REDEFINED: dict[str, tuple[date, str]] = {
    "rhr_z_score":          (date(2026, 10, 27), "RHR became overnight 10th-percentile HR (note 90)"),
    "sleep_hr_early_fixed": (date(2026, 10, 27), "early-night HR became the fixed 2 h window (#6)"),
    # Set to the DEPLOY date of the HRV change — update it if the deploy slips.
    "hrv_z_score":          (date(2026, 10, 28), "nightly HRV became the geometric mean of ≥3 readings"),
}
REDEFINITION_GRACE_DAYS = 1   # the change day's manual refreshes (local dates 10-27/10-28)


def is_redefinition_crossing(metric_b: str, crossed: Optional[date]) -> bool:
    """Did this ledger crossing happen only because the outcome was redefined?"""
    red = OUTCOME_REDEFINED.get(metric_b)
    return bool(red and crossed and red[0] <= crossed <= red[0] + timedelta(days=REDEFINITION_GRACE_DAYS))
# Night-of inputs describe the sleep that ended *this* morning, so they align with the
# same-morning measurement (lag 0); lag 1 captures carry-over to the next day.
# bedroom_temp/humidity arrive from the HA env-sensor bridge as one aggregate per
# night, timestamped at wake → they bucket to the wake date like sleep does.
_NIGHT_INPUTS = ["sleep_total_h", "sleep_quality_pct", "bedtime_hour",
                 "bedroom_temp", "bedroom_humidity", "slept_away", "partner_present",
                 "tv_prebed_min", "screen_evening", "caffeine_onboard_bed",
                 "alcohol_onboard_bed"]
# Daytime/behavioral inputs accrue across day D and affect the *next* morning's measured
# recovery and check-in → lag 1 (yesterday's behavior → today's state). This matches the
# date conventions: sleep_sessions.date / subjective_log.date are the wake/morning date.
_DAY_INPUTS = [
    "steps", "active_calories", "exercise_minutes", "stand_hours", "time_in_daylight", "daily_load", "acwr",
    "first_light_hour", "morning_light_min", "light_centroid_hour",
    "walking_speed", "walk_distance",
    "zone2_pct_7d", "zone3_pct_7d", "high_intensity_pct_7d", "hard_minutes",
    "calories", "protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g", "sodium_mg", "energy_balance",
    "restaurant_meals",
    "workout_strength", "workout_hiit", "workout_walk",
    # workout_conditioning / workout_lifting / workout_gym_class RETIRED FROM DISCOVERY
    # 2026-09-14 (note 31): over 365 days lifting found nothing, conditioning and any-class
    # only restated total load (next-day respiratory rate, rho ~0.2), and each extra input
    # widens the BH family for everything else. Replaced by the continuous `hard_minutes`
    # (with `tonnage` already here). Their SERIES are still built below so the finding
    # ledger keeps re-testing the entries they registered — removing the series would
    # leave those reading "accumulating" forever.
    "alcohol_sdu", "wfh_day", "nap_min", "water_oz", "mindful_min",
    "weather_temp_mean", "weather_aqi_mean", "weather_uv_max",
    "weather_pressure_mean", "weather_pressure_delta",
    "screen_total", "screen_social", "screen_communication", "screen_entertainment",
    "habit_caffeine", "habit_light", "habit_supplements", "habit_creatine", "habit_meditation",
    "caffeine_last_hour", "meditation_hour", "caffeine_total_mg",
]

# Metrics that participate in correlations but must NEVER reach an LLM prompt.
#
# The correlation engine is local arithmetic; the summaries ship context to the API.
# Those are different trust boundaries, so a metric listed here stays on this side of the
# second one. Enforced in summary_context, not the API path, so the findings stay visible
# in the app's own Insights view. Empty by default; it is the switch for any metric that
# should never leave the server.
PRIVATE_METRICS = frozenset()

# Strength inputs (Hevy) get lag 1 AND lag 2: DOMS and the inflammatory response peak
# 24–48 h post-session, so HRV/RHR effects may surface a day later than the standard
# next-morning window used for the other daytime inputs.
_STRENGTH_INPUTS = ["tonnage", "hard_sets"]
# Negative controls — known-null inputs that run through the FULL pipeline (same BH
# family, same n_eff machinery) as honest calibration. If either ever reaches
# suggestive/confirmed, the pipeline's false-positive control is leaking. Parity is
# an alternating deterministic null; the random walk is strongly autocorrelated on
# purpose — it stress-tests the Pyper-Peterman effective-N correction, which is
# exactly where spurious "significance" would sneak in. Deliberately absent from
# INPUTS so they can never surface as do-more/do-less advice.
_CONTROL_INPUTS = ["ctrl_date_parity", "ctrl_random_walk"]


def _generate_pairs() -> list[tuple[str, str, int]]:
    pairs: list[tuple[str, str, int]] = []
    for inp in _NIGHT_INPUTS:
        for out in _OUTCOME_KEYS:
            pairs.extend([(inp, out, 0), (inp, out, 1)])
    for inp in _DAY_INPUTS + _CONTROL_INPUTS:
        for out in _OUTCOME_KEYS:
            pairs.append((inp, out, 1))
    for inp in _STRENGTH_INPUTS:
        for out in _OUTCOME_KEYS:
            pairs.extend([(inp, out, 1), (inp, out, 2)])
    # subj_day is rated in the EVENING of day D, so — unlike the morning outcomes —
    # day inputs align same-day (lag 0: today's behavior → tonight's rating), as do
    # night inputs (the sleep that ended this morning shapes today). Strength gets
    # lag 0 and 1 (DOMS carry-over). It is deliberately NOT in _OUTCOME_KEYS, which
    # would generate wrong-lag pairs.
    for inp in _NIGHT_INPUTS + _DAY_INPUTS + _CONTROL_INPUTS:
        pairs.append((inp, "subj_day", 0))
    for inp in _STRENGTH_INPUTS:
        pairs.extend([(inp, "subj_day", 0), (inp, "subj_day", 1)])
    # Environment → OBJECTIVE sleep: bedroom conditions plausibly cause sleep
    # architecture, so sleep duration / deep+REM % act as outcomes for env inputs
    # only (they remain inputs everywhere else — do not generalize this).
    # Same-night alignment: both are properties of the night ending on wake day D.
    for inp in ("bedroom_temp", "bedroom_humidity"):
        for out in ("sleep_total_h", "sleep_quality_pct"):
            pairs.append((inp, out, 0))
    return pairs


# (metric_a_key, metric_b_key, lag_days) — generated grid (~189 pairs), filtered by
# significance/n downstream. lag>0: metric_a from day D, metric_b from day D+lag.
CORRELATION_PAIRS = _generate_pairs()


def _biased_acf(vals: list[float], max_lag: int) -> list[float]:
    """
    Biased sample ACF at lags 1..max_lag.
    Numerator: Σ_{t=0}^{n-k-1} (x_t - x̄)(x_{t+k} - x̄)   (n-k terms)
    Denominator: Σ_{t=0}^{n-1} (x_t - x̄)²                  (full-series variance)
    Dividing by n in both, the factors cancel — result equals cov_k / var_0.

    Each lag is one numpy dot product rather than a Python generator sum. This was
    the single hottest line in the whole engine (2.96 s of a 5.3 s pair loop, ~11 M
    generator calls); the arithmetic is unchanged.
    """
    n = len(vals)
    if n < 2 or max_lag < 1:
        return [0.0] * max_lag
    c = np.asarray(vals, dtype=float)
    c = c - c.mean()
    var0 = float(c @ c)
    if var0 <= 1e-10:
        return [0.0] * max_lag
    result = [float(c[:n - k] @ c[k:]) / var0
              for k in range(1, min(max_lag, n - 1) + 1)]
    while len(result) < max_lag:
        result.append(0.0)
    return result


def _acf_profile(vals: list[float]) -> Optional[dict]:
    """
    The per-metric quantities _effective_n needs, computed once.

    Everything _effective_n derives from a series — its length, its variance, and
    its ACF — depends on that ONE series, but it was being recomputed inside the
    per-pair loop: 930 pairs meant 1,860 ACF computations over only 78 distinct
    metrics, a 23.8x redundancy. So did the rankdata() call feeding it (2,828
    calls for the same 78 series).

    The ACF is computed out to `n // ACF_MAX_LAG_FRAC`, the longest lag ANY pair
    can ask this metric for: a pair's L is min(n_x, n_y) // ACF_MAX_LAG_FRAC,
    which can never exceed n_x // ACF_MAX_LAG_FRAC. Callers slice to their own L,
    and because ACF at lag k does not depend on max_lag, the slice is exact —
    identical values, not an approximation.

    Returns None for series _effective_n would reject anyway (too short, or
    zero-variance), so a None profile and a None n_eff mean the same thing.
    """
    n = len(vals)
    if n < 2:
        return None
    c = np.asarray(vals, dtype=float)
    c = c - c.mean()
    var = float(c @ c)
    if var <= 1e-6:
        # Zero-variance series — ACF undefined; _effective_n rejects these to
        # avoid anticonservative p-values.
        return None
    max_lag = n // ACF_MAX_LAG_FRAC
    acf = [float(c[:n - k] @ c[k:]) / var for k in range(1, max_lag + 1)]
    return {"n": n, "acf": acf}


def _effective_n(
    n_aligned: int,
    raw_x: list[float],
    raw_y: list[float],
    prof_x: Optional[dict] = None,
    prof_y: Optional[dict] = None,
) -> Optional[int]:
    """
    Pyper-Peterman (1998) generalized effective sample size:

        n_eff = n_aligned / (1 + 2 * Σ_{k=1..L} (1 - k/n_aligned) * ρx(k) * ρy(k))

    ρx(k), ρy(k) are the biased sample ACFs estimated on each FULL per-metric series
    separately (ACF on raw series, not aligned pairs — avoids contamination from missing
    days in the other metric). Dropping NaNs collapses calendar gaps; not corrected here.

    L = min(n_x, n_y) // ACF_MAX_LAG_FRAC; (1-k/n_aligned) is the Bartlett triangular taper.

    Returns None when n_eff ≤ 3 (inference would be meaningless).

    `prof_x` / `prof_y` are optional precomputed _acf_profile() results. Passing
    them skips the per-pair ACF; omitting them computes from raw_x/raw_y as before,
    which is the path the verification tests take.
    """
    if prof_x is None:
        prof_x = _acf_profile(raw_x)
    if prof_y is None:
        prof_y = _acf_profile(raw_y)
    if prof_x is None or prof_y is None:
        return None

    L = min(prof_x["n"], prof_y["n"]) // ACF_MAX_LAG_FRAC
    if L < 1:
        # Series too short to estimate any lag — fall back to n_aligned (conservative).
        n_eff = n_aligned
    else:
        acf_x, acf_y = prof_x["acf"], prof_y["acf"]
        vif_sum = 1.0
        for k in range(1, L + 1):
            taper = (1.0 - k / n_aligned) if k < n_aligned else 0.0
            vif_sum += 2.0 * taper * acf_x[k - 1] * acf_y[k - 1]
        if vif_sum <= 1e-6:
            return None
        n_eff = int(n_aligned / vif_sum)

    n_eff = min(n_eff, n_aligned)  # cannot exceed observed aligned pairs
    return n_eff if n_eff > 3 else None


def _compute_corr(
    xs: list[float],
    ys: list[float],
    raw_x: list[float],
    raw_y: list[float],
    prof_x: Optional[dict] = None,
    prof_y: Optional[dict] = None,
) -> Optional[tuple[float, float, float, int, float, float]]:
    """
    Returns (pearson_r, spearman_r, p_value, n_eff, ci_low, ci_high) or None.

    Headline coefficient is Spearman ρs; Pearson r is stored as reference only.
    p_value, CI, and n_eff are derived from the Spearman test.

    Effective N uses Pyper-Peterman on the rank-transformed full per-metric series.
    Autocorrelations are estimated from raw series before cross-metric alignment,
    so missing days in one metric cannot bias the ACF estimate for the other.

    `prof_x` / `prof_y` are optional _acf_profile() results for the RANKED raw
    series (see _rank_profiles). They carry no pair-specific information, so
    supplying them only avoids recomputation — the returned numbers are identical.
    """
    if len(xs) < 5:
        return None

    # SUPPORT GATE — reject near-constant series regardless of how large n is.
    #
    # Zero-inflated inputs otherwise get the worst of both worlds: n counts every day
    # (mindful_min sits at n=357 because Apple passively records mindfulness and most
    # days are a genuine zero) while the actual information lives in a handful of
    # nonzero days. Spearman on ~350 tied zeros plus 7 real values reports a p-value
    # computed from n=357, which badly overstates the evidence, and neither the n<20
    # display gate nor the Pyper-Peterman autocorrelation correction catches it —
    # they address different problems.
    #
    # Applied to BOTH series and to any distribution dominated by one value, not just
    # zeros, so it also covers a habit that is true on 353 of 357 days.
    for series in (xs, ys):
        modal = max(Counter(series).values()) if series else 0
        if len(series) - modal < MIN_SUPPORT_OBS:
            return None
    try:
        r_pearson, _ = stats.pearsonr(xs, ys)
        r_pearson = float(r_pearson)
        if math.isnan(r_pearson):
            return None

        r_spearman, _ = stats.spearmanr(xs, ys)
        r_spearman = float(r_spearman)
        if math.isnan(r_spearman):
            return None

        # Rank-transform full series for Pyper-Peterman ACF (captures rank
        # autocorrelation). Skipped entirely when the caller supplies profiles.
        if prof_x is None or prof_y is None:
            raw_x_ranked = rankdata(raw_x).tolist() if prof_x is None else []
            raw_y_ranked = rankdata(raw_y).tolist() if prof_y is None else []
        else:
            raw_x_ranked = raw_y_ranked = []
        n_eff = _effective_n(len(xs), raw_x_ranked, raw_y_ranked, prof_x, prof_y)
        if n_eff is None:
            return None

        df = n_eff - 2
        if df < 1:
            return None

        # Spearman t-statistic (exact under H0: ρs = 0)
        t_stat = r_spearman * math.sqrt(df) / max(math.sqrt(max(1.0 - r_spearman**2, 0.0)), 1e-10)
        p_val = float(2 * stats.t.sf(abs(t_stat), df=df))

        # Fisher z CI with Bonett-Wright (2000) SE for Spearman: SE = sqrt((1+ρs²/2)/(n_eff-3))
        z = 0.5 * math.log((1.0 + r_spearman) / max(1.0 - r_spearman, 1e-10))
        se = math.sqrt((1.0 + r_spearman**2 / 2.0) / max(n_eff - 3, 1))
        ci_low = float(math.tanh(z - 1.96 * se))
        ci_high = float(math.tanh(z + 1.96 * se))

        return (r_pearson, r_spearman, p_val, n_eff, ci_low, ci_high)
    except Exception:
        return None


# ── Why a pair could not be tested (2026-10-28, addendum item 12) ─────────────
# _compute_corr returns None for several reasons; such pairs carry no p-value and are NOT
# in the BH family (784 of 917 on 2026-10-28), but Evidence counted them as "tested". The
# reason is recorded so the view can list them honestly. Mirrors _compute_corr's order.
# slept_away is untestable BY DESIGN: an away night becomes a `travel` day flag, and
# flagged days are excluded from the frame, so inside it the input is always 0.
BY_DESIGN_UNTESTABLE = {"slept_away": "away nights are excluded from the analysis"}


def untestable_reason(metric_a: str, xs: list[float], ys: list[float]) -> str:
    if metric_a in BY_DESIGN_UNTESTABLE:
        return "by_design"
    if len(xs) < 5:
        return "few_pairs"
    for which, series in (("input", xs), ("outcome", ys)):
        modal = max(Counter(series).values()) if series else 0
        if len(series) - modal < MIN_SUPPORT_OBS:
            return f"no_variation_{which}"
    return "low_neff"


# ── Survivor details (2026-10-28, addendum items 14, 17, 18, 20) ──────────────
# For each significant/suggestive pair: an effect in NATURAL UNITS (Theil–Sen slope with its
# 95% CI; for a yes/no input that is the Hodges–Lehmann yes-minus-no difference), the
# outcome's own spread to judge it against, the input range the data actually covers
# (5th–95th pct — says nothing outside it), and how much the result rests on a few days:
# the 5 days whose removal moves ρ most, the drop-one-day ρ range, and a 95% band over
# random 10% drops. `unstable` = any of those changes the sign or crosses zero.
# Computed at refresh for survivors only (~0.15 s each); stored as JSON (0081).
DETAIL_DROP_REPS = 200
DETAIL_DROP_FRAC = 0.10
DETAIL_SEED = 20261028

# Units for the slope text; anything missing falls back to "unit".
METRIC_UNITS = {
    "alcohol_sdu": "SDU", "alcohol_onboard_bed": "SDU", "calories": "kcal", "energy_balance": "kcal",
    "carbs_g": "g", "protein_g": "g", "fat_g": "g", "fiber_g": "g", "sugar_g": "g", "sodium_mg": "mg",
    "restaurant_meals": "meals",
    "water_oz": "oz", "caffeine_total_mg": "mg", "caffeine_onboard_bed": "mg", "caffeine_last_hour": "h",
    "sleep_total_h": "h", "bedtime_hour": "h", "sleep_quality_pct": "%", "bedroom_temp": "°",
    "bedroom_humidity": "%", "steps": "steps", "active_calories": "kcal", "exercise_minutes": "min",
    "hard_minutes": "min", "daily_load": "TRIMP", "tonnage": "kg", "hard_sets": "sets",
    "walk_distance": "mi", "time_in_daylight": "min", "morning_light_min": "min",
    "first_light_hour": "h", "light_centroid_hour": "h", "screen_total": "min", "screen_social": "min",
    "screen_communication": "min", "screen_entertainment": "min", "screen_evening": "min",
    "tv_prebed_min": "min", "nap_min": "min", "mindful_min": "min", "stand_hours": "h",
    "zone2_pct_7d": "%", "zone3_pct_7d": "%", "high_intensity_pct_7d": "%",
    "weather_temp_mean": "°F", "weather_aqi_mean": "AQI", "weather_uv_max": "UV",
    "weather_pressure_mean": "hPa", "weather_pressure_delta": "hPa", "walking_speed": "mph",
    # outcomes
    "hrv_z_score": "z", "rhr_z_score": "z", "respiratory_rate": "brpm", "walking_hr": "bpm",
    "sleep_hr_early_fixed": "bpm", "sleep_hr_elevation": "bpm", "wrist_temp_deviation": "°C",
    "breathing_disturbances": "/h", "subj_energy": "pts", "subj_mood": "pts", "subj_sleep": "pts",
    "subj_stress": "pts", "subj_day": "pts",
}


def _aligned_with_dates(series: dict[date, dict], a: str, b: str, lag: int):
    dates, xs, ys = [], [], []
    for d in sorted(series):
        row_b = series.get(d + timedelta(days=lag))
        if row_b is None:
            continue
        x, y = series[d].get(a), row_b.get(b)
        if x is not None and y is not None:
            dates.append(d); xs.append(float(x)); ys.append(float(y))
    return dates, np.asarray(xs), np.asarray(ys)


def survivor_details(series: dict[date, dict], a: str, b: str, lag: int) -> Optional[dict]:
    dates, x, y = _aligned_with_dates(series, a, b, lag)
    n = len(x)
    if n < 10 or np.ptp(x) == 0 or np.ptp(y) == 0:
        return None
    binary = set(np.unique(x)) <= {0.0, 1.0}
    slope, _icpt, lo, hi = stats.theilslopes(y, x, alpha=0.95)
    sd_y = float(np.std(y, ddof=1))
    if binary:
        rng_lo = rng_hi = None
        span_effect = float(slope)                       # yes-day minus no-day
        n_yes = int((x == 1).sum())
    else:
        rng_lo, rng_hi = (float(v) for v in np.percentile(x, [5, 95]))
        span_effect = float(slope) * (rng_hi - rng_lo)
        n_yes = None
    rho = float(stats.spearmanr(x, y)[0])
    keep = np.ones(n, dtype=bool)
    drop1 = np.empty(n)
    for i in range(n):
        keep[i] = False
        drop1[i] = stats.spearmanr(x[keep], y[keep])[0]
        keep[i] = True
    order = np.argsort(-np.abs(drop1 - rho))[:5]
    rng = np.random.default_rng(DETAIL_SEED)
    k = max(1, int(round(DETAIL_DROP_FRAC * n)))
    drop10 = np.array([
        stats.spearmanr(x[m], y[m])[0]
        for m in (np.isin(np.arange(n), rng.choice(n, k, replace=False), invert=True)
                  for _ in range(DETAIL_DROP_REPS))])
    d10_lo, d10_hi = (float(v) for v in np.nanpercentile(drop10, [2.5, 97.5]))
    unstable = bool(np.sign(drop1.min()) != np.sign(drop1.max()) or d10_lo * d10_hi <= 0)
    # Significant figures, not decimals: a slope of 0.00099 z/kcal rounded to 3 decimals
    # became 0.001 with an interval of [0.001, 0.001].
    r3 = lambda v: None if v is None else float(f"{float(v):.4g}")
    return {
        "x_unit": METRIC_UNITS.get(a, "unit"), "y_unit": METRIC_UNITS.get(b, "unit"),
        "binary": binary, "slope": r3(slope), "slope_lo": r3(lo), "slope_hi": r3(hi),
        "y_sd": r3(sd_y), "x_p05": r3(rng_lo), "x_p95": r3(rng_hi), "n_yes": n_yes, "n": n,
        "span_effect": r3(span_effect), "span_effect_sd": r3(span_effect / sd_y if sd_y else None),
        "influential": [{"date": dates[i].isoformat(), "rho_without": r3(drop1[i])} for i in order],
        "drop1_lo": r3(drop1.min()), "drop1_hi": r3(drop1.max()),
        "drop10_lo": r3(d10_lo), "drop10_hi": r3(d10_hi), "unstable": unstable,
    }


# ── Reverse-lag placebo (added 2026-10-27) ────────────────────────────────────
# For a pair X(D) → Y(D+lag) the same machinery is also run on X(D) vs Y(D−r), an
# outcome measured BEFORE the input happened — X cannot have caused it. A slow or
# trait-level confounder (season, a training block, a life phase) or reverse causation
# (feeling bad → doing less) moves both directions alike; a short-acting effect of X
# shows up forward only. r = lag for lagged pairs and 1 for same-day pairs.
#
# The reverse coefficient is NOT zero under a real effect: if X is autocorrelated,
# X(D) tracks X(D−lag−r), which drove Y(D−r). So the test is not "reverse ≈ 0" but
# "forward clearly exceeds reverse": a circular moving-block bootstrap (blocks of a
# week, one shared set of calendar resamples so forward and reverse stay paired) of
# s·(ρ_fwd − ρ_rev), s = sign of the forward ρ. `forward_exceeds_reverse` is True when
# the 95% interval of that difference excludes 0. A reverse ρ of the OPPOSITE sign
# passes — it is evidence against a shared slow confounder, not for one.
#
# Inside each replicate the coefficient is Pearson on the ranks of the full aligned
# sample (count-weighted), i.e. "rank once" rather than re-ranking every resample —
# the standard approximation, and what lets 2,000 replicates run as two matrix products.
# Bootstrapped only for significant/suggestive pairs (a null pair has nothing to
# explain away); every pair still gets reverse_spearman_r and its CI.
REVERSE_BOOT_REPS = 2000
REVERSE_BLOCK_DAYS = 7
REVERSE_BOOT_SEED = 20261027
# Result keys the placebo adds; persisted 1:1 as insight_cache columns (0079).
REVERSE_FIELDS = ("reverse_lag_days", "reverse_spearman_r", "reverse_n", "reverse_ci_low",
                  "reverse_ci_high", "fwd_rev_diff", "fwd_rev_diff_low", "fwd_rev_diff_high",
                  "forward_exceeds_reverse")
# Every extra per-pair field persisted 1:1 in insight_cache (0079 reverse-lag, 0080 reason).
CACHE_EXTRA_FIELDS = REVERSE_FIELDS + ("untestable_reason", "survivor_details")

# ── Reverse-lag verdict for display (2026-10-27) ──────────────────────────────
# forward_exceeds_reverse is a yes/no on "is the forward ρ shown to beat the reverse",
# and a "no" means two different things that must not share a label:
#   confounded      — the reverse ρ is itself REAL (its 95% CI excludes 0), has the same
#                     sign and keeps at least REVERSE_SIMILAR_FRAC of the forward one. The
#                     input cannot have caused yesterday's outcome, so something else
#                     drives the link: a third factor, or the outcome driving the input
#                     (a reverse ρ as large as the forward one, its CI clear of 0). On
#                     the first run those reverse ρ were 3–10× what a real effect plus the
#                     input's own day-to-day repetition would produce. The user: plain
#                     "CONFOUNDED", no hedge — the CI requirement is what earns it
#                     (was `reverse_similar`, which also caught reverse ρ whose CI
#                     included 0; those are now `inconclusive`).
#   inconclusive    — failed, but the reverse is weaker or its interval spans 0: the test
#                     can't tell (a strong forward ρ, a near-zero reverse, small n).
#   not_testable    — the input is a 7-day rolling window (or ACWR, 7 vs 28 days): X(D)
#                     shares 6 of 7 days with X(D−1), so the reverse pair is nearly the
#                     forward pair and the test fails by construction.
#   passes          — forward_exceeds_reverse.
# None when the pair was not tested (not significant/suggestive). 0.5 is a round number,
# set after seeing the first run's fails; two sat near it (ratios of 0.52 and 0.54).
REVERSE_SIMILAR_FRAC = 0.5
ROLLING_INPUTS = frozenset({"zone2_pct_7d", "zone3_pct_7d", "high_intensity_pct_7d", "acwr"})


def reverse_check_label(r: dict) -> Optional[str]:
    if r.get("metric_a") in ROLLING_INPUTS and r.get("forward_exceeds_reverse") is not None:
        return "not_testable"
    fer = r.get("forward_exceeds_reverse")
    if fer is None:
        return None
    if fer:
        return "passes"
    fwd, rev = r.get("spearman_r"), r.get("reverse_spearman_r")
    lo, hi = r.get("reverse_ci_low"), r.get("reverse_ci_high")
    if (fwd and rev is not None and rev * fwd > 0
            and abs(rev) >= REVERSE_SIMILAR_FRAC * abs(fwd)
            and lo is not None and hi is not None and lo * fwd > 0 and hi * fwd > 0):
        return "confounded"
    return "inconclusive"


def descriptive_phrase(metric_a: str, metric_b: str, r: float, lag_days: int = 0) -> str:
    """The relationship described, never instructed — "Water → higher Energy (next day)".
    For rows that must not read as advice (confounded)."""
    a = INPUTS.get(metric_a, {}).get("label") or METRIC_LABELS.get(metric_a, metric_a)
    b = OUTCOMES.get(metric_b, {}).get("label") or METRIC_LABELS.get(metric_b, metric_b)
    lag = "" if not lag_days else " (next day)" if lag_days == 1 else f" ({lag_days} days later)"
    return f"{a} → {'higher' if r > 0 else 'lower'} {b}{lag}"


def _reverse_lag(lag_days: int) -> int:
    return -max(lag_days, 1)


def _block_counts(n_days: int, reps: int = REVERSE_BOOT_REPS,
                  block: int = REVERSE_BLOCK_DAYS, seed: int = REVERSE_BOOT_SEED) -> np.ndarray:
    """(reps × n_days) multiplicity of each calendar day in a circular moving-block
    resample of length n_days. Fixed seed so a refresh is reproducible."""
    rng = np.random.default_rng(seed)
    k = -(-n_days // block)
    starts = rng.integers(0, n_days, size=(reps, k))
    idx = ((starts[:, :, None] + np.arange(block)[None, None, :]) % n_days).reshape(reps, -1)[:, :n_days]
    counts = np.zeros((reps, n_days))
    np.add.at(counts, (np.repeat(np.arange(reps), n_days), idx.ravel()), 1.0)
    return counts


def _weighted_rank_r(w: np.ndarray, rx: np.ndarray, ry: np.ndarray) -> np.ndarray:
    """Per-row Pearson of (rx, ry) with row weights w — one coefficient per replicate."""
    sw = w.sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        mx, my = (w @ rx) / sw, (w @ ry) / sw
        cov = (w @ (rx * ry)) / sw - mx * my
        vx = (w @ (rx * rx)) / sw - mx * mx
        vy = (w @ (ry * ry)) / sw - my * my
        r = cov / np.sqrt(vx * vy)
    r[~np.isfinite(r) | (vx <= 1e-12) | (vy <= 1e-12)] = np.nan
    return r


def _calendar_values(series: dict[date, dict], cal: list[date], metric: str, shift: int) -> np.ndarray:
    out = np.full(len(cal), np.nan)
    for i, d in enumerate(cal):
        row = series.get(d + timedelta(days=shift))
        v = row.get(metric) if row is not None else None
        if v is not None:
            out[i] = float(v)
    return out


def _forward_reverse_interval(
    series: dict[date, dict], cal: list[date], counts: np.ndarray,
    metric_a: str, metric_b: str, lag_days: int, sign: float,
) -> Optional[tuple[float, float]]:
    """95% bootstrap interval of sign·(ρ_fwd − ρ_rev), or None if too few replicates resolve."""
    x = _calendar_values(series, cal, metric_a, 0)
    yf = _calendar_values(series, cal, metric_b, lag_days)
    yr = _calendar_values(series, cal, metric_b, _reverse_lag(lag_days))
    mf = ~np.isnan(x) & ~np.isnan(yf)
    mr = ~np.isnan(x) & ~np.isnan(yr)
    if mf.sum() < 5 or mr.sum() < 5:
        return None
    r_f = _weighted_rank_r(counts[:, mf], rankdata(x[mf]), rankdata(yf[mf]))
    r_r = _weighted_rank_r(counts[:, mr], rankdata(x[mr]), rankdata(yr[mr]))
    diff = sign * (r_f - r_r)
    diff = diff[np.isfinite(diff)]
    if len(diff) < REVERSE_BOOT_REPS // 2:
        return None
    lo, hi = np.percentile(diff, [2.5, 97.5])
    return float(lo), float(hi)


# Minimum observations before a pair may be REGISTERED in the finding ledger.
# Matches VALIDATION_MIN_N so a finding can never be registered on less evidence
# than is required to later judge it.
LEDGER_MIN_N = 20


def _apply_bh(results: list[dict]) -> None:
    """
    Apply Benjamini-Hochberg FDR correction in-place over all results with a raw p_value.
    Adds significant (p_adj < 0.05) and suggestive (0.05 ≤ p_adj < 0.20) boolean fields.
    """
    valid_idxs = [i for i, res in enumerate(results) if res.get("p_value") is not None]
    if valid_idxs:
        p_raw = [results[i]["p_value"] for i in valid_idxs]
        _, p_adj, _, _ = multipletests(p_raw, method="fdr_bh")
        for idx, p_adj_val in zip(valid_idxs, p_adj):
            results[idx]["p_value_adjusted"] = round(float(p_adj_val), 4)
            results[idx]["significant"] = bool(p_adj_val < 0.05)
            results[idx]["suggestive"] = bool(0.05 <= p_adj_val < 0.20)
    for res in results:
        if "p_value_adjusted" not in res:
            res["p_value_adjusted"] = None
            res["significant"] = False
            res["suggestive"] = False


def _r_label(r: Optional[float]) -> str:
    if r is None:
        return "insufficient data"
    a = abs(r)
    sign = "positive" if r > 0 else "negative"
    if a >= 0.7:
        return f"strong {sign}"
    if a >= 0.4:
        return f"moderate {sign}"
    if a >= 0.2:
        return f"weak {sign}"
    return "negligible"


async def _fetch_daily_series_uncached(
    db: AsyncSession, start: date, end: date, keep_excluded: bool = False
) -> dict[date, dict]:
    """Return a date-keyed dict of daily_metrics fields + sleep + health_metrics aggregates.

    keep_excluded: also build rows for travel/sick/… days. Correlations must NOT (those
    days swamp every behavioural effect); the daily briefing must, because it reports
    what actually happened yesterday — including on a travel day (note 55)."""
    result = await db.execute(
        select(DailyMetric)
        .where(DailyMetric.date >= start)
        .where(DailyMetric.date <= end)
        .order_by(DailyMetric.date)
    )
    dm_rows = {r.date: r for r in result.scalars().all()}

    result = await db.execute(
        select(SleepSession)
        .where(SleepSession.date >= start)
        .where(SleepSession.date <= end)
    )
    sleep_rows = {r.date: r for r in result.scalars().all()}

    result = await db.execute(
        select(SubjectiveLog)
        .where(SubjectiveLog.date >= start)
        .where(SubjectiveLog.date <= end)
    )
    subj_rows = {r.date: r for r in result.scalars().all()}

    # Whole-day exclusions (travel/sick/…) are dropped entirely; nutrition-incomplete
    # days keep their non-food signals but have their macros nulled (a partial meal
    # log understates intake and would muddy the nutrition correlations).
    from app.services.day_flags import (
        excluded_dates as _excluded_dates,
        nutrition_excluded_dates as _nutrition_excluded_dates,
    )
    excluded_days = await _excluded_dates(db, start, end)
    nutrition_excluded_days = await _nutrition_excluded_dates(db, start, end)

    # Outdoor conditions (Open-Meteo), one row per local day — see WeatherDaily.
    result = await db.execute(
        select(WeatherDaily)
        .where(WeatherDaily.date >= start)
        .where(WeatherDaily.date <= end)
    )
    weather_rows = {r.date: r for r in result.scalars().all()}

    # Local-day window bounds for naive-UTC timestamp columns (half-open)
    win_start = local_day_bounds_utc(start)[0]
    win_end = local_day_bounds_utc(end)[1]
    hm_day = local_date_sql(HealthMetric.timestamp)

    # Alcohol: sum per FOOD day — local wall-clock with the 04:00 boundary (2026-10-21).
    # Evening drinks land on the drinking day, and so do drinks after midnight: a 00:30
    # drink belongs to the night it was drunk, so lag 1 pairs it with the NEXT morning.
    alc_day = local_food_date_sql(HealthMetric.timestamp)
    result = await db.execute(
        select(
            alc_day.label("day"),
            func.sum(HealthMetric.value).label("total"),
        )
        .where(HealthMetric.metric_type == "alcohol")
        .where(HealthMetric.timestamp >= win_start)
        .where(HealthMetric.timestamp < win_end)
        .group_by(alc_day)
    )
    alcohol_by_date = {r.day: r.total for r in result.all()}  # r.day is a string

    # Naps: total daytime sleep minutes per day, from the manual nap log. Added
    # 2026-08-17 with zero history — pairs stay excluded by the n floor until data
    # accrues, so registering it now costs nothing and means the analysis starts the
    # day the first nap is logged rather than whenever someone remembers to add it.
    result = await db.execute(
        select(
            hm_day.label("day"),
            func.sum(HealthMetric.value).label("total"),
        )
        .where(HealthMetric.metric_type == "nap")
        .where(HealthMetric.timestamp >= win_start)
        .where(HealthMetric.timestamp < win_end)
        .group_by(hm_day)
    )
    nap_by_date = {r.day: r.total for r in result.all()}

    # Water: total oz per day from the in-app logger. Same treatment as naps — declared
    # now with no history so the analysis starts the day data exists, rather than
    # whenever someone remembers to wire it up.
    result = await db.execute(
        select(hm_day.label("day"), func.sum(HealthMetric.value).label("total"))
        .where(HealthMetric.metric_type == "water")
        .where(HealthMetric.timestamp >= win_start)
        .where(HealthMetric.timestamp < win_end)
        .group_by(hm_day)
    )
    water_by_date = {r.day: r.total for r in result.all()}

    # Mindful minutes: already ingested from Apple (HKMindfulSession) and, until now,
    # used by nothing. NOTE the ingestion map flags value extraction as unverified for
    # this category sample, and the 6 rows so far are all sub-3-minute — treat the
    # DURATIONS as suspect until a known session is checked against them. Registering
    # the input is still right: it costs nothing and starts the clock.
    result = await db.execute(
        select(hm_day.label("day"), func.sum(HealthMetric.value).label("total"))
        .where(HealthMetric.metric_type == "mindful_minutes")
        .where(HealthMetric.timestamp >= win_start)
        .where(HealthMetric.timestamp < win_end)
        .group_by(hm_day)
    )
    mindful_by_date = {r.day: r.total for r in result.all()}

    # Steps: sum per day
    result = await db.execute(
        select(
            hm_day.label("day"),
            func.sum(HealthMetric.value).label("total"),
        )
        .where(HealthMetric.metric_type == "steps")
        .where(HealthMetric.timestamp >= win_start)
        .where(HealthMetric.timestamp < win_end)
        .group_by(hm_day)
    )
    steps_by_date = {r.day: r.total for r in result.all()}

    # ── First-observation dates for EVENT metrics ─────────────────────────────
    #
    # These are zero-filled below because an absent day genuinely means "did not
    # happen". That is only true AFTER logging for that metric began. Before it,
    # absent means "not recorded", and filling 0 fabricates observations.
    #
    # Caught when a newly added manual metric with a handful of real rows reported
    # n≈350 against a year-long outcome: almost every pair was an invented zero, and
    # any rho was driven entirely by which few days happened to be logged. Metrics
    # with years of history were unaffected only because they predate every window.
    async def _first_seen(metric_type: str):
        day_expr = local_food_date_sql(HealthMetric.timestamp) if metric_type == "alcohol" else hm_day
        r = await db.execute(
            select(func.min(day_expr))
            .where(HealthMetric.metric_type == metric_type)
            .where(HealthMetric.value > 0)
        )
        v = r.scalar()
        return str(v) if v is not None else None

    first_seen = {m: await _first_seen(m) for m in
                  ("alcohol", "nap", "water", "mindful_minutes")}

    def _event(metric_type: str, by_date: dict, d) -> float | None:
        """0 after logging began, None before it."""
        first = first_seen.get(metric_type)
        key = str(d)
        if first is None or key < first:
            return None
        return by_date.get(key, 0.0) or 0.0

    # ── Screen time (iPhone only — the behavioral hypothesis is about phone use).
    # Per-LOCAL-day totals overall + by category (minutes). A day present here had
    # screen-time collection active; a category absent for such a day means 0 min.
    result = await db.execute(
        select(
            local_date_sql(ScreentimeEvent.start_time).label("day"),
            ScreentimeEvent.category,
            func.sum(ScreentimeEvent.duration_sec).label("secs"),
        )
        .where(ScreentimeEvent.device == "iPhone")
        .where(ScreentimeEvent.start_time >= win_start)
        .where(ScreentimeEvent.start_time < win_end)
        .group_by(local_date_sql(ScreentimeEvent.start_time), ScreentimeEvent.category)
    )
    screen_total_by_date: dict[str, float] = {}
    screen_cat_by_date: dict[tuple[str, str], float] = {}
    for r in result.all():
        mins = (r.secs or 0) / 60.0
        screen_total_by_date[r.day] = screen_total_by_date.get(r.day, 0.0) + mins
        # Accumulate, don't assign. The GROUP BY makes (day, category) unique, so a
        # plain assignment is correct for every category EXCEPT "Other": a NULL
        # category folds into "Other" here, and a day carrying both a NULL row and a
        # real "Other" row would silently keep only whichever landed last. Currently
        # dormant (0 NULL-category rows), so this is a latent bug, not an active one
        # — but it activates the moment an unmapped bundle writes a NULL.
        key = (r.category or "Other", r.day)
        screen_cat_by_date[key] = screen_cat_by_date.get(key, 0.0) + mins

    # Evening screen: iPhone minutes in the 2 h before each night's sleep onset,
    # keyed by that sleep session's (wake) date — aligns with bedtime_hour. Each night
    # uses its own bedtime window: [bedtime − 2 h, bedtime).
    # In Python since 2026-10-21: the equivalent per-session SQL join cost 1.8 s of the
    # 2.5 s series build (SQLite scanned the events per session) and made every visit to
    # Insights → Threshold Bands take ~4 s. One indexed range read + a bisect per night
    # gives the same sums.
    import bisect
    nights = (await db.execute(
        select(SleepSession.date, SleepSession.bedtime)
        .where(SleepSession.date >= start).where(SleepSession.date <= end)
        .where(SleepSession.bedtime.isnot(None))
    )).all()
    screen_evening_by_date = {}
    if nights:
        lo_t = min(b for _, b in nights) - timedelta(hours=2)
        hi_t = max(b for _, b in nights)
        ev = (await db.execute(
            select(ScreentimeEvent.start_time, ScreentimeEvent.duration_sec)
            .where(ScreentimeEvent.device == "iPhone")
            .where(ScreentimeEvent.start_time >= lo_t - timedelta(seconds=1))
            .where(ScreentimeEvent.start_time < hi_t + timedelta(seconds=1))
            .order_by(ScreentimeEvent.start_time)
        )).all()
        ts = [e[0] for e in ev]
        cum = [0]
        for e in ev:
            cum.append(cum[-1] + (e[1] or 0))
        for d, bed in nights:
            i = bisect.bisect_left(ts, bed - timedelta(hours=2))
            j = bisect.bisect_left(ts, bed)
            if j > i:   # the SQL inner join produced no row for a night with no events
                screen_evening_by_date[str(d)] = screen_evening_by_date.get(str(d), 0.0) + (cum[j] - cum[i]) / 60.0

    # Generic health-metric daily aggregate (sum or avg), keyed by LOCAL date string
    async def _hm_by_day(metric_type: str, agg):
        res = await db.execute(
            select(hm_day.label("day"), agg(HealthMetric.value).label("v"))
            .where(HealthMetric.metric_type == metric_type)
            .where(HealthMetric.timestamp >= win_start)
            .where(HealthMetric.timestamp < win_end)
            .group_by(hm_day)
        )
        return {r.day: r.v for r in res.all()}

    active_cal_by_date = await _hm_by_day("active_calories", func.sum)
    exercise_by_date   = await _hm_by_day("exercise_minutes", func.sum)
    stand_by_date      = await _hm_by_day("stand_hours", func.sum)
    resp_by_date       = await _hm_by_day("respiratory_rate", func.avg)
    daylight_by_date   = await _hm_by_day("time_in_daylight", func.sum)  # total daylight minutes
    # Mobility (iPhone-carried). Only these two enter the frame: gait speed is the
    # best-validated functional marker and, with distance, the only thing that can tell
    # "fitter" from "walking slower" when walk HR falls at constant duration. The rest
    # of the suite is ingested as a baseline but deliberately kept OUT of the FDR
    # family — see the skip-list comment in services/ingestion.
    walking_speed_by_date = await _hm_by_day("walking_speed", func.avg)
    walk_distance_by_date = await _hm_by_day("walking_running_distance", func.sum)

    # ── Daylight TIMING ───────────────────────────────────────────────────────
    #
    # `time_in_daylight` arrives at MINUTE resolution (~53k rows, each a fraction of
    # one minute spent outdoors), so when the light happened is already recorded —
    # it just was not being read. Circadian effect depends on timing, not only dose:
    # 60 minutes at 08:00 and 60 minutes at 18:00 are the same daily total and very
    # different signals to the clock.
    #
    # These are DERIVED, never stored, so they are correct over the whole history the
    # moment they exist — no migration, no backfill job.
    #
    # A minute counts as daylight at >= 0.5 so a partial minute by a window does not
    # register as "first light"; sums stay fractional and exact.
    light_rows = (await db.execute(
        select(hm_day.label("day"), HealthMetric.timestamp, HealthMetric.value)
        .where(HealthMetric.metric_type == "time_in_daylight")
        .where(HealthMetric.timestamp >= win_start)
        .where(HealthMetric.timestamp < win_end)
        .order_by(HealthMetric.timestamp)
    )).all()

    _tz = ZoneInfo(app_settings.tz)
    first_light_by_date: dict[str, float] = {}
    morning_light_by_date: dict[str, float] = {}
    light_centroid_num: dict[str, float] = {}
    light_centroid_den: dict[str, float] = {}
    for r in light_rows:
        day_key = str(r.day)
        local = to_local(r.timestamp)
        hour = local.hour + local.minute / 60.0
        val = float(r.value or 0.0)
        if val >= 0.5 and day_key not in first_light_by_date:
            first_light_by_date[day_key] = round(hour, 3)
        if hour < MORNING_LIGHT_CUTOFF_H:
            morning_light_by_date[day_key] = morning_light_by_date.get(day_key, 0.0) + val
        light_centroid_num[day_key] = light_centroid_num.get(day_key, 0.0) + hour * val
        light_centroid_den[day_key] = light_centroid_den.get(day_key, 0.0) + val
    light_centroid_by_date = {
        k: round(light_centroid_num[k] / light_centroid_den[k], 3)
        for k in light_centroid_den if light_centroid_den[k] > 0
    }
    walking_hr_by_date = await _hm_by_day("walking_heart_rate_avg", func.avg)
    breathing_by_date  = await _hm_by_day("breathing_disturbances", func.avg)
    sleep_hr_elev_by_date    = await _hm_by_day("sleep_hr_elevation", func.avg)  # derived
    sleep_hr_fixed_by_date   = await _hm_by_day("sleep_hr_early_fixed", func.avg)  # derived
    bedroom_temp_by_date     = await _hm_by_day("bedroom_temp", func.avg)      # HA bridge
    bedroom_humidity_by_date = await _hm_by_day("bedroom_humidity", func.avg)  # HA bridge
    slept_away_by_date       = await _hm_by_day("slept_away", func.avg)        # HA bridge
    partner_by_date          = await _hm_by_day("partner_present", func.avg)   # HA bridge
    wfh_by_date              = await _hm_by_day("wfh_day", func.avg)           # HA bridge
    tv_by_date               = await _hm_by_day("tv_prebed_min", func.avg)     # HA bridge
    # Earlier nights counted anyone's viewing, not his (services/tv).
    from app.services.tv import TV_CONFIRMED_WAKE_FROM
    tv_by_date = {d: v for d, v in tv_by_date.items() if str(d) >= str(TV_CONFIRMED_WAKE_FROM)}

    # ── Caffeine instances → dose + bedtime-residual (PK) ─────────────────────
    from app.models import CaffeineLog
    from app.services.caffeine import onboard_at_bed as _onboard_at_bed
    from app.services.caffeine import incomplete_dates as _caffeine_incomplete
    _caf_rows = (await db.execute(
        select(CaffeineLog).where(CaffeineLog.timestamp >= win_start)
        .where(CaffeineLog.timestamp < win_end)
    )).scalars().all()
    caffeine_by_date: dict = {}
    for r in _caf_rows:
        d = to_local_date(r.timestamp)
        caffeine_by_date.setdefault(d, []).append(r)
    caffeine_excluded = await _caffeine_incomplete(db, start, end)

    # ── Alcohol instances → bedtime residual (zero-order PK) ──────────────────
    # Distinct from alcohol_sdu (the day total): two nights with identical totals
    # leave very different amounts at sleep onset depending on pace and how early
    # drinking stopped. See services/alcohol.py — expect many exact zeros.
    from app.services.alcohol import onboard_at_bed as _alc_onboard_at_bed
    _alc_rows = (await db.execute(
        select(HealthMetric.timestamp, HealthMetric.value)
        .where(HealthMetric.metric_type == "alcohol")
        .where(HealthMetric.timestamp >= win_start)
        .where(HealthMetric.timestamp < win_end)
    )).all()
    alcohol_instances_by_date: dict = {}
    for r in _alc_rows:
        alcohol_instances_by_date.setdefault(food_day_of_utc(r.timestamp), []).append(r)

    # ── Presence gate for the weather inputs ──────────────────────────────────
    # Outdoor conditions AT HOME are meaningless on days spent elsewhere, so
    # weather inputs are nulled on away days. But the gate is only applied when
    # the presence signal is TRUSTWORTHY: if `slept_away` shows no variation over
    # the window (e.g. HA's device_tracker goes stale and reports not_home
    # forever — observed 2026-07-16→18), gating would silently discard every day
    # and produce zero weather pairs. A constant signal carries no information,
    # so we fail OPEN and log instead.
    _away_vals = [v for v in slept_away_by_date.values() if v is not None]
    _presence_trustworthy = bool(_away_vals) and 0 < sum(1 for v in _away_vals if v >= 0.5) < len(_away_vals)
    # Only worth a warning over a real window: the daily briefing reads a 2-day slice
    # (note 55), where a constant presence signal is simply normal.
    if len(_away_vals) >= 14 and not _presence_trustworthy:
        logger.warning(
            "Presence signal has no variation over the window (%d/%d days flagged away) — "
            "weather inputs NOT gated on presence. Check HA device_tracker/person entity.",
            sum(1 for v in _away_vals if v >= 0.5), len(_away_vals),
        )

    def _wx(row, attr):
        """Weather value, nulled on away days when presence is trustworthy."""
        if row is None:
            return None
        if _presence_trustworthy:
            away = slept_away_by_date.get(str(row.date))
            if away is not None and away >= 0.5:
                return None
        return getattr(row, attr)

    # Strength (Hevy): tonnage + hard sets per LOCAL day. Days after the first synced
    # Hevy workout with no session are genuine rest days → 0.0; days before Hevy data
    # begins are None (not zero) so pre-integration history can't dilute the series.
    hevy_day = local_date_sql(HevyWorkout.start_time)
    hevy_res = await db.execute(
        select(
            hevy_day.label("day"),
            func.sum(HevyWorkout.tonnage_kg).label("tonnage"),
            func.sum(HevyWorkout.working_sets).label("hard_sets"),
        )
        .group_by(hevy_day)
    )
    hevy_by_date = {r.day: r for r in hevy_res.all()}
    hevy_first_day = min(hevy_by_date) if hevy_by_date else None  # 'YYYY-MM-DD' string

    def _hevy_val(d: date, field: str) -> Optional[float]:
        if hevy_first_day is None or str(d) < hevy_first_day:
            return None
        row = hevy_by_date.get(str(d))
        v = getattr(row, field, None) if row is not None else None
        return float(v) if v is not None else 0.0

    # Nutrition macros per day (food_log)
    nut_res = await db.execute(
        select(
            food_date_sql(FoodLog.timestamp).label("day"),
            func.sum(FoodLog.calories).label("calories"),
            func.sum(FoodLog.protein_g).label("protein_g"),
            func.sum(FoodLog.carbs_g).label("carbs_g"),
            func.sum(FoodLog.fat_g).label("fat_g"),
            func.sum(FoodLog.fiber_g).label("fiber_g"),
            func.sum(FoodLog.sugar_g).label("sugar_g"),
            func.sum(FoodLog.sodium_mg).label("sodium_mg"),
        )
        .where(FoodLog.timestamp >= food_day_start(start))
        .where(FoodLog.timestamp < food_day_end(end))
        .group_by(food_date_sql(FoodLog.timestamp))
    )
    nutrition_by_date = {r.day: r for r in nut_res.all()}

    # Restaurant meals per food day (note 84, 2026-11-11): meal slots with at least one item
    # from a restaurant, cafe, bar or food truck (drinks excluded). Only from the first day any entry carries
    # the tag; before that the series is unknown, not zero. Untagged (unclear) entries count
    # as not-restaurant, so this can undercount; nutrition-incomplete days are null, as for
    # calories.
    first_tagged = (await db.execute(
        select(func.min(food_date_sql(FoodLog.timestamp))).where(FoodLog.restaurant.isnot(None))
    )).scalar()
    rest_res = await db.execute(
        select(food_date_sql(FoodLog.timestamp).label("day"),
               func.count(func.distinct(FoodLog.meal_type)).label("n"))
        .where(FoodLog.restaurant.is_(True))
        # Food only: a bar's beer or a cafe's coffee is already in alcohol / caffeine.
        .where(FoodLog.meal_type != "drinks")
        .where(FoodLog.source_kind.is_(None))
        .where(FoodLog.timestamp >= food_day_start(start))
        .where(FoodLog.timestamp < food_day_end(end))
        .group_by(food_date_sql(FoodLog.timestamp))
    )
    restaurant_by_date = {r.day: float(r.n) for r in rest_res.all()}

    # Profile + weight (kg, carry-forward) for energy_balance = intake − (BMR + active)
    settings_row = (await db.execute(select(UserSettings).limit(1))).scalar_one_or_none()

    # Expenditure model for `energy_balance`, resolved once — see the note at the
    # energy_balance assignment for why this must be the app's model and not a
    # second one. Both are day-invariant in effective_calorie_goal() too, so
    # hoisting them changes nothing except the query count.
    from app.services.energy import current_activity_factor, resolve_rmr
    energy_rmr = (await resolve_rmr(db, settings_row))["rmr"]
    energy_k = await current_activity_factor(db)
    _tz = ZoneInfo(app_settings.tz)

    def _bedtime_hour(bedtime) -> Optional[float]:
        """Local clock hour of sleep onset; early-morning bedtimes wrap above 24 for continuity."""
        if not bedtime:
            return None
        local = to_local(bedtime)
        h = local.hour + local.minute / 60.0
        return h + 24 if h < 12 else h

    # Workout-type day flags (local calendar day). Absence of a workout record = no
    # workout that day (0.0, not None) — matches the worked_out/went_for_walk derived-habit
    # convention in streaks/habits. Categories from workout_categories.categorize_activity.
    wo_day = local_date_sql(Workout.start_time)
    result = await db.execute(
        select(wo_day.label("day"), Workout.activity_type, Workout.high_intensity_min)
        .where(Workout.start_time >= win_start)
        .where(Workout.start_time < win_end)
    )
    workout_type_days: dict[str, set[str]] = {"strength": set(), "hiit": set(), "walk": set()}
    # ── Splitting the "HIIT" label by what the heart rate actually did ────────
    #
    # The gym calls every class "HIIT", so one label covers two different stimuli:
    # conditioning (burpees, box jumps, battle ropes) and lifting (curls, presses,
    # rows). Measured across 52 sessions the distinction is BIMODAL in high-intensity
    # minutes — 19 sessions at 0-4 min, 9 at 25-34, only 3 in the 15-19 valley — and it
    # matches the class titles in Hevy exactly ("Arms" vs "Athletic conditioning").
    #
    # Derived rather than relabelled deliberately: the HR signature is objective, works
    # RETROACTIVELY on all 52 historical sessions, and does not depend on remembering to
    # change a watch setting. Load itself was never affected — TRIMP comes from heart
    # rate, not from the label.
    #
    # `workout_hiit` is KEPT alongside these. It carries 5 finding-ledger entries (2
    # confirmed) and dropping it would orphan pre-registered findings. The three are
    # collinear by construction — hiit = conditioning ∪ lifting — so read them as one
    # family, not three independent results.
    # `gym_class_days` is hiit ∪ strength — "did a gym class happen", continuous across
    # any relabelling. `workout_hiit` and `workout_strength` are KEPT separate alongside
    # it: they record what the watch actually said, which is a different question and
    # carries the finding-ledger history.
    gym_hi_by_day: dict[str, float] = {}
    gym_class_days: set[str] = set()
    # HARD MINUTES (note 31, 2026-09-14): zone 4+5 minutes summed over every workout that
    # day — the continuous quantity the lifting/conditioning cut at 15 min was thresholding.
    # 0.0 on a day with no workout (same convention as the day flags); None only when the
    # day HAS a workout but none of them carries heart rate — unmeasured, not zero.
    hard_min_by_day: dict[str, float] = {}
    hard_min_unknown: set[str] = set()
    for r in result.all():
        cat = categorize_activity(r.activity_type)
        if cat in workout_type_days:
            workout_type_days[cat].add(str(r.day))
        if r.high_intensity_min is None:
            hard_min_unknown.add(str(r.day))
        else:
            hard_min_by_day[str(r.day)] = hard_min_by_day.get(str(r.day), 0.0) + float(r.high_intensity_min)
        # GATED ON "is this a gym session", NOT on "is it labelled HIIT" (fixed
        # 2026-09-09). The old `cat == "hiit"` gate meant the derived split covered only
        # the HIIT-labelled era: the user logged "Traditional Strength Training" from
        # 2025-01-15 to 2026-04-19 and "High Intensity Interval Training" from
        # 2026-03-28 on, so the two LABEL-based inputs are nearly TIME-DISJOINT and the
        # derived split silently began in March 2026. Widening the gate to hiit ∪
        # strength extends it over 115 sessions and 20 months instead of 60 and 6.
        #
        # Existing classifications do NOT change — every day already covered had a hiit
        # label and keeps its value. 55 previously-excluded days gain one.
        if cat in ("hiit", "strength") and r.high_intensity_min is not None:
            k = str(r.day)
            gym_hi_by_day[k] = max(gym_hi_by_day.get(k, 0.0), float(r.high_intensity_min))
            gym_class_days.add(k)

    conditioning_days = {d for d, hi in gym_hi_by_day.items() if hi >= CONDITIONING_HI_MIN}
    lifting_days = {d for d, hi in gym_hi_by_day.items() if hi < CONDITIONING_HI_MIN}

    # Binary habits (True→1.0, False→0.0, None→excluded from correlation)
    result = await db.execute(
        select(HabitLog)
        .where(HabitLog.date >= start)
        .where(HabitLog.date <= end)
    )
    habit_rows = {r.date: r for r in result.scalars().all()}

    def _b(v):
        """Convert boolean habit value to float, preserving None for unlogged days."""
        if v is True:  return 1.0
        if v is False: return 0.0
        return None

    # ABSENT = NO for creatine and supplements (2026-10-28): he logs only the days he
    # takes them, never a "no" — so the series was all-true and untestable. From the first
    # day each was ever logged, an unlogged day counts as not taken; before it, unlogged.
    # Other habits keep None = not logged (their "no" days are logged explicitly).
    absent_is_no_first: dict[str, date] = {}
    for attr in ("creatine", "supplements"):
        first = (await db.execute(
            select(func.min(HabitLog.date)).where(getattr(HabitLog, attr).is_(True))
        )).scalar()
        if first is not None:
            absent_is_no_first[attr] = first

    def _b_absent_no(attr: str, habit, d: date):
        first = absent_is_no_first.get(attr)
        if first is None or d < first:
            return _b(getattr(habit, attr)) if habit else None
        return 1.0 if habit is not None and getattr(habit, attr) is True else 0.0

    # Negative-control series (deterministic — every recompute sees identical values).
    # Random walk: fixed-seed increments indexed by days-since-epoch, cumulatively
    # summed, so value(d) is a stable function of the date alone.
    _ctrl_epoch = date(2020, 1, 1)
    _walk_end_idx = (end - _ctrl_epoch).days
    _walk = (np.cumsum(np.random.default_rng(20260711).standard_normal(_walk_end_idx + 1))
             if _walk_end_idx >= 0 else None)

    def _ctrl_walk(d: date) -> Optional[float]:
        i = (d - _ctrl_epoch).days
        if _walk is None or i < 0 or i >= len(_walk):
            return None
        return float(_walk[i])

    # Merge into unified per-day dicts
    series: dict[date, dict] = {}
    for d in dm_rows:
        dm = dm_rows[d]
        sl = sleep_rows.get(d)
        # The night that FOLLOWS day d. sleep_sessions.date is the WAKE date, so the
        # sleep day d's evening behaviour actually affects is the session dated d+1.
        # Used by the bedtime-residual inputs (caffeine/alcohol on board at sleep
        # onset), which must look forward, not at the previous night's bedtime.
        next_sl = sleep_rows.get(d + timedelta(days=1))
        subj = subj_rows.get(d)
        wx = weather_rows.get(d)
        # Nonstandard days are dropped entirely (as day D AND as lag source):
        # illness/travel/etc. swamp every behavioral effect on HRV/RHR/energy, and
        # behavior on such days is unrepresentative. _build_pairs skips pairs whose
        # either day is absent. Unions DayFlag reasons + the legacy sick flag.
        if d in excluded_days and not keep_excluded:
            continue
        alc = _event("alcohol", alcohol_by_date, d)
        # steps: None (not 0) when no Apple Health records exist for this day — indicates
        # non-wear or a sync gap, not a genuine rest day. Pairwise deletion correctly
        # excludes these days; using 0.0 as a default would conflate non-wear with rest.
        steps = steps_by_date.get(str(d))
        habit = habit_rows.get(d)

        sleep_total_h = None
        sleep_quality_pct = None
        if sl and sl.total_min:
            sleep_total_h = sl.total_min / 60.0
            if sl.total_min > 0:
                deep_rem = (sl.deep_min or 0) + (sl.rem_min or 0)
                sleep_quality_pct = deep_rem / sl.total_min * 100

        # Null the macros on nutrition-incomplete days — a partial meal log
        # (e.g. breakfast+lunch, no dinner) understates true intake.
        nut = None if d in nutrition_excluded_days else nutrition_by_date.get(str(d))
        intake = nut.calories if nut else None
        active_cal = active_cal_by_date.get(str(d))

        # Energy balance = intake − expenditure, using THE SAME expenditure model the
        # rest of the app shows the user: resolve_rmr() (Katch-McArdle off measured
        # lean mass, Mifflin only as fallback) plus the fitted activity factor k.
        #
        # This used to be `intake − (mifflin_bmr + raw active_calories)`, which is a
        # different model from the calorie ring and the nudges — the exact "every
        # consumer rolls its own" bug that effective_calorie_goal() was written to
        # end, still living here. Measured on real data, the two disagreed by
        # ~200 kcal on a typical day. That is NOT a constant offset Spearman would absorb — the gap
        # is 0.164 × active_calories, so it scales with exactly the term that varies
        # most, and it changed the ranking rather than shifting it.
        #
        # rmr and k are resolved ONCE above rather than per day: this reproduces
        # effective_calorie_goal()'s arithmetic exactly (its projected_active with
        # hour=None is the same local-day sum used here) while adding no per-day
        # queries to what is already the most expensive call in the engine.
        # The goal's daily_offset is deliberately NOT applied — that is a target,
        # not expenditure.
        energy_balance = None
        if intake and energy_rmr is not None and active_cal is not None:
            energy_balance = intake - (energy_rmr + energy_k * active_cal)

        series[d] = {
            # ── Outcomes (measured) ───────────────────────────────────────────
            "hrv_z_score":          dm.hrv_z_score,
            "rhr_z_score":          dm.rhr_z_score,
            "wrist_temp_deviation": dm.wrist_temp_deviation,
            "respiratory_rate":     resp_by_date.get(str(d)),
            "walking_hr":           walking_hr_by_date.get(str(d)),
            "breathing_disturbances": breathing_by_date.get(str(d)),
            "subj_energy":          subj.energy        if subj else None,
            "subj_mood":            subj.mood          if subj else None,
            "subj_sleep":           subj.sleep_quality if subj else None,
            "subj_stress":          subj.stress        if subj else None,
            "subj_day":             subj.day_score     if subj else None,
            "sleep_hr_elevation":   sleep_hr_elev_by_date.get(str(d)),   # ledger re-tests only
            "sleep_hr_early_fixed": sleep_hr_fixed_by_date.get(str(d)),
            # ── Excluded as outcomes (circular) — present for reference only ───
            "readiness_score":      dm.readiness_score,
            "sleep_debt_hours":     dm.sleep_debt_hours,
            # ── Night-of inputs ───────────────────────────────────────────────
            "sleep_total_h":        sleep_total_h,
            "sleep_quality_pct":    sleep_quality_pct,
            "bedtime_hour":         _bedtime_hour(sl.bedtime) if sl else None,
            "bedroom_temp":         bedroom_temp_by_date.get(str(d)),
            "bedroom_humidity":     bedroom_humidity_by_date.get(str(d)),
            "slept_away":           slept_away_by_date.get(str(d)),
            "partner_present":      partner_by_date.get(str(d)),
            "tv_prebed_min":        tv_by_date.get(str(d)),
            # ── Caffeine dose + bedtime residual (None on caffeine-incomplete days:
            # a partial dose log understates intake) ──────────────────────────
            #
            # BEDTIME PAIRING — FIXED 2026-08-30. `sl` is the session whose *wake*
            # date is d, so `sl.bedtime` is the PREVIOUS evening, i.e. before any of
            # day d's caffeine was drunk. onboard_at_bed skips doses at/after the
            # bedtime it is given, so the old `sl.bedtime` pairing returned exactly
            # 0.0 on every single day — verified 28 of 28 logged days — and the
            # input has been silently dead since it shipped (a constant series also
            # has zero variance, so it could never surface as a correlation either).
            # The night that FOLLOWS day d is the session dated d+1.
            "caffeine_total_mg":    (None if d in caffeine_excluded
                                     else round(sum(r.mg for r in caffeine_by_date.get(d, [])), 1)),
            "caffeine_onboard_bed": (None if (d in caffeine_excluded or not
                                              (next_sl and next_sl.bedtime))
                                     else _onboard_at_bed(caffeine_by_date.get(d, []),
                                                          next_sl.bedtime)),
            # Alcohol residual at sleep onset — same next-night pairing. Distinct
            # from alcohol_sdu (day total); expect many exact zeros, see
            # services/alcohol.py.
            "alcohol_onboard_bed":  (None if not (next_sl and next_sl.bedtime)
                                     else _alc_onboard_at_bed(
                                         alcohol_instances_by_date.get(d, []),
                                         next_sl.bedtime)),
            # ── Activity / training inputs ────────────────────────────────────
            "steps":                steps,
            "active_calories":      active_cal,
            "exercise_minutes":     exercise_by_date.get(str(d)),
            "stand_hours":          stand_by_date.get(str(d)),
            "time_in_daylight":     daylight_by_date.get(str(d)),
            "walking_speed":        walking_speed_by_date.get(str(d)),
            "walk_distance":        walk_distance_by_date.get(str(d)),
            "first_light_hour":     first_light_by_date.get(str(d)),
            "morning_light_min":    morning_light_by_date.get(str(d)),
            "light_centroid_hour":  light_centroid_by_date.get(str(d)),
            "daily_load":           dm.daily_load,
            "acwr":                 dm.acwr,
            "zone2_pct_7d":           dm.zone2_pct_7d,
            "zone3_pct_7d":           dm.zone3_pct_7d,
            "high_intensity_pct_7d":  dm.high_intensity_pct_7d,
            "hard_minutes":         (hard_min_by_day[str(d)] if str(d) in hard_min_by_day
                                     else None if str(d) in hard_min_unknown else 0.0),
            # ── Strength inputs (Hevy) ────────────────────────────────────────
            "tonnage":              _hevy_val(d, "tonnage"),
            "hard_sets":            _hevy_val(d, "hard_sets"),
            # ── Nutrition inputs ──────────────────────────────────────────────
            "calories":             intake,
            "protein_g":            nut.protein_g if nut else None,
            "carbs_g":              nut.carbs_g   if nut else None,
            "fat_g":                nut.fat_g     if nut else None,
            "fiber_g":              nut.fiber_g   if nut else None,
            "sugar_g":              nut.sugar_g   if nut else None,
            "sodium_mg":            nut.sodium_mg if nut else None,
            "energy_balance":       energy_balance,
            "restaurant_meals":     (None if nut is None or first_tagged is None or str(d) < first_tagged
                                     else restaurant_by_date.get(str(d), 0.0)),
            "wfh_day":              wfh_by_date.get(str(d)),
            # ── Workout-type day flags ────────────────────────────────────────
            "workout_strength":     1.0 if str(d) in workout_type_days["strength"] else 0.0,
            "workout_hiit":         1.0 if str(d) in workout_type_days["hiit"] else 0.0,
            "workout_walk":         1.0 if str(d) in workout_type_days["walk"] else 0.0,
            # The next three are RETIRED FROM DISCOVERY (2026-09-14, see _DAY_INPUTS) and
            # built only so the finding ledger can keep re-testing what they registered.
            # A gym class happened, whatever the watch called it. Continuous across
            # relabelling, unlike workout_hiit / workout_strength which are label-based
            # and — for this user — nearly time-disjoint (strength ≤2026-04, hiit ≥2026-03).
            "workout_gym_class":    1.0 if str(d) in (workout_type_days["hiit"]
                                                      | workout_type_days["strength"]) else 0.0,
            # None (not 0.0) on a gym day whose HR is missing — unclassifiable, which is
            # not the same as "did not happen". Keyed on the gym-class union now, so the
            # 55 strength-labelled sessions are classified rather than excluded.
            "workout_conditioning": (None if (str(d) in (workout_type_days["hiit"]
                                                         | workout_type_days["strength"])
                                              and str(d) not in gym_hi_by_day)
                                     else 1.0 if str(d) in conditioning_days else 0.0),
            "workout_lifting":      (None if (str(d) in (workout_type_days["hiit"]
                                                         | workout_type_days["strength"])
                                              and str(d) not in gym_hi_by_day)
                                     else 1.0 if str(d) in lifting_days else 0.0),
            # ── Other behavioral inputs ───────────────────────────────────────
            "alcohol_sdu":          alc,
            "nap_min":              _event("nap", nap_by_date, d),
            "water_oz":             _event("water", water_by_date, d),
            "mindful_min":          _event("mindful_minutes", mindful_by_date, d),
            # ── Outdoor conditions (None before weather collection began, and
            #    nulled on away days when presence is trustworthy — see _wx) ────
            "weather_temp_mean":      _wx(wx, "temp_mean"),
            "weather_aqi_mean":       _wx(wx, "aqi_mean"),
            "weather_uv_max":         _wx(wx, "uv_max"),
            "weather_pressure_mean":  _wx(wx, "pressure_mean"),
            "weather_pressure_delta": _wx(wx, "pressure_delta"),
            # ── Screen time (iPhone; None before collection began, else minutes;
            #    a category is 0 on a covered day with no such usage) ───────────
            "screen_total":         screen_total_by_date.get(str(d)),
            "screen_social":        (screen_cat_by_date.get(("Social", str(d)), 0.0)
                                     if str(d) in screen_total_by_date else None),
            "screen_communication": (screen_cat_by_date.get(("Communication", str(d)), 0.0)
                                     if str(d) in screen_total_by_date else None),
            "screen_entertainment": (screen_cat_by_date.get(("Entertainment", str(d)), 0.0)
                                     if str(d) in screen_total_by_date else None),
            # Evening screen: 0 is a genuine value on nights with a bedtime and
            # active collection (this or the prior local day has screen data).
            "screen_evening":       (screen_evening_by_date.get(str(d), 0.0)
                                     if (sl and sl.bedtime and
                                         (str(d) in screen_total_by_date
                                          or str(d - timedelta(days=1)) in screen_total_by_date))
                                     else None),
            # Negative controls (known-null; see _CONTROL_INPUTS)
            "ctrl_date_parity":     float(d.day % 2),
            "ctrl_random_walk":     _ctrl_walk(d),
            # Binary habits (None = not logged that day → excluded from pair)
            "habit_caffeine":       _b(habit.no_late_caffeine) if habit else None,
            "habit_light":          _b(habit.outdoor_light_am) if habit else None,
            "habit_supplements":    _b_absent_no("supplements", habit, d),
            "habit_creatine":       _b_absent_no("creatine", habit, d),
            "habit_meditation":     _b(habit.meditation)       if habit else None,
            # Continuous companions (hour of day; None unless a time was logged).
            "caffeine_last_hour":   (habit.caffeine_last_min / 60.0
                                     if habit and habit.caffeine_last_min is not None else None),
            "meditation_hour":      (habit.meditation_min / 60.0
                                     if habit and habit.meditation_min is not None else None),
        }

    # Build raw per-metric series for autocorrelation estimates. Extracted before any
    # cross-metric alignment so that missing days in one metric cannot bias the lag-1
    # autocorrelation estimate for another metric.
    sorted_dates_all = sorted(series.keys())
    all_metric_keys = list(next(iter(series.values())).keys()) if series else []
    raw_series: dict[str, list[float]] = {
        k: [float(series[d][k]) for d in sorted_dates_all if series[d].get(k) is not None]
        for k in all_metric_keys
    }
    return series, raw_series


# ── Series cache (added 2026-08-31) ──────────────────────────────────────────
# _fetch_daily_series costs ~3.3 s for a 365-day window, and the Trends tab fires
# corroboration, validation, stratified insights and threshold bands CONCURRENTLY —
# four independent calls over the same window, on a 2-core box. Measured inside a
# single refresh_correlation_cache the calls are only 2 and both windows differ, so
# the redundancy is across ENDPOINTS, not within the refresh.
#
# Single-flight matters as much as the TTL here: with a plain TTL cache four
# simultaneous requests all miss and all compute. The per-key lock makes the first
# caller compute while the rest await that same result.
#
# The TTL is deliberately short. The series is derived from daily_metrics and
# health_metrics, which change on ingestion and on recompute; 60 s covers a page-load
# burst and is far too brief for any workflow to read stale data it cares about.
_SERIES_TTL_S = 60.0
_SERIES_CACHE_MAX = 4

_series_cache: dict[tuple, tuple[float, tuple]] = {}
_series_locks: dict[tuple, "asyncio.Lock"] = {}


async def _fetch_daily_series(db: AsyncSession, start: date, end: date):
    """Cached, single-flighted wrapper over _fetch_daily_series_uncached.

    Returns the SAME objects to every caller within the TTL, so callers must treat
    the result as read-only — every current caller already does.
    """
    import asyncio
    import time

    key = (start, end)
    now = time.monotonic()

    hit = _series_cache.get(key)
    if hit is not None and now - hit[0] < _SERIES_TTL_S:
        return hit[1]

    lock = _series_locks.setdefault(key, asyncio.Lock())
    async with lock:
        # Re-check: a concurrent caller may have filled it while we waited.
        hit = _series_cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < _SERIES_TTL_S:
            return hit[1]

        value = await _fetch_daily_series_uncached(db, start, end)
        _series_cache[key] = (time.monotonic(), value)

        # Drop expired entries, then the oldest, so a long-lived process cannot
        # accumulate windows (each is a year of ~80 metrics).
        cutoff = time.monotonic() - _SERIES_TTL_S
        for k in [k for k, (t, _) in _series_cache.items() if t < cutoff]:
            _series_cache.pop(k, None)
            _series_locks.pop(k, None)
        while len(_series_cache) > _SERIES_CACHE_MAX:
            oldest = min(_series_cache, key=lambda k: _series_cache[k][0])
            _series_cache.pop(oldest, None)
            _series_locks.pop(oldest, None)
        return value


def invalidate_series_cache() -> None:
    """Drop the cache. Call after anything that rewrites daily_metrics wholesale."""
    _series_cache.clear()



def _rank_profiles(raw_series: dict[str, list[float]]) -> dict[str, Optional[dict]]:
    """
    One _acf_profile() per metric, over its rank-transformed raw series.

    Both the rank transform and the ACF are properties of a single metric, so
    hoisting them out of the pair loop is pure deduplication — no pair sees a
    different number than it would have computed for itself.
    """
    return {k: _acf_profile(rankdata(v).tolist() if len(v) >= 2 else v)
            for k, v in raw_series.items()}


def _build_pairs(
    series: dict[date, dict],
    metric_a: str,
    metric_b: str,
    lag_days: int,
) -> tuple[list[float], list[float]]:
    """
    Build aligned (a_values, b_values) for the correlation calculation.
    For lag > 0: a comes from day D, b comes from day D+lag.
    Both values must be non-None.
    """
    sorted_dates = sorted(series.keys())
    date_set = set(sorted_dates)
    xs, ys = [], []
    for d in sorted_dates:
        d_lag = d + timedelta(days=lag_days)
        if d_lag not in date_set:
            continue
        a_val = series[d].get(metric_a)
        b_val = series[d_lag].get(metric_b)
        if a_val is not None and b_val is not None:
            xs.append(float(a_val))
            ys.append(float(b_val))
    return xs, ys


# Frame-metric name -> the health_metrics type it is built from. Only inputs whose
# coverage is genuinely in question are listed: a metric that has run for years does not
# need a "recently added" note, and claiming one would be noise.
COVERAGE_SOURCES = {
    "nap_min": "nap", "water_oz": "water",
    "alcohol_sdu": "alcohol",
    "mindful_min": "mindful_minutes", "tv_prebed_min": "tv_prebed_min",
    "slept_away": "slept_away", "wfh_day": "wfh_day",
    "bedroom_temp": "bedroom_temp", "bedroom_humidity": "bedroom_humidity",
    "first_light_hour": "time_in_daylight", "morning_light_min": "time_in_daylight",
    "light_centroid_hour": "time_in_daylight", "time_in_daylight": "time_in_daylight",
}


async def metric_coverage(db: AsyncSession) -> dict:
    """First observed local date and day-count per metric, for "recently added" notes.

    A pair sitting at n=9 because the metric is eight days old is a completely different
    statement from one at n=9 after a year of collection, and the view cannot tell them
    apart from n alone.
    """
    day = local_date_sql(HealthMetric.timestamp)
    rows = (await db.execute(
        select(HealthMetric.metric_type,
               func.min(day).label("first"),
               func.count(func.distinct(day)).label("days"))
        .where(HealthMetric.value > 0)
        .where(HealthMetric.metric_type.in_(set(COVERAGE_SOURCES.values())))
        .group_by(HealthMetric.metric_type)
    )).all()
    by_type = {r.metric_type: {"first": str(r.first), "days": r.days} for r in rows}
    return {frame: by_type[src] for frame, src in COVERAGE_SOURCES.items() if src in by_type}


async def compute_correlations(
    db: AsyncSession,
    days: int = 365,
    offset_days: int = 0,
    with_reverse: bool = True,
) -> list[dict]:
    """
    Compute all defined metric pair correlations over a `days`-long window ending
    `offset_days` before today.

    `with_reverse` adds the reverse-lag placebo fields (see REVERSE_BOOT_REPS). The
    year-over-year pass turns it off — it only reads spearman_r and n.

    `offset_days` exists for the year-over-year drift check: offset_days=365 with
    days=365 yields the same statistic over the PREVIOUS year, which is directly
    comparable because both windows span a whole seasonal cycle.

    Each result includes autocorrelation-corrected effective N, a raw p-value computed
    using the corrected degrees of freedom, and Fisher z 95% CIs. Benjamini-Hochberg
    FDR correction is applied across all 189 valid pairs before returning.

    Returns a list of result dicts (not persisted — caller decides whether to cache).
    """
    end = local_today() - timedelta(days=offset_days)
    start = end - timedelta(days=days)
    series, raw_series = await _fetch_daily_series(db, start, end)
    profiles = _rank_profiles(raw_series)

    results = []
    for metric_a, metric_b, lag_days in CORRELATION_PAIRS:
        xs, ys = _build_pairs(series, metric_a, metric_b, lag_days)
        raw_x = raw_series.get(metric_a, [])
        raw_y = raw_series.get(metric_b, [])
        corr_result = _compute_corr(xs, ys, raw_x, raw_y,
                                    profiles.get(metric_a), profiles.get(metric_b))
        if corr_result is not None:
            r_pearson, r_spearman, p_val, n_eff, ci_low, ci_high = corr_result
        else:
            r_pearson = r_spearman = p_val = n_eff = ci_low = ci_high = None

        results.append({
            "metric_a":       metric_a,
            "metric_b":       metric_b,
            "metric_a_label": METRIC_LABELS.get(metric_a, metric_a),
            "metric_b_label": METRIC_LABELS.get(metric_b, metric_b),
            "lag_days":       lag_days,
            "pearson_r":      round(r_pearson, 3) if r_pearson is not None else None,
            "spearman_r":     round(r_spearman, 3) if r_spearman is not None else None,
            "n":              len(xs),
            "n_eff":          n_eff,
            "p_value":        round(p_val, 4) if p_val is not None else None,
            "ci_low":         round(ci_low, 3) if ci_low is not None else None,
            "ci_high":        round(ci_high, 3) if ci_high is not None else None,
            "relationship":   _r_label(r_spearman),
            "days_window":    days,
            "offset_days":    offset_days,
            "untestable_reason": (untestable_reason(metric_a, xs, ys)
                                  if corr_result is None else None),
        })
        if with_reverse:
            rev_lag = _reverse_lag(lag_days)
            rx, ry = _build_pairs(series, metric_a, metric_b, rev_lag)
            rev = _compute_corr(rx, ry, raw_x, raw_y,
                                profiles.get(metric_a), profiles.get(metric_b))
            results[-1].update({
                "reverse_lag_days":   rev_lag,
                "reverse_spearman_r": round(rev[1], 3) if rev else None,
                "reverse_n":          len(rx),
                "reverse_ci_low":     round(rev[4], 3) if rev else None,
                "reverse_ci_high":    round(rev[5], 3) if rev else None,
                "fwd_rev_diff":       None,
                "fwd_rev_diff_low":   None,
                "fwd_rev_diff_high":  None,
                "forward_exceeds_reverse": None,
            })

    _apply_bh(results)

    if with_reverse and series:
        tested = [r for r in results
                  if (r["significant"] or r["suggestive"]) and r["spearman_r"]
                  and r["reverse_spearman_r"] is not None]
        if tested:
            first, last = min(series), max(series)
            cal = [first + timedelta(days=i) for i in range((last - first).days + 1)]
            counts = _block_counts(len(cal))
            for r in tested:
                sign = 1.0 if r["spearman_r"] > 0 else -1.0
                r["fwd_rev_diff"] = round(sign * (r["spearman_r"] - r["reverse_spearman_r"]), 3)
                iv = _forward_reverse_interval(series, cal, counts, r["metric_a"],
                                               r["metric_b"], r["lag_days"], sign)
                if iv is not None:
                    r["fwd_rev_diff_low"], r["fwd_rev_diff_high"] = round(iv[0], 3), round(iv[1], 3)
                    r["forward_exceeds_reverse"] = iv[0] > 0
    for r in results:
        r["reverse_check"] = reverse_check_label(r)
        r["survivor_details"] = None
    if with_reverse and series:
        for r in results:
            if r["significant"] or r["suggestive"]:
                try:
                    r["survivor_details"] = survivor_details(series, r["metric_a"], r["metric_b"],
                                                             r["lag_days"])
                except Exception as exc:  # noqa: BLE001 — details are optional, never fatal
                    logger.warning("survivor details failed for %s→%s: %s",
                                   r["metric_a"], r["metric_b"], exc)
    return results


# Fixed clock cutoff for "morning" light. A wake-relative window (light within 2 h of
# rising) is the more circadian-correct definition, but it depends on a sleep session
# existing for that date; this one is computable from daylight data alone, which is what
# makes the whole feature free.
MORNING_LIGHT_CUTOFF_H = 10.0

# Minimum observations that must differ from the most common value, per series. Below
# this a coefficient is describing a handful of points dressed up as a large sample.
MIN_SUPPORT_OBS = 10

# High-intensity minutes separating a conditioning class from a lifting class. Set at
# the valley of an observed bimodal distribution (19 sessions 0-4 min, 9 at 25-34, only
# 3 between 15 and 19), not chosen a priori.
# Re-exported, not redefined: the canonical home is app.workout_categories, where the
# Training tab and the summaries' period table also read it. Importers doing
# `from app.services.correlations import CONDITIONING_HI_MIN` keep working.
from app.workout_categories import CONDITIONING_HI_MIN  # noqa: E402,F401
from app.foodday import food_date_sql, food_day_start, food_day_end

SIGNIFICANCE_THRESHOLD = 0.3  # |r| at which a correlation becomes "notable"

# The bar for "what to act on", DELIBERATELY LOWER than SIGNIFICANCE_THRESHOLD
# (design call, 2026-09-07, note 16).
#
# At 0.3 the Findings tab listed nine surviving pairs and then acted on three, with no
# explanation of the gap. Five of the six excluded rows failed on |r| alone despite
# being FDR-significant at n > 340 — real effects, just small ones (rho ~0.2 is ~4% of
# variance). The argument for including them: for a lever as cheap as skipping a drink,
# a small reliable effect is still worth acting on, and hiding it while listing it two
# sections above is the actual defect.
#
# SIGNIFICANCE_THRESHOLD stays at 0.3 and still governs the ntfy "newly significant"
# alert and the AI summaries' confirmed-lever block, which have limited words and should
# carry only the strongest findings. Do NOT collapse these two constants.
ACTION_THRESHOLD = 0.2

# ── Newsworthiness gate for the daily briefing (added 2026-08-30) ─────────────
# The finding_ledger tiers are PURE p-VALUE tiers — confirmed is p_adj < 0.05,
# suggestive is 0.05 ≤ p_adj < 0.20 — with no effect-size floor. That was fine at
# small n and is not fine now: with n in the hundreds, trivial effects clear them.
# 31 of 75 ledger crossings sit below |ρ| 0.3, including CONFIRMED ones at |ρ| ≈ 0.2
# — about 4% of variance, which supports no action at all.
#
# events_block fed those to the daily summary as "New suggestive finding", and the
# prompt requires every correlation to be labelled New/Firming/Known. So the app
# manufactured news and then obliged the model to report it — the actual cause of
# the "talking for the sake of saying something" complaint, and something no
# prompt rewrite could fix from downstream.
#
# THE DRIFT TERM. Pyper-Peterman computes n_eff by dividing out
# 1 + 2·Σ(1-k/n)·ρx(k)·ρy(k) — a PRODUCT of the two series' autocorrelations, so it
# collapses only when BOTH series drift together. n_eff/n is therefore already a
# direct measure of shared drift, needing no new statistics.
#
# Calibrated against the negative controls rather than guessed: the one
# ctrl_random_walk pair that reaches |ρ| ≥ 0.3 (→ subj_mood, ρ ≈ +0.57) has
# n_eff/n = 0.09, the most extreme drift signature in the dataset, while
# ctrl_date_parity — a NON-drifting null — sits at 1.00 and real pairs at a median
# of 0.94. Of the 44 real pairs passing |ρ| and n, this drops exactly 2, and any cut
# from 0.3 to 0.6 drops the same 2 (the distribution is bimodal, so the threshold is
# not a knife edge). Both are textbook artifacts: active_calories → respiratory_rate
# (part of a known X→resp-rate cluster) and zone2_pct_7d → subj_mood,
# a 7-day rolling window correlated against anything via shared smoothness.
NEWS_MIN_N = 30
NEWS_MIN_NEFF_RATIO = 0.5

# ── Controllability (added 2026-08-30) ───────────────────────────────────────
# Gating on effect size alone still let the briefing turn a REAL correlation into
# an invented instruction: weather_pressure_delta → wrist_temp_deviation (ρ=+0.55)
# came back as "Watch for thermal drift today." The statistics were fine; the
# problem is that barometric pressure is not a thing you can do.
#
# build_actionable_insights already splits lever-vs-diagnostic, but on the OUTCOME
# (subjective ⇒ actionable, physiological ⇒ context) — nothing looks at whether the
# INPUT is controllable, so an ambient input against a subjective outcome (AQI →
# day quality, ρ=+0.39) still reads as a lever.
#
# Deliberately CONSERVATIVE: only things unambiguously outside his control are
# listed, and everything else defaults to a lever. A wrongly-suppressed real lever
# costs more than an over-permissive tag, since the tag only changes how a finding
# is framed, never whether it is shown. Bedroom temp/humidity are NOT here — a
# window, AC or dehumidifier makes those genuinely actionable.
CONTEXT_INPUTS = frozenset({
    "weather_temp_mean", "weather_aqi_mean", "weather_uv_max",
    "weather_pressure_mean", "weather_pressure_delta",
    "partner_present",          # someone else's presence, not a choice
    "ctrl_date_parity", "ctrl_random_walk",   # negative controls: never actionable
})


def is_controllable(metric: str) -> bool:
    """Can he do something about this today? Drives lever-vs-context framing only."""
    return metric not in CONTEXT_INPUTS


def is_newsworthy(rho: float | None, n: int | None, n_eff: int | None) -> bool:
    """Is a correlation worth leading a daily briefing with?

    Deliberately STRICTER than significant/suggestive: those answer "is this
    distinguishable from zero", this answers "is this big enough, measured on
    enough independent days, to act on today". A pair can be highly significant
    and still fail here, which is the point.
    """
    if rho is None or n is None or abs(rho) < SIGNIFICANCE_THRESHOLD:
        return False
    if n < NEWS_MIN_N:
        return False
    # No n_eff means the ACF was uncomputable (too short / zero-variance). Treat as
    # unverifiable rather than clean — the gate exists to keep noise out.
    if not n_eff or n_eff / n < NEWS_MIN_NEFF_RATIO:
        return False
    return True


async def refresh_correlation_cache(days: int = 365) -> list[dict]:
    """
    Recompute all correlations and upsert into insight_cache.
    Returns a list of actionable insights that newly crossed SIGNIFICANCE_THRESHOLD
    (i.e. were below it before this refresh but are at or above it now).
    Called by the weekly scheduler job.
    """
    from app.database import AsyncSessionLocal
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    async with AsyncSessionLocal() as db:
        # Snapshot old values before overwriting
        old_cache = await read_correlation_cache()
        old_significant: set[tuple] = {
            (row["metric_a"], row["metric_b"], row["lag_days"])
            for row in old_cache
            if row.get("significant")
        }

        results = await compute_correlations(db, days=days)

        # Same pairs, previous window. Wrapped because for most pairs the prior year
        # simply does not exist yet (watch data starts 2025-01, check-ins 2026-04) —
        # a missing comparison must degrade to "no drift check available", never to
        # a spurious zero.
        prev_by_pair: dict[tuple, dict] = {}
        try:
            prev = await compute_correlations(db, days=days, offset_days=days,
                                              with_reverse=False)
            prev_by_pair = {(r["metric_a"], r["metric_b"], r["lag_days"]): r for r in prev}
        except Exception as exc:
            logger.warning("year-over-year comparison pass failed: %s", exc)

        now = datetime.utcnow()
        for r in results:
            stmt = sqlite_insert(InsightCache).values(
                metric_a=r["metric_a"],
                metric_b=r["metric_b"],
                lag_days=r["lag_days"],
                pearson_r=r["pearson_r"],
                spearman_r=r.get("spearman_r"),
                n=r["n"],
                n_eff=r.get("n_eff"),
                p_value=r.get("p_value"),
                ci_low=r.get("ci_low"),
                ci_high=r.get("ci_high"),
                days_window=days,
                prev_spearman_r=(prev_by_pair.get(
                    (r["metric_a"], r["metric_b"], r["lag_days"]), {}) or {}).get("spearman_r"),
                prev_n=(prev_by_pair.get(
                    (r["metric_a"], r["metric_b"], r["lag_days"]), {}) or {}).get("n"),
                **{k: r.get(k) for k in CACHE_EXTRA_FIELDS},
                computed_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["metric_a", "metric_b", "lag_days"],
                set_={
                    "pearson_r":   r["pearson_r"],
                    "spearman_r":  r.get("spearman_r"),
                    "n":           r["n"],
                    "n_eff":       r.get("n_eff"),
                    "p_value":     r.get("p_value"),
                    "ci_low":      r.get("ci_low"),
                    "ci_high":     r.get("ci_high"),
                    "days_window": days,
                    "prev_spearman_r": (prev_by_pair.get(
                        (r["metric_a"], r["metric_b"], r["lag_days"]), {}) or {}).get("spearman_r"),
                    "prev_n": (prev_by_pair.get(
                        (r["metric_a"], r["metric_b"], r["lag_days"]), {}) or {}).get("n"),
                    **{k: r.get(k) for k in CACHE_EXTRA_FIELDS},
                    "computed_at": now,
                },
            )
            await db.execute(stmt)

        # Prune stale rows: pairs that were removed from CORRELATION_PAIRS (e.g. after a
        # redefinition) would otherwise linger forever with outdated r and pollute the
        # actionable view, since they're never recomputed.
        current_keys = {(a, b, lag) for a, b, lag in CORRELATION_PAIRS}
        for row in (await db.execute(select(InsightCache))).scalars().all():
            if (row.metric_a, row.metric_b, row.lag_days) not in current_keys:
                await db.delete(row)

        # Pre-registration ledger: record the FIRST time a pair crosses each tier
        # (with ρ and n at that moment). Never overwritten — the prospective-
        # validation holdout clock starts at these dates (see build_validation).
        # Ledger rows are never pruned: a lapsed finding keeps its history.
        today_d = local_today()
        ledger = {(l.metric_a, l.metric_b, l.lag_days): l
                  for l in (await db.execute(select(FindingLedger))).scalars().all()}
        for r in results:
            if not (r.get("significant") or r.get("suggestive")):
                continue
            # Minimum-n gate, added 2026-08-15. Without it the ledger recorded
            # registrations at n=6 (rho=+0.985, high_intensity_pct_7d x subj_day,
            # 2026-07-18) — at n=6 you need |rho| >= 0.81 for uncorrected p<0.05, so
            # scanning ~200 pairs makes such a hit near-certain by chance. Because
            # ledger rows are never overwritten or pruned, one bad registration is
            # permanent, and the prospective-validation clock starts from it.
            #
            # Applied FORWARD ONLY. Existing sub-floor rows are deliberately left
            # alone: the ledger is a pre-registration instrument, and editing it
            # after seeing outcomes destroys the guarantee it exists to provide.
            # They are labelled in the UI instead, and their holdout columns already
            # expose them (that n=6 entry has since gone to rho=-0.22).
            if (r.get("n") or 0) < LEDGER_MIN_N:
                continue
            key = (r["metric_a"], r["metric_b"], r["lag_days"])
            led = ledger.get(key)
            if led is None:
                # Stamp the window that measured it. An entry registered under 180
                # days and one under 365 are not the same object, and a ledger that
                # cannot tell them apart cannot support prospective validation.
                led = FindingLedger(metric_a=key[0], metric_b=key[1], lag_days=key[2],
                                    days_window=days)
                db.add(led)
                ledger[key] = led
            if led.first_suggestive_at is None:
                led.first_suggestive_at = today_d
                led.rho_at_suggestive = r.get("spearman_r")
                led.n_at_suggestive = r.get("n")
            if r.get("significant") and led.first_confirmed_at is None:
                led.first_confirmed_at = today_d
                led.rho_at_confirmed = r.get("spearman_r")
                led.n_at_confirmed = r.get("n")

        await db.commit()

    logger.info("Correlation cache refreshed: %d pairs over %d days", len(results), days)

    # Detect pairs that newly became statistically significant (BH-corrected p < 0.05)
    newly_significant = []
    for r in results:
        if r.get("spearman_r") is None:
            continue
        was_significant = (r["metric_a"], r["metric_b"], r["lag_days"]) in old_significant
        now_significant = r.get("significant", False) and abs(r["spearman_r"]) >= SIGNIFICANCE_THRESHOLD
        if now_significant and not was_significant:
            newly_significant.append(r)

    if newly_significant:
        actionable = build_actionable_insights(
            newly_significant, min_abs_r=SIGNIFICANCE_THRESHOLD, min_n=20,
        )
        all_items = (actionable["do_more"] + actionable["do_less"]
                     + actionable["diagnostic"])
        logger.info(
            "Correlation alert: %d pair(s) newly crossed r=%.1f",
            len(all_items), SIGNIFICANCE_THRESHOLD,
        )
        return all_items

    return []


# ── Actionable insights ───────────────────────────────────────────────────────

# Things the user controls — key → {label, subject (sentence-friendly noun phrase that
# reads naturally after "More"/"Less")}
INPUTS: dict[str, dict] = {
    # Sleep / timing
    "sleep_total_h":     {"label": "Sleep duration",     "subject": "sleep"},
    "sleep_quality_pct": {"label": "Deep+REM %",         "subject": "deep+REM sleep"},
    "bedtime_hour":      {"label": "Bedtime",            "subject": "late bedtimes"},
    # Activity / training
    "steps":             {"label": "Steps",              "subject": "daily steps"},
    "active_calories":   {"label": "Active calories",    "subject": "active-calorie burn"},
    "exercise_minutes":  {"label": "Exercise minutes",   "subject": "exercise minutes"},
    "stand_hours":       {"label": "Stand hours",        "subject": "standing hours"},
    "daily_load":        {"label": "Training load",      "subject": "training load (TRIMP)"},
    "acwr":              {"label": "ACWR",               "subject": "acute training load (ACWR)"},
    "zone2_pct_7d":           {"label": "Zone 2 % (7d)",          "subject": "Zone 2 training"},
    "zone3_pct_7d":           {"label": "Zone 3 % (7d)",          "subject": "Zone 3 training"},
    "high_intensity_pct_7d":  {"label": "High-intensity % (7d)",  "subject": "high-intensity training"},
    "hard_minutes":      {"label": "Hard minutes",      "subject": "hard training minutes (zones 4–5)"},
    "tonnage":           {"label": "Strength tonnage",  "subject": "lifting volume (tonnage)"},
    "hard_sets":         {"label": "Hard sets",         "subject": "hard strength sets"},
    # Nutrition
    "calories":          {"label": "Calorie intake",     "subject": "calorie intake"},
    "protein_g":         {"label": "Protein",            "subject": "protein"},
    "carbs_g":           {"label": "Carbs",              "subject": "carbohydrates"},
    "fat_g":             {"label": "Fat",                "subject": "dietary fat"},
    "fiber_g":           {"label": "Fiber",              "subject": "fiber"},
    "sugar_g":           {"label": "Sugar",              "subject": "sugar"},
    "sodium_mg":         {"label": "Sodium",             "subject": "sodium"},
    "energy_balance":    {"label": "Energy balance",     "subject": "calorie surplus"},
    "restaurant_meals":  {"label": "Restaurant meals",   "subject": "restaurant meals"},
    # Environment (HA bridge)
    "bedroom_temp":      {"label": "Bedroom temp",       "subject": "bedroom temperature"},
    "bedroom_humidity":  {"label": "Bedroom humidity",   "subject": "bedroom humidity"},
    "slept_away":        {"label": "Away night",         "subject": "nights away from home"},
    "partner_present":   {"label": "Partner present",    "subject": "nights with partner present"},
    "wfh_day":           {"label": "WFH day",            "subject": "working from home"},
    "tv_prebed_min":     {"label": "TV before bed",      "subject": "pre-bed TV time"},
    # Workout type
    "workout_strength":  {"label": "Strength day",       "subject": "strength-training days"},
    "workout_hiit":      {"label": "HIIT day",           "subject": "HIIT days"},
    # workout_conditioning / workout_lifting / workout_gym_class retired 2026-09-14 (note 31).
    "workout_walk":      {"label": "Walk day",           "subject": "walk days"},
    # Other behavioral
    "alcohol_sdu":       {"label": "Alcohol",            "subject": "alcohol"},
    "nap_min":           {"label": "Napping",            "subject": "daytime napping"},
    "water_oz":          {"label": "Water",              "subject": "water intake"},
    "mindful_min":       {"label": "Mindful minutes",    "subject": "meditation time"},
    "weather_temp_mean":     {"label": "Outdoor temp",      "subject": "warmer days"},
    "weather_aqi_mean":      {"label": "Air quality (AQI)", "subject": "poor air quality"},
    "weather_uv_max":        {"label": "UV index",          "subject": "high-UV days"},
    "weather_pressure_mean": {"label": "Barometric pressure", "subject": "barometric pressure"},
    "weather_pressure_delta": {"label": "Pressure swing",   "subject": "big pressure swings"},
    "screen_total":      {"label": "Phone screen time",  "subject": "phone screen time"},
    "screen_social":     {"label": "Social-media time",  "subject": "social-media use"},
    "screen_communication": {"label": "Messaging time",  "subject": "messaging-app use"},
    "screen_entertainment": {"label": "Entertainment time", "subject": "entertainment screen time"},
    "screen_evening":    {"label": "Screen before bed",  "subject": "phone use before bed"},
    "habit_caffeine":    {"label": "No late caffeine",   "subject": "avoiding late caffeine"},
    "habit_light":       {"label": "Outdoor light AM",   "subject": "morning outdoor light"},
    # Added 2026-08-06 by the registry audit — same silent-drop bug as
    # breathing_disturbances. It has been in _DAY_INPUTS and the series builder all
    # along, so 12 pairs were computed and cached every run and then discarded on
    # read because this dict lacked the entry. Distinct from `habit_light`, which is
    # the BINARY morning-window credit; this is total daylight MINUTES across the day,
    # so the two answer different questions and both are worth keeping.
    "time_in_daylight":  {"label": "Daylight (min)",     "subject": "time in daylight"},
    "walking_speed":     {"label": "Walking speed",      "subject": "how fast you walk"},
    "walk_distance":     {"label": "Walking distance",   "subject": "distance walked"},
    "first_light_hour":  {"label": "First daylight",     "subject": "how early you get outside"},
    "morning_light_min": {"label": "Morning daylight",   "subject": "morning daylight"},
    "light_centroid_hour": {"label": "Daylight midpoint", "subject": "when your daylight falls"},
    "habit_supplements": {"label": "Supplements",        "subject": "taking supplements"},
    "habit_creatine":    {"label": "Creatine",           "subject": "taking creatine"},
    "habit_meditation":  {"label": "Meditation",         "subject": "meditation"},
    "caffeine_last_hour": {"label": "Last caffeine (hour)", "subject": "later caffeine"},
    "meditation_hour":   {"label": "Meditation time",    "subject": "meditation timing"},
    "caffeine_total_mg": {"label": "Caffeine dose",      "subject": "higher caffeine dose"},
    "caffeine_onboard_bed": {"label": "Caffeine at bedtime", "subject": "caffeine still on board at bedtime"},
    "alcohol_onboard_bed": {"label": "Alcohol at bedtime", "subject": "alcohol still on board at bedtime"},
}

# Things the user wants to improve — key → {label, higher_is_better}
# readiness_score / sleep_debt are intentionally excluded as outcomes: they are
# algebraically computed from the inputs (HRV/RHR/sleep/ACWR), so correlating any input
# with them is circular by construction. wrist_temp_deviation and respiratory_rate are
# measured recovery/illness markers where elevated = worse.
OUTCOMES: dict[str, dict] = {
    "hrv_z_score":          {"label": "HRV",            "higher_is_better": True},
    "rhr_z_score":          {"label": "RHR",            "higher_is_better": False},
    "wrist_temp_deviation": {"label": "Wrist temp",     "higher_is_better": False},
    "respiratory_rate":     {"label": "Respiratory rate", "higher_is_better": False},
    # Added 2026-08-05. Both were already in _OUTCOME_KEYS and the series builder, so
    # 67 pairs each were being computed and cached every run — then dropped on the
    # floor, because build_actionable_insights skips any pair whose outcome is not in
    # THIS dict. Purely a registry omission; no new computation, no new multiplicity
    # (the BH family already included them). Physiological, so they route to
    # Diagnostic Signals rather than do-more/do-less directives.
    "breathing_disturbances": {"label": "Breathing disturbances", "higher_is_better": False},
    "walking_hr":           {"label": "Walking HR",       "higher_is_better": False},
    "subj_energy":          {"label": "Energy",         "higher_is_better": True},
    "subj_mood":            {"label": "Mood",           "higher_is_better": True},
    "subj_sleep":           {"label": "Sleep quality",  "higher_is_better": True},
    "subj_stress":          {"label": "Stress",         "higher_is_better": False},
    "subj_day":             {"label": "Day quality",    "higher_is_better": True},
    # Retired from discovery 2026-10-27; kept so ledger rows registered on it still label.
    "sleep_hr_elevation":   {"label": "Early-night HR (thirds)", "higher_is_better": False},
    "sleep_hr_early_fixed": {"label": "Early-night HR", "higher_is_better": False},
}


# ── Registry parity guard ─────────────────────────────────────────────────────
# A metric can be fully wired into the SERIES (a lag list + the row dict) and still
# be missing from INPUTS/OUTCOMES, which are what build_actionable_insights filters
# on. When that happens the pairs are computed, cached and then silently discarded —
# nothing errors, results just never appear. It has bitten twice:
#   2026-08-05  breathing_disturbances + walking_hr  (67 pairs each)
#   2026-08-06  time_in_daylight                     (12 pairs)
# Log loudly at import so the third time is caught in seconds rather than by audit.
def _check_registry_parity() -> list[str]:
    problems = []
    lagged = set(_NIGHT_INPUTS) | set(_DAY_INPUTS) | set(_STRENGTH_INPUTS)
    orphan_inputs = lagged - set(INPUTS)
    if orphan_inputs:
        problems.append(
            f"paired but missing from INPUTS (computed then dropped): {sorted(orphan_inputs)}")
    orphan_outcomes = set(_OUTCOME_KEYS) - set(OUTCOMES)
    if orphan_outcomes:
        problems.append(
            f"in _OUTCOME_KEYS but missing from OUTCOMES (computed then dropped): "
            f"{sorted(orphan_outcomes)}")
    unpaired = set(INPUTS) - lagged
    if unpaired:
        problems.append(f"in INPUTS but in no lag list (never paired): {sorted(unpaired)}")
    unclassified = set(INPUTS) - set(INPUT_CONTROL)
    if unclassified:
        problems.append(
            f"in INPUTS but missing from INPUT_CONTROL (defaulting to "
            f"'{DEFAULT_INPUT_CONTROL}', which may put them in the wrong section): "
            f"{sorted(unclassified)}")
    stray = set(INPUT_CONTROL) - set(INPUTS)
    if stray:
        problems.append(f"in INPUT_CONTROL but not in INPUTS (dead entries): {sorted(stray)}")
    return problems




# Outcomes the user experiences directly. NO LONGER the routing rule (see INPUT_CONTROL
# below) — it now only decides ORDER within "what to act on", so the things you feel lead
# and the physiological consequences follow.
_SUBJECTIVE_OUTCOME_KEYS = {"subj_energy", "subj_mood", "subj_sleep", "subj_stress", "subj_day"}


# ── Controllability: what actually decides "can I act on this?" ───────────────
#
# ROUTING USED TO BE ON THE OUTCOME AND THAT WAS THE WRONG AXIS (fixed 2026-09-07,
# note 16). `build_actionable_insights` asked "is the outcome subjective?", so
# `alcohol -> respiratory rate` (among the best-powered findings in the whole
# engine) was filed as a non-actionable "diagnostic signal", while
# `sleep_total_h -> subj_sleep` was filed as actionable even though sleep duration is
# far less directly controllable than whether you have a drink.
#
# Actionability is a property of the INPUT — can I change it? The outcome only says why
# you would care, and now only affects wording and ordering.
#
#   direct   — you decide it outright (what you drink, eat, log, when you train)
#   indirect — you influence it but cannot set it (you can go to bed; you cannot will
#              sleep. You can run the AC; the weather still votes.)
#   none     — observed, not chosen. Weather, and gait markers that are readouts rather
#              than dials. These stay in Diagnostic Signals, which is what that section
#              is actually for.
#
# direct and indirect BOTH route to do-more/do-less; the split is kept because it is
# real and because a future "only show me things I fully control" filter is one line.
INPUT_CONTROL: dict[str, str] = {
    # Intake — the most directly controllable things here
    "alcohol_sdu": "direct", "alcohol_onboard_bed": "direct",
    "caffeine_total_mg": "direct", "caffeine_last_hour": "direct",
    "caffeine_onboard_bed": "direct",
    "calories": "direct", "protein_g": "direct", "carbs_g": "direct", "fat_g": "direct",
    "fiber_g": "direct", "sodium_mg": "direct", "sugar_g": "direct", "water_oz": "direct",
    "restaurant_meals": "direct",
    # Logged habits
    "habit_caffeine": "direct", "habit_creatine": "direct", "habit_light": "direct",
    "habit_meditation": "direct", "habit_supplements": "direct",
    "mindful_min": "direct", "meditation_hour": "direct", "nap_min": "direct",
    # Screens and evening routine
    "screen_total": "direct", "screen_social": "direct", "screen_communication": "direct",
    "screen_entertainment": "direct", "screen_evening": "direct", "tv_prebed_min": "direct",
    # Training — a measurement of what you chose to do, so an instruction lands
    "active_calories": "direct", "exercise_minutes": "direct", "daily_load": "direct",
    "tonnage": "direct", "hard_sets": "direct", "steps": "direct", "stand_hours": "direct",
    "walk_distance": "direct",
    "workout_strength": "direct", "workout_hiit": "direct", "workout_walk": "direct",
    "zone2_pct_7d": "direct", "zone3_pct_7d": "direct", "high_intensity_pct_7d": "direct",
    "hard_minutes": "direct",
    # Light exposure — going outside is a choice
    "time_in_daylight": "direct", "first_light_hour": "direct",
    "morning_light_min": "direct", "light_centroid_hour": "direct",
    # Timing
    "bedtime_hour": "direct",
    # Influenced, not set
    "sleep_total_h": "indirect", "sleep_quality_pct": "indirect",
    "bedroom_temp": "indirect", "bedroom_humidity": "indirect",
    "slept_away": "indirect", "partner_present": "indirect", "wfh_day": "indirect",
    # Derived from other inputs you already control, so reachable but not a dial itself
    "energy_balance": "indirect", "acwr": "indirect",
    # Observed, not chosen
    "weather_temp_mean": "none", "weather_aqi_mean": "none", "weather_uv_max": "none",
    "weather_pressure_mean": "none", "weather_pressure_delta": "none",
    # A gait/health READOUT, not a dial — walking faster on command is not a lever
    "walking_speed": "none",
}

# Unclassified inputs default to `indirect` rather than being dropped, so a newly added
# metric shows up (possibly in the wrong section) instead of vanishing silently — the
# failure mode that hid breathing_disturbances and time_in_daylight for months. The
# parity check below turns it into a loud warning at import.
DEFAULT_INPUT_CONTROL = "indirect"


def input_is_actionable(metric_a: str) -> bool:
    """True when the user can change this input at all."""
    return INPUT_CONTROL.get(metric_a, DEFAULT_INPUT_CONTROL) != "none"


# ── Input families: one quantity measured several ways (2026-10-27) ──────────
# Findings shows a family's rows as ONE finding (collapsed, expandable) so five training
# measures against one outcome don't read as five discoveries. Membership is FIXED BY
# MEANING, not re-clustered from the data each week (design call): statistical clustering
# cannot tell "same quantity" from "habits that co-occur" (drinks + late bedtime), and
# those must stay separate levers. The data only CHECKS the families — family_check()
# flags a member that no longer tracks its family and lists ungrouped inputs that move
# together, as candidates, never auto-merged. Sleep duration and bedtime are NOT a family:
# the 2026-10-27 probe separated them (duration carries energy; bedtime carries RHR).
INPUT_FAMILIES: dict[str, dict] = {
    # Was LOAD_FAMILY in Findings.jsx; moved here so backend and UI share one list.
    "training_dose": {
        "label": "Training dose",
        "blurb": "near-identical measures of how much training happened",
        # high_intensity_pct_7d REMOVED 2026-10-28: an intensity MIX, not a dose — the
        # family check measured its median same-day |ρ| to the rest at 0.22 (bar 0.4).
        "members": ["active_calories", "exercise_minutes", "daily_load", "workout_hiit",
                    "hard_sets", "tonnage", "acwr", "hard_minutes"],
    },
    # alcohol_onboard_bed is the drinks' residual at sleep onset; given alcohol_sdu it
    # added nothing for RHR (+0.25 → +0.08) or resp rate (+0.23 → +0.08).
    "alcohol": {
        "label": "Alcohol",
        "blurb": "the same drinks counted two ways (amount, and what is left at bedtime)",
        "members": ["alcohol_sdu", "alcohol_onboard_bed"],
    },
}
FAMILY_OF: dict[str, str] = {m: f for f, spec in INPUT_FAMILIES.items() for m in spec["members"]}

# A member "tracks" its family when its median same-day |ρ| to the other members is at
# least this. Set on the 2026-10-27 data: dose measures sit at 0.65–0.76, ACWR 0.47,
# the alcohol pair 0.48; high_intensity_pct_7d (an intensity MIX, not a dose) at 0.22.
FAMILY_MIN_TRACK = 0.4
# Ungrouped inputs this correlated (same day) are listed as grouping candidates — the
# cut the 2026-10-27 clustering probe used.
FAMILY_CANDIDATE_RHO = 0.6
FAMILY_MIN_OVERLAP = 30


def _same_day_rho(series: dict[date, dict], a: str, b: str) -> tuple[Optional[float], int]:
    pairs = [(row.get(a), row.get(b)) for row in series.values()]
    pairs = [p for p in pairs if p[0] is not None and p[1] is not None]
    if len(pairs) < FAMILY_MIN_OVERLAP:
        return None, len(pairs)
    r = stats.spearmanr(*zip(*pairs))[0]
    return (None if math.isnan(r) else float(r)), len(pairs)


async def family_check(db: AsyncSession, days: int = 365) -> dict:
    """Family membership plus the data check over the last `days` (see INPUT_FAMILIES).

    Candidates are drawn from inputs in currently significant/suggestive pairs only —
    those are the rows a grouping would change.
    """
    end = local_today()
    series, _ = await _fetch_daily_series(db, end - timedelta(days=days), end)

    families = []
    for fid, spec in INPUT_FAMILIES.items():
        members = []
        for m in spec["members"]:
            rhos = [r for o in spec["members"] if o != m
                    for r in [_same_day_rho(series, m, o)[0]] if r is not None]
            med = float(np.median([abs(r) for r in rhos])) if rhos else None
            members.append({
                "key": m, "label": METRIC_LABELS.get(m, m),
                "median_abs_rho": round(med, 2) if med is not None else None,
                "tracks": None if med is None else med >= FAMILY_MIN_TRACK,
            })
        families.append({"id": fid, "label": spec["label"], "blurb": spec["blurb"],
                         "members": members})

    surviving = sorted({r["metric_a"] for r in await read_correlation_cache()
                        if (r["significant"] or r["suggestive"])
                        and not r["metric_a"].startswith("ctrl_")})
    candidates = []
    for i, a in enumerate(surviving):
        for b in surviving[i + 1:]:
            if FAMILY_OF.get(a) is not None and FAMILY_OF.get(a) == FAMILY_OF.get(b):
                continue
            r, n = _same_day_rho(series, a, b)
            if r is not None and abs(r) >= FAMILY_CANDIDATE_RHO:
                candidates.append({"a": a, "b": b, "a_label": METRIC_LABELS.get(a, a),
                                   "b_label": METRIC_LABELS.get(b, b),
                                   "rho": round(r, 2), "n": n})
    candidates.sort(key=lambda c: -abs(c["rho"]))
    return {"days": days, "families": families, "candidates": candidates,
            "thresholds": {"min_track": FAMILY_MIN_TRACK, "candidate_rho": FAMILY_CANDIDATE_RHO}}


# Runs here, not next to _check_registry_parity, because the check now also covers
# INPUT_CONTROL and everything it inspects must already be defined at import time.
for _problem in _check_registry_parity():
    logger.warning("correlations registry parity: %s", _problem)


def direction_phrase(metric_a: str, metric_b: str, r: float) -> str:
    """The relationship IN WORDS — "Less late bedtimes → better Energy". Shared by the
    actionable insights and the briefing's news block, so the direction never rides on
    the sign of rho alone: the news block once rendered "Bedtime (later → higher) →
    Energy (ρ=-0.32)" and the briefing reported the opposite of the finding (note 55).
    Falls back to "higher X → higher/lower Y" for pairs outside the registries."""
    inp, out = INPUTS.get(metric_a), OUTCOMES.get(metric_b)
    if inp is None or out is None:
        a = METRIC_LABELS.get(metric_a, metric_a)
        b = METRIC_LABELS.get(metric_b, metric_b)
        return f"higher {a} → {'higher' if r > 0 else 'lower'} {b}"
    higher_is_better: bool = out["higher_is_better"]
    beneficial = (r > 0 and higher_is_better) or (r < 0 and not higher_is_better)
    if input_is_actionable(metric_a):
        # Directive framing whenever the input can be CHANGED — including when the
        # outcome is physiological. "Less alcohol → lower Respiratory rate (effect
        # next day)" is a perfectly good instruction.
        direction = "More" if beneficial else "Less"
        outcome_dir = "better" if higher_is_better else "lower"
        return f"{direction} {inp['subject']} → {outcome_dir} {out['label']}"
    # Diagnostic framing — describe the relationship, not a directive. Reserved
    # for inputs that cannot be acted on at all (weather, gait readouts).
    return f"{inp['label']} → {'higher' if r > 0 else 'lower'} {out['label']}"


def build_actionable_insights(
    correlation_results: list[dict],
    min_abs_r: float = 0.0,
    min_n: int = 14,
    # Raised from 5 (2026-09-07): with routing on the input rather than the outcome,
    # more rows legitimately qualify, and a silent truncation at 5 was already dropping
    # findings without saying so.
    # None = no truncation. The Findings endpoint passes None (note 36): with the cap,
    # 17 suggestive "less" rows were cut to 12 without a word, so the lists could never
    # add up to the counts beside them.
    max_per_section: Optional[int] = 12,
) -> dict:
    """
    Transform correlation results into actionable do-more/do-less lists plus
    a separate diagnostic signals section.

    Routing:
      - input → subjective outcome  → do_more / do_less (things you can act on)
      - input → physiological outcome → diagnostic (context, not directives)

    Each section has a suggestive variant (0.05 ≤ p_adj < 0.20) shown dimmed.
    Pairs without significant or suggestive flags are excluded entirely.
    """
    do_more: list[dict] = []
    do_less: list[dict] = []
    do_more_suggestive: list[dict] = []
    do_less_suggestive: list[dict] = []
    diagnostic: list[dict] = []
    diagnostic_suggestive: list[dict] = []
    # CONFOUNDED rows (reverse_check, 2026-10-27): kept visible but never a directive —
    # they don't vote on a lever's direction, so they can't create a trade-off either.
    confounded: list[dict] = []
    # WHERE EVERY FINDING WENT (note 36). One entry per significant/suggestive pair,
    # including the ones this function drops, so the UI can reconcile "N survive" with
    # the lists: act | trade_off | cannot_change | below_action_bar | not_input_outcome.
    routes: dict[tuple, dict] = {}

    def _route(a, b, lag, sig, how):
        routes[(a, b, lag)] = {"metric_a": a, "metric_b": b, "lag_days": lag,
                               "tier": "confirmed" if sig else "suggestive", "route": how}

    for corr in correlation_results:
        metric_a = corr["metric_a"]
        metric_b = corr["metric_b"]
        # Spearman ρs is the headline coefficient; Pearson r is available as corr["pearson_r"]
        r = corr.get("spearman_r")
        n = corr.get("n", 0)
        lag_days = corr.get("lag_days", 0)
        significant = corr.get("significant", False)
        suggestive = corr.get("suggestive", False)

        # Same three gates as before, in an order that can NAME the reason; every one
        # still ends in `continue`, so what reaches the lists is unchanged.
        if r is None:
            continue
        if not significant and not suggestive:
            continue
        if metric_a not in INPUTS or metric_b not in OUTCOMES:
            _route(metric_a, metric_b, lag_days, significant, "not_input_outcome")
            continue
        if abs(r) < min_abs_r or n < min_n:
            _route(metric_a, metric_b, lag_days, significant, "below_action_bar")
            continue

        inp = INPUTS[metric_a]
        out = OUTCOMES[metric_b]
        higher_is_better: bool = out["higher_is_better"]

        # beneficial = "more of input" makes outcome better
        beneficial = (r > 0 and higher_is_better) or (r < 0 and not higher_is_better)

        if lag_days == 0:
            lag_str = ""
        elif lag_days == 1:
            lag_str = " (effect next day)"
        else:
            lag_str = f" (effect {lag_days} days later)"

        is_subjective = metric_b in _SUBJECTIVE_OUTCOME_KEYS
        # Still needed below to sort rows into do-more/do-less vs diagnostic. Removing it
        # with the direction_phrase refactor (ec1535f) raised NameError here, and
        # personal_patterns_block swallowed it — the briefings silently lost their
        # findings block for ~a day (found 2026-10-05 by the note-66 test run).
        actionable = input_is_actionable(metric_a)
        sentence = direction_phrase(metric_a, metric_b, r) + lag_str

        item = {
            "input_key":   metric_a,
            "outcome_key": metric_b,
            "input":       inp["label"],
            "outcome":     out["label"],
            "abs_r":       round(abs(r), 2),
            "raw_r":       round(r, 3),
            "n":           n,
            "n_eff":       corr.get("n_eff"),
            "ci_low":      corr.get("ci_low"),
            "ci_high":     corr.get("ci_high"),
            "lag_days":    lag_days,
            "beneficial":  beneficial,
            "sentence":    sentence,
            "control":     INPUT_CONTROL.get(metric_a, DEFAULT_INPUT_CONTROL),
            "felt":        is_subjective,
            # Tier and proxy flag travel with every row (note 30): trade-off rows used to
            # render confirmed and suggestive findings at the same weight.
            "tier":        "confirmed" if significant else "suggestive",
            "proxy_only":  False,
            "reverse_check": corr.get("reverse_check"),
            "confounded":  False,
        }

        if actionable and corr.get("reverse_check") == "confounded":
            item["confounded"] = True
            item["sentence"] = descriptive_phrase(metric_a, metric_b, r, lag_days)
            confounded.append(item)
            continue

        if actionable:
            if significant:
                if beneficial: do_more.append(item)
                else:          do_less.append(item)
            else:
                if beneficial: do_more_suggestive.append(item)
                else:          do_less_suggestive.append(item)
        else:
            if significant:
                diagnostic.append(item)
            else:
                diagnostic_suggestive.append(item)

    # ── AN INPUT THAT POINTS BOTH WAYS IS A TRADE-OFF, NOT A LEVER ────────────
    #
    # Found immediately on lowering the bar to ACTION_THRESHOLD (2026-09-07). Routing on
    # controllability plus a 0.2 cut produced, all at once:
    #     "More sleep → better Sleep quality"        (rho +0.45)
    #     "Less sleep → lower Early-night HR"        (rho +0.26)
    #     "More Zone 2 → better Sleep quality"       (rho +0.34)
    #     "Less active-calorie burn → lower Respiratory rate" (rho +0.36)
    #     "More late bedtimes → lower Early-night HR"(rho -0.20)
    # Every one of those is defensible on its own numbers. Together they instruct the
    # user to sleep less, train less and go to bed later, which is not advice anyone
    # should act on — and "sleep less to lower early-night HR" is very likely an artifact
    # (a longer night is simply more time inside the early-night window).
    #
    # PHYSIOLOGICAL MARKERS TRADE OFF, so a single correlation against one of them does
    # not license a behavioural directive. The old outcome-based routing suppressed these
    # by accident, by refusing to phrase any physiological pair as an instruction. That
    # was the wrong mechanism (it also suppressed alcohol, which is unambiguous) but it
    # was protecting against something real.
    #
    # The rule that separates the two cases without hiding anything: if an input appears
    # with directives in BOTH directions, it is not a lever — move all of its rows to
    # `trade_offs`, described rather than instructed. Alcohol says "less" against every
    # outcome it touches and stays a clean directive; sleep and training do not.
    # WHAT YOU FEEL ARBITRATES. A first pass demoted every conflicted input wholesale
    # and that was too blunt: it threw out "More sleep → better Sleep quality"
    # (the strongest finding in the engine) because one physiological
    # marker disagreed, and that disagreement is very likely an artifact
    # (a longer night is simply more time inside the early-night HR window).
    #
    # Felt outcomes are the terminal values here; physiological markers are instrumental
    # proxies for them. So the direction is set by the FELT rows when any exist, and a
    # physiological row that contradicts them is the thing that moves — not the lever.
    # An input with no felt evidence at all (alcohol) takes its direction from its
    # physiological rows, which is why the alcohol story survives intact.
    felt_dirs: dict[str, set] = {}
    phys_dirs: dict[str, set] = {}
    phys_confirmed: dict[str, bool] = {}
    for lst, direction, is_conf in ((do_more, "more", True), (do_less, "less", True),
                                    (do_more_suggestive, "more", False),
                                    (do_less_suggestive, "less", False)):
        for it in lst:
            bucket = felt_dirs if it["felt"] else phys_dirs
            bucket.setdefault(it["input_key"], set()).add(direction)
            if not it["felt"] and is_conf:
                phys_confirmed[it["input_key"]] = True

    proxy_only: set[str] = set()
    consensus: dict[str, Optional[str]] = {}
    for key in set(felt_dirs) | set(phys_dirs):
        felt = felt_dirs.get(key, set())
        phys = phys_dirs.get(key, set())
        if len(felt) == 1:
            consensus[key] = next(iter(felt))          # what you feel wins
        elif not felt and len(phys) == 1:
            # NO FELT CORROBORATION: the direction rests entirely on proxies, which is
            # exactly where spurious directives come from. Found via note 19 (2026-09-07):
            # organising by outcome surfaced "To improve RESPIRATORY RATE: MORE
            # entertainment screen time" (a suggestive finding) — almost certainly
            # confounded by rest days, and indefensible as an instruction.
            #
            # So a physiological-only input must be CONFIRMED to become a directive.
            # A finding confirmed across three markers clears this; a lone suggestive
            # correlation does not.
            #
            # NOTE 30 (2026-09-12): right to keep such a row off the CONFIRMED list, wrong
            # about where it went. It used to fall through to `None` and land in
            # trade_offs, which labelled a ONE-directional finding as a conflict — live,
            # 4 of the 5 trade-off inputs had no opposing row at all ("Calorie intake →
            # lower HRV" and "→ higher respiratory rate" both say LESS). Every such row is
            # suggestive by construction (a confirmed one would have set phys_confirmed),
            # so it now keeps its direction and stays in the suggestive lists like any
            # other suggestive finding, flagged `proxy_only` so the UI can say "body
            # markers only". The entertainment → respiratory-rate case is still visible,
            # as exactly that, instead of hidden as a fake trade-off.
            consensus[key] = next(iter(phys))
            if not phys_confirmed.get(key):
                proxy_only.add(key)
        else:
            consensus[key] = None                      # felt evidence itself disagrees

    trade_offs: list[dict] = []
    for lst, direction in ((do_more, "more"), (do_less, "less"),
                           (do_more_suggestive, "more"), (do_less_suggestive, "less")):
        for it in [x for x in lst
                   if consensus.get(x["input_key"]) is None
                   or consensus[x["input_key"]] != direction]:
            # The comprehension above is materialised before the loop body runs, so
            # removing from `lst` while iterating it is safe.
            lst.remove(it)
            dir_word = "higher" if it["raw_r"] > 0 else "lower"
            it["sentence"] = (f"{INPUTS[it['input_key']]['label']} → {dir_word} "
                              f"{OUTCOMES[it['outcome_key']]['label']}"
                              + ("" if it["lag_days"] == 0 else
                                 " (effect next day)" if it["lag_days"] == 1 else
                                 f" (effect {it['lag_days']} days later)"))
            it["conflicting"] = True
            trade_offs.append(it)
    trade_offs.sort(key=lambda x: (x["input"], -x["abs_r"]))
    for lst in (do_more_suggestive, do_less_suggestive):
        for it in lst:
            it["proxy_only"] = it["input_key"] in proxy_only

    # Felt outcomes lead, then physiological, each by effect size. Preserves the
    # original intent — what you EXPERIENCE is the headline — without using it as a
    # filter that hides a well-powered finding about a controllable input.
    for lst in (do_more, do_less, do_more_suggestive, do_less_suggestive):
        lst.sort(key=lambda x: (not x["felt"], -x["abs_r"]))
    for lst in (diagnostic, diagnostic_suggestive):
        lst.sort(key=lambda x: -x["abs_r"])
    confounded.sort(key=lambda x: (not x["felt"], -x["abs_r"]))

    # Routes are recorded from the FULL lists, before any truncation below.
    for how, lists in (("act", (do_more, do_less, do_more_suggestive, do_less_suggestive)),
                       ("trade_off", (trade_offs,)),
                       ("cannot_change", (diagnostic, diagnostic_suggestive)),
                       ("confounded", (confounded,))):
        for lst in lists:
            for it in lst:
                _route(it["input_key"], it["outcome_key"], it["lag_days"],
                       it["tier"] == "confirmed", how)

    def cap(lst: list) -> list:
        return lst if max_per_section is None else lst[:max_per_section]

    return {
        "do_more":               cap(do_more),
        "do_less":               cap(do_less),
        "do_more_suggestive":    cap(do_more_suggestive),
        "do_less_suggestive":    cap(do_less_suggestive),
        "diagnostic":            cap(diagnostic),
        "diagnostic_suggestive": cap(diagnostic_suggestive),
        # Controllable inputs whose effects point in opposite directions across
        # outcomes. Real findings; not instructions. Not truncated — the whole point is
        # to see both sides of the trade.
        "trade_offs":            trade_offs,
        # Not truncated: few, and hiding one would hide why a lever lost a row.
        "confounded":            confounded,
        "routes":                list(routes.values()),
    }


# ── Threshold bands ───────────────────────────────────────────────────────────

def _compute_bands(
    metric_a: str,
    metric_b: str,
    lag_days: int,
    xs: list[float],
    ys: list[float],
    min_n: int = 30,
    n_bins: int = 3,
) -> dict:
    """
    Bin xs into n_bins equal-size quantile groups and return the avg ys per bin.
    Returns a dict with sufficient_data=False when len(xs) < min_n.
    """
    n_total = len(xs)
    base = {
        "metric_a":       metric_a,
        "metric_b":       metric_b,
        "metric_a_label": METRIC_LABELS.get(metric_a, metric_a),
        "metric_b_label": METRIC_LABELS.get(metric_b, metric_b),
        "lag_days":       lag_days,
        "n_total":        n_total,
        "min_n_required": min_n,
    }
    if n_total < min_n:
        return {**base, "sufficient_data": False}

    paired = sorted(zip(xs, ys), key=lambda p: p[0])
    bin_size = n_total // n_bins
    labels = ["Low", "Mid", "High"] if n_bins == 3 else [str(i + 1) for i in range(n_bins)]
    bins = []
    for i in range(n_bins):
        s = i * bin_size
        e = n_total if i == n_bins - 1 else (i + 1) * bin_size
        bxs = [p[0] for p in paired[s:e]]
        bys = [p[1] for p in paired[s:e]]
        bins.append({
            "label":       labels[i],
            "lo":          round(min(bxs), 2),
            "hi":          round(max(bxs), 2),
            "avg_outcome": round(sum(bys) / len(bys), 2),
            "n":           len(bxs),
        })
    higher_is_better = OUTCOMES.get(metric_b, {}).get("higher_is_better", True)
    return {**base, "sufficient_data": True, "bins": bins, "higher_is_better": higher_is_better}


async def compute_all_threshold_bands(
    db: AsyncSession,
    pairs: list[tuple[str, str, int]],
    days: int = 180,
    min_n: int = 30,
) -> list[dict]:
    """
    Compute threshold bands for a list of (metric_a, metric_b, lag_days) pairs
    using a single _fetch_daily_series call.
    """
    if not pairs:
        return []
    end = local_today()
    start = end - timedelta(days=days)
    series, _ = await _fetch_daily_series(db, start, end)
    results = []
    for a, b, lag in pairs:
        xs, ys = _build_pairs(series, a, b, lag)
        results.append(_compute_bands(a, b, lag, xs, ys, min_n))
    return results


# ── Readiness-stratified correlations ─────────────────────────────────────────

async def _dates_for_stratum(
    db: AsyncSession, start: date, end: date, stratum: str
) -> set[date]:
    """
    Return dates in [start, end] where readiness falls in the requested stratum.
      "low"  : readiness_score < 50  (Low + Rest Day labels)
      "high" : readiness_score >= 75 (High label)
    """
    if stratum == "low":
        condition = DailyMetric.readiness_score < 50
    elif stratum == "high":
        condition = DailyMetric.readiness_score >= 75
    else:
        raise ValueError(f"Unknown stratum: {stratum!r}. Use 'low' or 'high'.")

    rows = (await db.execute(
        select(DailyMetric.date)
        .where(DailyMetric.date >= start)
        .where(DailyMetric.date <= end)
        .where(condition)
        .where(DailyMetric.readiness_score.isnot(None))
    )).scalars().all()
    return set(rows)


async def compute_stratified_actionable(
    db: AsyncSession,
    stratum: str,
    days: int = 180,
    min_n_stratum: int = 30,
) -> dict:
    """
    Compute actionable insights restricted to days where readiness matches a stratum.
    Returns the same shape as build_actionable_insights, plus stratum metadata.
    When the stratum has fewer than min_n_stratum days, returns sufficient_data=False
    with empty insight lists so the caller can show a friendly "not enough data" message.
    """
    end = local_today()
    start = end - timedelta(days=days)
    series, _ = await _fetch_daily_series(db, start, end)
    n_total = len(series)

    stratum_dates = await _dates_for_stratum(db, start, end, stratum)
    filtered = {d: v for d, v in series.items() if d in stratum_dates}
    n_stratum = len(filtered)

    meta = {
        "stratum":        stratum,
        "n_stratum":      n_stratum,
        "n_total":        n_total,
        "min_n_required": min_n_stratum,
    }

    if n_stratum < min_n_stratum:
        return {
            **meta,
            "sufficient_data":    False,
            "do_more":            [], "do_less":            [],
            "do_more_suggestive": [], "do_less_suggestive": [],
            "diagnostic":         [], "diagnostic_suggestive": [],
        }

    # Rebuild raw per-metric series from the filtered dates
    sorted_dates = sorted(filtered.keys())
    all_keys = list(next(iter(filtered.values())).keys()) if filtered else []
    raw_series = {
        k: [float(filtered[d][k]) for d in sorted_dates if filtered[d].get(k) is not None]
        for k in all_keys
    }
    profiles = _rank_profiles(raw_series)

    results = []
    for metric_a, metric_b, lag_days in CORRELATION_PAIRS:
        xs, ys = _build_pairs(filtered, metric_a, metric_b, lag_days)
        raw_x = raw_series.get(metric_a, [])
        raw_y = raw_series.get(metric_b, [])
        corr_result = _compute_corr(xs, ys, raw_x, raw_y,
                                    profiles.get(metric_a), profiles.get(metric_b))
        if corr_result is not None:
            r_pearson, r_spearman, p_val, n_eff, ci_low, ci_high = corr_result
        else:
            r_pearson = r_spearman = p_val = n_eff = ci_low = ci_high = None
        results.append({
            "metric_a":       metric_a,
            "metric_b":       metric_b,
            "metric_a_label": METRIC_LABELS.get(metric_a, metric_a),
            "metric_b_label": METRIC_LABELS.get(metric_b, metric_b),
            "lag_days":       lag_days,
            "pearson_r":      round(r_pearson, 3) if r_pearson is not None else None,
            "spearman_r":     round(r_spearman, 3) if r_spearman is not None else None,
            "n":              len(xs),
            "n_eff":          n_eff,
            "p_value":        round(p_val, 4) if p_val is not None else None,
            "ci_low":         round(ci_low, 3) if ci_low is not None else None,
            "ci_high":        round(ci_high, 3) if ci_high is not None else None,
            "relationship":   _r_label(r_spearman),
            "days_window":    days,
        })

    _apply_bh(results)

    # Use min_n=14 within strata — we've already required n_stratum ≥ 30,
    # so a per-pair n ≥ 5 minimum is enforced via _pearson_r; 14 surfaces
    # pairs with modest but non-trivial sample sizes.
    actionable = build_actionable_insights(results, min_abs_r=SIGNIFICANCE_THRESHOLD, min_n=14)
    return {**meta, "sufficient_data": True, **actionable}


async def read_correlation_cache() -> list[dict]:
    """
    Read cached correlations from insight_cache, augmented with labels.
    BH correction is re-applied on read so significant/suggestive flags are always current.
    """
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(InsightCache).order_by(InsightCache.metric_a, InsightCache.lag_days)
        )
        rows = result.scalars().all()

    results = [
        {
            "metric_a":       r.metric_a,
            "metric_b":       r.metric_b,
            "metric_a_label": METRIC_LABELS.get(r.metric_a, r.metric_a),
            "metric_b_label": METRIC_LABELS.get(r.metric_b, r.metric_b),
            "lag_days":       r.lag_days,
            "pearson_r":      r.pearson_r,
            "spearman_r":     r.spearman_r,
            "n":              r.n,
            "n_eff":          r.n_eff,
            "p_value":        r.p_value,
            "ci_low":         r.ci_low,
            "ci_high":        r.ci_high,
            "relationship":   _r_label(r.spearman_r),
            "days_window":    r.days_window,
            # Year-over-year drift. `prev_n` is the honest gate: a prev_spearman_r
            # computed from a handful of days is not a comparison, so callers must
            # check n before drawing an arrow.
            "prev_spearman_r": r.prev_spearman_r,
            "prev_n":          r.prev_n,
            **{k: getattr(r, k) for k in CACHE_EXTRA_FIELDS},
            "computed_at":    r.computed_at.isoformat() if r.computed_at else None,
        }
        for r in rows
    ]
    _apply_bh(results)
    for r in results:
        r["reverse_check"] = reverse_check_label(r)
    return results


# ── Prospective (holdout) validation ──────────────────────────────────────────

VALIDATION_MIN_N = 20            # below this the holdout verdict is "accumulating"
VALIDATION_REPLICATE_RHO = 0.15  # half the discovery threshold — holdout ρ is noisier
                                 # and a real effect regressing toward the mean is expected
# One-sided α for the holdout test. One-sided is correct here and not a liberty:
# each ledger row is a single PRE-REGISTERED directional hypothesis, tested on data
# that did not exist when it was registered, so the direction is not chosen after
# seeing the holdout. No BH — there is one test per pre-registration.
VALIDATION_ALPHA = 0.05


async def build_validation(db: AsyncSession) -> dict:
    """
    Prospective validation of ledgered findings + negative-control status.

    For each finding_ledger row, recompute the pair's Spearman ρ using ONLY days
    strictly after the crossing date recorded in the ledger. That data did not
    exist when the finding was selected, so it is untouched by the selection /
    multiple-comparisons process that produced it — this is a genuine holdout.
    Each pair is a single pre-registered directional test on independent data,
    so no BH correction is applied here (and none would be appropriate).

    Verdicts:
      accumulating — holdout n < VALIDATION_MIN_N
      replicating  — same sign, |ρ_holdout| ≥ VALIDATION_REPLICATE_RHO, AND the
                     one-sided holdout test clears VALIDATION_ALPHA
      consistent   — same sign and magnitude, but the test does NOT clear α:
                     the direction held, the evidence is not yet sufficient
      weakening    — |ρ_holdout| < threshold, OR opposite sign without enough
                     evidence to call the reversal
      reversed     — opposite sign, |ρ_holdout| ≥ threshold, AND the one-sided test
                     in that opposite direction clears α

    WHY `consistent` EXISTS. The verdict used to depend only on sign and magnitude,
    so ρ = 0.15 at n = 20 (one-sided p ≈ 0.26 — indistinguishable from noise) was
    labelled `replicating` in exactly the same words as ρ = 0.15 at n = 200. A
    prospective-validation view whose headline verdict ignores its own sample size
    is not validating anything; it is restating the sign. n ≥ 20 is a floor for
    computing a coefficient at all, never evidence that the coefficient means
    something, and the two were being conflated.

    The p-value uses Pyper-Peterman effective N on the holdout series, not raw n.
    Holdout days are consecutive and autocorrelated; using raw n here would make
    this gate optimistic in precisely the way the main engine already corrects for,
    and a validation step that is more permissive than discovery is worse than none.

    Controls: current cache status of the _CONTROL_INPUTS pairs. These are null
    by construction; any control reaching suggestive/confirmed means the
    pipeline's false-positive control is leaking — surfaced as clean=False.
    """
    ledger_rows = (await db.execute(
        select(FindingLedger).order_by(FindingLedger.first_suggestive_at)
    )).scalars().all()

    end = local_today()
    results: list[dict] = []
    if ledger_rows:
        earliest = min(
            (l.first_suggestive_at or l.first_confirmed_at) for l in ledger_rows
        )
        series, _raw = await _fetch_daily_series(db, earliest + timedelta(days=1), end)

        for led in ledger_rows:
            # Validate against the strongest tier the pair has reached; its clock
            # is the *suggestive* crossing (earliest selection event) so the
            # holdout excludes everything the discovery could have touched.
            entry_date = led.first_suggestive_at or led.first_confirmed_at
            tier = "confirmed" if led.first_confirmed_at else "suggestive"
            entry_rho = (led.rho_at_confirmed if led.first_confirmed_at
                         else led.rho_at_suggestive)
            entry_n = (led.n_at_confirmed if led.first_confirmed_at
                       else led.n_at_suggestive)

            sub = {d: v for d, v in series.items() if d > entry_date}
            xs, ys = _build_pairs(sub, led.metric_a, led.metric_b, led.lag_days)
            n_h = len(xs)

            rho_h = None
            if n_h >= 5 and len(set(xs)) > 1 and len(set(ys)) > 1:
                r_val, _p = stats.spearmanr(xs, ys)
                if not math.isnan(r_val):
                    rho_h = float(r_val)

            # Precision of the holdout estimate, in the pre-registered direction.
            # n_eff over raw n because consecutive days are autocorrelated — the
            # aligned pair series stands in for the per-metric series here, which is
            # the same approximation tests/verify_correlations makes.
            p_1s = n_eff_h = ci_lo = ci_hi = None
            if rho_h is not None and entry_rho is not None and n_h >= VALIDATION_MIN_N:
                n_eff_h = _effective_n(n_h, rankdata(xs).tolist(),
                                       rankdata(ys).tolist()) or n_h
                df = n_eff_h - 2
                if df >= 1:
                    denom = max(math.sqrt(max(1.0 - rho_h ** 2, 0.0)), 1e-10)
                    t_stat = rho_h * math.sqrt(df) / denom
                    # Signed so the test is one-sided in the REGISTERED direction:
                    # a holdout that lands the other way can never clear α.
                    p_1s = float(stats.t.sf(t_stat * (1 if entry_rho > 0 else -1), df=df))
                    z = 0.5 * math.log((1.0 + rho_h) / max(1.0 - rho_h, 1e-10))
                    se = math.sqrt((1.0 + rho_h ** 2 / 2.0) / max(n_eff_h - 3, 1))
                    ci_lo = float(math.tanh(z - 1.96 * se))
                    ci_hi = float(math.tanh(z + 1.96 * se))

            if n_h < VALIDATION_MIN_N or rho_h is None or entry_rho is None:
                verdict = "accumulating"
            elif abs(rho_h) < VALIDATION_REPLICATE_RHO:
                verdict = "weakening"
            elif (rho_h > 0) != (entry_rho > 0):
                # A reversal has to clear the SAME evidential bar as a replication,
                # just pointing the other way. Sign-and-magnitude alone put three
                # pairs in red whose 95% CIs all straddled zero — claiming a finding
                # flipped on evidence that cannot even exclude no-effect is the same
                # error as calling rho=0.15 at n=20 a replication, mirrored. When the
                # opposite-direction test does not clear alpha, the honest statement
                # is the weaker one: the registered effect is not showing up.
                verdict = ("reversed" if (p_1s is not None
                                          and (1.0 - p_1s) < VALIDATION_ALPHA)
                           else "weakening")
            elif p_1s is not None and p_1s < VALIDATION_ALPHA:
                verdict = "replicating"
            else:
                # Right direction, respectable size, not enough evidence to call it.
                verdict = "consistent"

            results.append({
                "metric_a":       led.metric_a,
                "metric_b":       led.metric_b,
                "metric_a_label": METRIC_LABELS.get(led.metric_a, led.metric_a),
                "metric_b_label": METRIC_LABELS.get(led.metric_b, led.metric_b),
                "lag_days":       led.lag_days,
                "tier":           tier,
                "entered_at":     entry_date.isoformat(),
                "rho_at_entry":   round(entry_rho, 3) if entry_rho is not None else None,
                "n_at_entry":     entry_n,
                "rho_holdout":    round(rho_h, 3) if rho_h is not None else None,
                "n_holdout":      n_h,
                "n_eff_holdout":  n_eff_h,
                "p_holdout_1s":   round(p_1s, 4) if p_1s is not None else None,
                "ci_holdout":     ([round(ci_lo, 3), round(ci_hi, 3)]
                                   if ci_lo is not None else None),
                # A FLOOR for computing a coefficient, never a sufficiency bar —
                # the verdict is what says whether the evidence is enough.
                "n_needed":       VALIDATION_MIN_N,
                "verdict":        verdict,
            })

    verdict_rank = {"reversed": 0, "weakening": 1, "consistent": 2,
                    "replicating": 3, "accumulating": 4}
    results.sort(key=lambda r: (verdict_rank[r["verdict"]],
                                -abs(r["rho_at_entry"] or 0)))

    # ── Negative-control status (from the current cache) ─────────────────────
    # Severity is tier-aware: a CONFIRMED control (BH p_adj < 0.05) means the
    # FDR machinery is genuinely leaking → alarm. A control in the SUGGESTIVE
    # band is within that tier's designed error rate (p_adj 0.05–0.20 admits up
    # to ~20% false discoveries) → caution: it measures how much to discount
    # suggestive findings, especially input/outcome pairs that both drift.
    cache = await read_correlation_cache()
    controls = []
    n_confirmed_ctrl = 0
    n_suggestive_ctrl = 0
    for ctrl in _CONTROL_INPUTS:
        rows = [c for c in cache if c["metric_a"] == ctrl]
        rhos = [abs(c["spearman_r"]) for c in rows if c.get("spearman_r") is not None]
        flagged = [c for c in rows if c.get("significant") or c.get("suggestive")]
        n_confirmed_ctrl += sum(1 for c in flagged if c.get("significant"))
        n_suggestive_ctrl += sum(1 for c in flagged if not c.get("significant"))
        controls.append({
            "key":         ctrl,
            "label":       METRIC_LABELS.get(ctrl, ctrl),
            "n_pairs":     len(rows),
            "max_abs_rho": round(max(rhos), 3) if rhos else None,
            "flagged": [
                {"outcome": METRIC_LABELS.get(c["metric_b"], c["metric_b"]),
                 "rho": c.get("spearman_r"),
                 "tier": "confirmed" if c.get("significant") else "suggestive"}
                for c in flagged
            ],
        })

    level = ("alarm" if n_confirmed_ctrl
             else "caution" if n_suggestive_ctrl
             else "clean")
    return {
        "results": results,
        "controls": {"level": level, "clean": level == "clean", "inputs": controls},
    }
