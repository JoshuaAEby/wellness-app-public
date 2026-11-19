"""
Energy balance / TDEE estimation.

Two layers, by design:

1. **Prior** — Mifflin-St Jeor RMR + *measured* Apple active calories.
   TDEE_prior(day) = BMR(weight, height, age, sex) + active_calories(day).
   Uses measured movement rather than a population activity factor, so it's
   individualised from day one. Apple's active-calorie estimate still carries
   ±20–30% error and neither term includes the thermic effect of food.

2. **Adaptive (headline)** — energy-balance back-calculation from the observed
   weight-vs-intake relationship over a window. This is the only individually
   *validated* number: if logged intake averaged `I` and body mass changed by
   `Δm` over `n` days, then net balance = Δm·7700 kcal, so
       TDEE_adaptive = mean_intake − (Δm_kg · 7700) / n_days.
   We get Δm from a least-squares fit of weight over time (robust to day noise).
   Self-corrects for individual metabolism and Apple's systematic error, but
   needs enough logged-intake days and weight readings to be trustworthy.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Optional
from app.timeutils import local_today
from app.foodday import food_date_sql, food_day_start, food_day_end

LB_TO_KG = 0.45359237
KCAL_PER_KG = 7700.0  # ≈ energy density of body-mass change (mixed tissue)


def to_kg(value: Optional[float], unit: Optional[str]) -> Optional[float]:
    """Normalise a weight reading to kg. Apple Health may report lb."""
    if value is None:
        return None
    if unit and unit.strip().lower() in ("lb", "lbs", "pound", "pounds"):
        return value * LB_TO_KG
    return value  # already kg (manual log) or assumed kg


def mifflin_bmr(weight_kg: float, height_cm: float, age: int, sex: Optional[str]) -> float:
    """Mifflin-St Jeor resting metabolic rate (kcal/day)."""
    s = 5 if (sex or "").strip().lower().startswith("m") else -161
    return 10.0 * weight_kg + 6.25 * height_cm - 5.0 * age + s


def katch_mcardle_rmr(lean_mass_kg: float) -> float:
    """
    Katch-McArdle RMR from measured lean body mass (kcal/day).

    Preferred over Mifflin when a lean-mass reading exists: it needs no age or sex
    assumption, and it tracks body-composition change automatically rather than
    treating every kg as metabolically identical. For a 65.8 kg lean mass the two
    agree to within ~2 kcal, so switching bases does not introduce a step change.
    """
    return 370.0 + 21.6 * lean_mass_kg


def _ols_slope_per_day(points: list[tuple[int, float]]) -> Optional[float]:
    """Least-squares slope (units of y per unit x) for (x_ordinal_day, weight) points."""
    n = len(points)
    if n < 2:
        return None
    sx = sum(x for x, _ in points)
    sy = sum(y for _, y in points)
    sxx = sum(x * x for x, _ in points)
    sxy = sum(x * y for x, y in points)
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    return (n * sxy - sx * sy) / denom


def _ols_fit(points: list[tuple[int, float]]) -> Optional[tuple[float, float]]:
    """
    OLS fit for (x_ordinal, y) points.
    Returns (slope, slope_variance) where slope_variance = MSE / Σ(x - x̄)².
    Requires ≥ 3 points so that df = n - 2 ≥ 1.
    """
    n = len(points)
    if n < 3:
        return None
    sx = sum(x for x, _ in points)
    sy = sum(y for _, y in points)
    sxx = sum(x * x for x, _ in points)
    sxy = sum(x * y for x, y in points)
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    x_mean = sx / n
    ssx = sum((x - x_mean) ** 2 for x, _ in points)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in points)
    mse = ss_res / (n - 2)
    if ssx <= 0:
        return None
    return (slope, mse / ssx)


MIN_PTS_FOR_REGRESSION_ANCHOR = 3


def regression_weight_at(points: list[tuple[int, float]], x0: int) -> Optional[float]:
    """Least-squares fitted weight at ordinal day `x0`, or None if under-determined.

    WHY THIS EXISTS (2026-09-07). The predicted-vs-actual weight curve used to anchor on
    a SINGLE scale reading — the first one on or after logged intake began. Body mass
    scatters +/-1.5 lb day to day on water, glycogen and gut contents, so whatever noise
    was in that one reading became a PERMANENT offset on every later point of the curve,
    and it also inflated the card's "~X kcal/day" figure, which divides the end gap by
    the number of days.

    Measured on real data: for the 90-day window the single-reading anchor sat
    far enough above the fitted line to make up about half of the apparent gap. Pure artifact, removable for free.

    The fit deliberately spans the whole comparison window rather than a few local
    points: this is a DIAGNOSTIC, not a forecast, so using data from after the origin to
    place the origin is legitimate — the question is whether the two trajectories have
    the same SHAPE, not what the weight will be tomorrow.
    """
    n = len(points)
    if n < MIN_PTS_FOR_REGRESSION_ANCHOR:
        return None
    sx = sum(x for x, _ in points)
    sy = sum(y for _, y in points)
    sxx = sum(x * x for x, _ in points)
    sxy = sum(x * y for x, y in points)
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return slope * x0 + intercept


def adaptive_tdee(series: list[dict]) -> dict:
    """
    Back-calculate TDEE from intake + weight trend over the series.

    `series` items: {"date": date, "intake": float|None, "weight_kg": float|None}
    (intake should be the day's logged calories, or None/0 if nothing logged.)

    Returns the adaptive estimate plus the inputs that produced it and a coarse
    confidence flag so the UI can hedge.

    Also computes a 95% sampling CI on TDEE:
        Var(TDEE) ≈ (7700²)·Var(b) + Var(mean_intake)
        Var(b) = MSE / Σ(t - t̄)²            [OLS slope variance, unbiased MSE]
        Var(mean_intake) = sample_var(intake) / n_intake_days

    IMPORTANT: this CI is the STATISTICAL sampling interval on the weight-trend
    slope and the mean intake estimate. It does NOT capture glycogen/water
    systematic bias (body mass can shift ±1–2 kg in 24 h from fluid). A tight CI
    does not mean the TDEE estimate is unbiased — short windows are especially
    vulnerable. The CI complements, not replaces, the qualitative confidence flag.
    """
    intake_days = [d for d in series if d.get("intake") and d["intake"] > 0]
    weight_pts = [
        (d["date"].toordinal(), d["weight_kg"])
        for d in series
        if d.get("weight_kg") is not None
    ]

    n_intake = len(intake_days)
    n_weight = len(weight_pts)
    span_days = (series[-1]["date"] - series[0]["date"]).days + 1 if series else 0

    # Use _ols_fit (≥3 pts) for slope + variance; fall back to slope-only for ≥2 pts
    fit = _ols_fit(weight_pts)
    if fit is not None:
        slope, slope_var = fit
    elif n_weight >= 2:
        slope = _ols_slope_per_day(weight_pts)
        slope_var = None
    else:
        slope = slope_var = None

    mean_intake = sum(d["intake"] for d in intake_days) / n_intake if n_intake else None

    tdee = None
    if mean_intake is not None and slope is not None:
        tdee = mean_intake - slope * KCAL_PER_KG

    # Confidence: needs a decent number of logged days AND a weight trend over a
    # meaningful span. Body mass is noisy day-to-day (water), so short windows lie.
    if tdee is None:
        confidence = "none"
    elif n_intake >= 14 and n_weight >= 8 and span_days >= 14:
        confidence = "high"
    elif n_intake >= 7 and n_weight >= 4 and span_days >= 10:
        confidence = "medium"
    else:
        confidence = "low"

    # 95% sampling CI — see docstring for what this CI does and doesn't capture
    tdee_ci_low = tdee_ci_high = None
    if tdee is not None and slope_var is not None and n_intake >= 2:
        intake_vals = [d["intake"] for d in intake_days]
        intake_mean_val = sum(intake_vals) / n_intake
        intake_sample_var = sum((v - intake_mean_val) ** 2 for v in intake_vals) / (n_intake - 1)
        var_tdee = (KCAL_PER_KG ** 2) * slope_var + intake_sample_var / n_intake
        se_tdee = math.sqrt(var_tdee)
        tdee_ci_low = round(tdee - 1.96 * se_tdee)
        tdee_ci_high = round(tdee + 1.96 * se_tdee)

    return {
        "tdee": round(tdee) if tdee is not None else None,
        "tdee_ci_low": tdee_ci_low,
        "tdee_ci_high": tdee_ci_high,
        "mean_intake": round(mean_intake) if mean_intake is not None else None,
        "weekly_weight_change_kg": round(slope * 7, 3) if slope is not None else None,
        "n_intake_days": n_intake,
        "n_weight_readings": n_weight,
        "span_days": span_days,
        "confidence": confidence,
    }


# ── DB-backed helpers: adaptive TDEE + calorie-goal resolution ─────────────────

async def latest_adaptive_tdee(db, days: int = 30) -> Optional[dict]:
    """
    Compute the adaptive (weight-calibrated) TDEE over the trailing `days` using a
    lightweight intake+weight series. Returns the adaptive_tdee() dict, or None when
    the physical profile is missing or there's not enough data for a TDEE estimate.
    (Intentionally does NOT duplicate the /energy router's full prior+series payload.)
    """
    from datetime import timedelta

    from sqlalchemy import func, select

    from app.models import BodyWeightLog, FoodLog, HealthMetric, UserSettings

    s = (await db.execute(select(UserSettings).limit(1))).scalar_one_or_none()
    if not s or s.height_cm is None or s.birth_year is None:
        return None

    end = local_today()
    start = end - timedelta(days=days)

    from app.timeutils import local_day_bounds_utc, to_local_date

    end_bound = local_day_bounds_utc(end)[1]
    weight_events: list[tuple[date, float]] = []
    for r in (await db.execute(
        select(HealthMetric.timestamp, HealthMetric.value, HealthMetric.unit)
        .where(HealthMetric.metric_type == "weight")
        .where(HealthMetric.timestamp < end_bound)
        .order_by(HealthMetric.timestamp)
    )).all():
        kg = to_kg(r.value, r.unit)
        if kg is not None:
            weight_events.append((to_local_date(r.timestamp), kg))
    for r in (await db.execute(
        select(BodyWeightLog.timestamp, BodyWeightLog.weight_kg)
        .where(BodyWeightLog.timestamp < end_bound)
        .order_by(BodyWeightLog.timestamp)
    )).all():
        weight_events.append((to_local_date(r.timestamp), r.weight_kg))
    weight_events.sort(key=lambda e: e[0])

    def w_asof(day: date) -> Optional[float]:
        w = None
        for d0, kg in weight_events:
            if d0 <= day:
                w = kg
            else:
                break
        return w

    intake_rows = (await db.execute(
        select(food_date_sql(FoodLog.timestamp).label("d"), func.sum(FoodLog.calories).label("v"))
        .where(FoodLog.timestamp >= food_day_start(start))
        .where(FoodLog.timestamp < food_day_end(end))
        .group_by(food_date_sql(FoodLog.timestamp))
    )).all()
    intake_by = {date.fromisoformat(str(r.d)): (r.v or 0.0) for r in intake_rows}

    # A partially-logged day (incomplete_data flag) reads as a low-intake day and drags
    # mean_intake — and so TDEE and k — down. Dropping it from the mean is the same as
    # imputing the mean of the complete days. Sick/travel days are NOT dropped: their
    # logs are complete, and what was eaten on them is real intake.
    from app.services.day_flags import nutrition_excluded_dates
    for d in await nutrition_excluded_dates(db, start, end):
        intake_by.pop(d, None)

    series, cur = [], start
    while cur <= end:
        series.append({"date": cur, "intake": intake_by.get(cur), "weight_kg": w_asof(cur)})
        cur += timedelta(days=1)

    a = adaptive_tdee(series)
    return a if a and a.get("tdee") is not None else None


# ── Personal activity calibration (k) ─────────────────────────────────────────
#
# Apple's active-calorie figure carries a systematic error (wrist accelerometry
# over-attributes incidental movement). Rather than trust it, fit one personal
# scaling factor against the only individually-validated quantity available — the
# observed weight trajectory:
#
#     TDEE(d) = RMR + k · active_apple(d)
#     k = (TDEE_adaptive − RMR) / mean_active
#
# WHAT k IS NOT: a measurement of Apple's error. Because it is fit against *logged*
# intake, any systematic under-logging is folded into the same number — both biases
# push the same direction and are not separable from this data. That is fine for
# prediction (the model is calibrated to how intake is actually logged) but k must
# not be reported as a physiological quantity.
#
# One parameter only. Fitting an additive term as well would need the day-to-day
# weight signal to be informative, and it is not: daily water noise (±1.5 lb) swamps
# a daily energy imbalance of ±300 kcal (±0.086 lb) by more than an order of
# magnitude. Whether the multiplicative form is right is testable — plot the
# prediction residual against daily active calories; a tilt means the bias is
# additive and the form should change.

CALIBRATION_WINDOW_DAYS = 90
MIN_DAYS_FOR_CALIBRATION = 50
K_BOUNDS = (0.4, 1.6)


async def fit_activity_factor(db, window_days: int = CALIBRATION_WINDOW_DAYS) -> Optional[dict]:
    """
    Fit k over the trailing window. Returns None when there is not enough logged
    intake to be worth doing — the caller then holds k at 1.0 and flags it
    uncalibrated, rather than showing a confidently wrong number.
    """
    from datetime import timedelta

    from sqlalchemy import func, select

    from app.models import HealthMetric, UserSettings
    from app.timeutils import local_date_sql, local_day_bounds_utc, local_today

    s = (await db.execute(select(UserSettings).limit(1))).scalar_one_or_none()
    a = await latest_adaptive_tdee(db, days=window_days)
    if not a or a.get("tdee") is None:
        return None
    if (a.get("n_intake_days") or 0) < MIN_DAYS_FOR_CALIBRATION:
        return None

    rmr_info = await resolve_rmr(db, s)
    if rmr_info["rmr"] is None:
        return None

    today = local_today()
    start = local_day_bounds_utc(today - timedelta(days=window_days))[0]
    end = local_day_bounds_utc(today)[0]  # exclude the partial current day
    lday = local_date_sql(HealthMetric.timestamp)
    rows = (await db.execute(
        select(lday.label("d"), func.sum(HealthMetric.value).label("v"))
        .where(HealthMetric.metric_type == "active_calories")
        .where(HealthMetric.timestamp >= start).where(HealthMetric.timestamp < end)
        .group_by(lday)
    )).all()
    actives = [r.v or 0.0 for r in rows]
    if not actives:
        return None
    mean_active = sum(actives) / len(actives)
    if mean_active <= 0:
        return None

    tdee, rmr = a["tdee"], rmr_info["rmr"]
    k_raw = (tdee - rmr) / mean_active
    k = min(max(k_raw, K_BOUNDS[0]), K_BOUNDS[1])

    # Propagate the TDEE confidence interval through to k (RMR and mean_active are
    # treated as fixed; their uncertainty is small next to the weight-slope term).
    k_lo = k_hi = None
    if a.get("tdee_ci_low") is not None and a.get("tdee_ci_high") is not None:
        k_lo = round((a["tdee_ci_low"] - rmr) / mean_active, 3)
        k_hi = round((a["tdee_ci_high"] - rmr) / mean_active, 3)

    return {
        "k": round(k, 3),
        "k_raw": round(k_raw, 3),
        "k_ci_low": k_lo,
        "k_ci_high": k_hi,
        "clamped": abs(k - k_raw) > 1e-9,
        "rmr": rmr,
        "rmr_basis": rmr_info["basis"],
        "mean_active": round(mean_active),
        "adaptive_tdee": tdee,
        "n_intake_days": a.get("n_intake_days"),
        "window_days": window_days,
    }


async def current_activity_factor(db) -> float:
    """
    The stored k, or 1.0 when uncalibrated. 1.0 reproduces the pre-calibration
    behaviour exactly, so an uncalibrated install is unchanged rather than broken.
    """
    from sqlalchemy import select

    from app.models import EnergyCalibration

    row = (await db.execute(
        select(EnergyCalibration).order_by(EnergyCalibration.fitted_at.desc()).limit(1)
    )).scalars().first()
    return row.k if row and row.k else 1.0


REFIT_EVERY_DAYS = 7


async def maybe_refit_activity_factor(db, force: bool = False) -> Optional[dict]:
    """
    Refit k weekly, writing a new row only when it actually moved.

    Weekly sounds aggressive but consecutive 90-day windows share 83 of 90 days, so
    successive fits are strongly autocorrelated and k moves smoothly — it cannot chase
    noise. Refitting *monthly* would be worse: the same precision, delivered as visible
    step changes.

    Hysteresis: skip the write when the change is inside the sampling noise of the
    CHANGE — which is much smaller than the sampling noise of either fit.

    This originally compared |Δk| against the marginal SE of a single fit, and that
    is the wrong yardstick by roughly 2.5x. Two fits sharing 83 of 90 days are not
    independent draws: Var(k₂ − k₁) = 2σ²(1 − ρ), and with the estimates correlated
    about as strongly as their windows overlap, SE_Δ ≈ σ·√(2·d/W) for d days elapsed
    on a W-day window. At the weekly cadence that is σ·√(2·7/90) ≈ 0.39σ.

    Measured 2026-08-25 the marginal SE was 0.070 — 8.4% of k — so the old gate
    demanded an 8.4% jump before recording anything. The two stored rows are 0.765 →
    0.836, a 9.3% move that only just cleared it. That is exactly the "visible step
    changes" this docstring says weekly refitting exists to avoid: the gate was
    suppressing the smooth drift it was supposed to record and admitting only jumps.

    Using elapsed days rather than REFIT_EVERY_DAYS matters when the job has not run
    for a while: a month's gap shares only 60 of 90 days, the estimates are genuinely
    more independent, and the threshold widens on its own.
    """
    from datetime import datetime, timedelta

    from sqlalchemy import select

    from app.models import EnergyCalibration

    last = (await db.execute(
        select(EnergyCalibration).order_by(EnergyCalibration.fitted_at.desc()).limit(1)
    )).scalars().first()

    if not force and last is not None:
        if datetime.utcnow() - last.fitted_at < timedelta(days=REFIT_EVERY_DAYS):
            return None

    fit = await fit_activity_factor(db)
    if fit is None:
        return None

    if not force and last is not None:
        se = None
        if fit["k_ci_low"] is not None and fit["k_ci_high"] is not None:
            se = (fit["k_ci_high"] - fit["k_ci_low"]) / (2 * 1.96)
        if se:
            window = fit.get("window_days") or CALIBRATION_WINDOW_DAYS
            elapsed = max((datetime.utcnow() - last.fitted_at).days, 1)
            # Fraction of the window that is NOT shared with the previous fit.
            # Capped at 1: past a full window the two are independent and the
            # marginal SE is the right yardstick again.
            fresh = min(elapsed / window, 1.0)
            se_delta = se * math.sqrt(2.0 * fresh)
            if abs(fit["k"] - last.k) < se_delta:
                return None

    row = EnergyCalibration(
        k=fit["k"], k_raw=fit["k_raw"],
        k_ci_low=fit["k_ci_low"], k_ci_high=fit["k_ci_high"],
        rmr=fit["rmr"], rmr_basis=fit["rmr_basis"],
        mean_active=fit["mean_active"], adaptive_tdee=fit["adaptive_tdee"],
        n_intake_days=fit["n_intake_days"], window_days=fit["window_days"],
    )
    db.add(row)
    await db.commit()
    return fit


KCAL_PER_LB_PER_WEEK = 500.0  # 1 lb/week ≈ 3500 kcal / 7 days


def pace_daily_offset(lbs_per_week: Optional[float]) -> float:
    """Convert weekly weight-change target (lbs) to daily kcal offset (negative = deficit)."""
    if lbs_per_week is None:
        return 0.0
    return lbs_per_week * KCAL_PER_LB_PER_WEEK


async def resolve_calorie_goal(db, settings) -> dict:
    """
    Resolve the calorie goal mode.

    Returns:
      • fixed   → {mode, value=kcal, dynamic=False, daily_offset=0}
      • balance → {mode, value=None, dynamic=True, daily_offset=0}
                  Goal = daily Out (RMR + active_cals). Targets net zero.
      • pace    → {mode, value=None, dynamic=True, daily_offset=N}
                  Goal = Out + daily_offset; offset from weight_change_goal_lbs_per_week.
      • maintain → legacy alias for balance (migrated transparently)

    For balance/pace, `value` is None because the effective goal changes each day
    with active calories. Callers that need a per-day goal must compute:
        goal_kcal = rmr + active_cals_for_day + daily_offset
    """
    raw_mode = (getattr(settings, "calorie_goal_mode", None) or "fixed") if settings else "fixed"
    mode = "balance" if raw_mode == "maintain" else raw_mode

    if mode in ("balance", "pace"):
        offset = pace_daily_offset(getattr(settings, "weight_change_goal_lbs_per_week", None))
        return {
            "mode": mode, "value": None, "dynamic": True,
            "daily_offset": round(offset), "tdee": None, "confidence": None,
        }
    return {
        "mode": "fixed",
        "value": settings.calorie_goal if settings else None,
        "dynamic": False, "daily_offset": 0, "tdee": None, "confidence": None,
    }


# ── Resolved daily energy: one source of truth for "Out" and the calorie goal ──
#
# Everything that needs a per-day calorie goal must go through effective_calorie_goal().
# Before this existed each consumer rolled its own: Today.jsx computed Mifflin client-side
# for today but switched to Apple's resting for past dates (a ~380 kcal step at the
# one-day boundary), while nudges.py read the raw stored calorie_goal and ignored the
# mode entirely. Those two disagreed with each other and with the calorie ring.

async def latest_lean_mass_kg(db) -> Optional[float]:
    """Most recent measured lean body mass in kg, or None."""
    from sqlalchemy import select

    from app.models import HealthMetric

    r = (await db.execute(
        select(HealthMetric.value, HealthMetric.unit)
        .where(HealthMetric.metric_type == "lean_body_mass")
        .order_by(HealthMetric.timestamp.desc()).limit(1)
    )).first()
    return to_kg(r.value, r.unit) if r else None


async def resolve_rmr(db, settings) -> dict:
    """
    Resting metabolic rate, preferring Katch-McArdle off measured lean mass and
    falling back to Mifflin-St Jeor.

    Deliberately does NOT use Apple's `resting_calories`. Apple does not measure basal
    rate — no wearable does — it models it from the same anthropometrics Mifflin uses,
    through a regression that reads ~380 kcal/day high here. Treating it as "measured"
    was the bug this replaces.
    """
    from datetime import date as _date

    from sqlalchemy import select

    from app.models import HealthMetric

    lean_kg = await latest_lean_mass_kg(db)
    if lean_kg:
        return {"rmr": round(katch_mcardle_rmr(lean_kg), 1), "basis": "katch_mcardle",
                "lean_mass_kg": round(lean_kg, 2)}

    if not settings or settings.height_cm is None or settings.birth_year is None:
        return {"rmr": None, "basis": None, "lean_mass_kg": None}

    r = (await db.execute(
        select(HealthMetric.value, HealthMetric.unit)
        .where(HealthMetric.metric_type == "weight")
        .order_by(HealthMetric.timestamp.desc()).limit(1)
    )).first()
    wkg = to_kg(r.value, r.unit) if r else None
    if wkg is None:
        return {"rmr": None, "basis": None, "lean_mass_kg": None}

    age = _date.today().year - settings.birth_year
    return {
        "rmr": round(mifflin_bmr(wkg, settings.height_cm, age,
                                 getattr(settings, "biological_sex", None)), 1),
        "basis": "mifflin", "lean_mass_kg": None,
    }


async def prefetch_active_totals(db, shared: dict, start: date, end: date) -> None:
    """Fill `shared` with each local day's active-calorie total for [start, end], in ONE
    grouped query, for projected_active() to read instead of one query per day.

    Same bucketing as projected_active's own per-day query by construction: both use
    the local-day mapping with literal 19-char bounds (on_local_days / local_date_sql),
    so a sample sitting exactly on local midnight lands on the same day either way.
    Days with no samples read 0.0, as the per-day query's `or 0.0` does. Built
    2026-10-21 for /streaks, which resolved ~175 goals one query each.
    """
    from sqlalchemy import func, select

    from app.models import HealthMetric
    from app.timeutils import local_date_sql, on_local_days

    lday = local_date_sql(HealthMetric.timestamp)
    rows = (await db.execute(
        select(lday, func.sum(HealthMetric.value))
        .where(HealthMetric.metric_type == "active_calories")
        .where(on_local_days(HealthMetric.timestamp, start, end))
        .group_by(lday)
    )).all()
    shared["active_by_day"] = {date.fromisoformat(d): (v or 0.0) for d, v in rows}
    shared["active_range"] = (start, end)


async def projected_active(db, day: date, hour: Optional[int] = None,
                           lookback_days: int = 28, shared: Optional[dict] = None) -> dict:
    """
    Active calories for `day`: accrued so far, plus an estimate of the rest of the day.

    A dynamic goal built on active-so-far inflates all day — useless for deciding what
    to eat at noon, and it perversely rewards a late workout. So for a *goal* we project
    the full day: what has accrued, plus the mean amount that has historically arrived
    after this hour over the trailing `lookback_days`. That handles evening training.
    As the day closes the remainder term decays to zero and the projection converges on
    the actual, so there is no midnight discontinuity.

    `hour=None` means the day is complete — projection equals the measured total.
    """
    from datetime import timedelta

    from sqlalchemy import Integer, case, func, select

    from app.models import HealthMetric
    from app.timeutils import local_date_sql, on_local_days

    rng = (shared or {}).get("active_range")
    if rng and rng[0] <= day <= rng[1]:
        so_far = shared["active_by_day"].get(day, 0.0)      # prefetch_active_totals
    else:
        # Literal bounds (on_local_days), not bound datetimes: a bound datetime carries
        # microseconds and pushed a sample sitting exactly on local midnight into the
        # previous day (2026-10-21).
        so_far = (await db.execute(
            select(func.sum(HealthMetric.value))
            .where(HealthMetric.metric_type == "active_calories")
            .where(on_local_days(HealthMetric.timestamp, day))
        )).scalar() or 0.0

    if hour is None:
        return {"so_far": round(so_far), "expected_remaining": 0,
                "projected": round(so_far), "basis": "complete"}

    # Trailing days: total, and the portion that had accrued by this hour.
    lday = local_date_sql(HealthMetric.timestamp)
    lhour = func.cast(func.strftime("%H", HealthMetric.timestamp, "localtime"), Integer)
    rows = (await db.execute(
        select(
            lday.label("d"),
            func.sum(HealthMetric.value).label("total"),
            func.sum(case((lhour < hour, HealthMetric.value), else_=0.0)).label("upto"),
        )
        .where(HealthMetric.metric_type == "active_calories")
        # Literal bounds: with bound datetimes a sample exactly at today's local
        # midnight passed `< d0` and formed a spurious one-sample "today" group.
        .where(on_local_days(HealthMetric.timestamp, day - timedelta(days=lookback_days),
                             day - timedelta(days=1)))
        .group_by(lday)
    )).all()

    remainders = [max((r.total or 0.0) - (r.upto or 0.0), 0.0) for r in rows]
    expected = (sum(remainders) / len(remainders)) if remainders else 0.0
    return {
        "so_far": round(so_far),
        "expected_remaining": round(expected),
        "projected": round(so_far + expected),
        "basis": f"{len(remainders)}d_lookback",
    }


# NOTE: a batched calorie_goals_for_range() was written and REMOVED (2026-08-22).
# It resolved RMR/k once and bucketed active calories in Python, on the assumption
# that 84 per-day calls would be slow. Measured, the opposite held: the per-day loop
# ran 0.16 s cold for 90 days while the batch took 0.42 s warm — 90 indexed aggregate
# queries beat pulling thousands of raw rows into Python. It also disagreed with
# effective_calorie_goal by up to 3 kcal on some dates. Two functions answering
# "what is the goal" must never disagree, and there was no speed to buy the risk.
# Call effective_calorie_goal() per day; it is the single source of truth.

async def effective_calorie_goal(db, settings, day: date,
                                 hour: Optional[int] = None,
                                 shared: Optional[dict] = None) -> dict:
    """
    The calorie goal actually in force for `day`.

    fixed          → the stored number.
    balance / pace → RMR + k · projected_active + daily_offset, where k is the fitted
                     personal activity factor (see fit_activity_factor). k defaults to
                     1.0 until a calibration exists, which reproduces the previous
                     behaviour exactly rather than silently changing targets.

    `shared`: optional dict reused across calls in ONE request (e.g. streaks over ~200
    days). The mode, RMR and k don't depend on `day` — each reads the LATEST stored
    value — so computing them once per request returns byte-identical goals while
    cutting the per-day cost to the one projected_active query (2026-10-21: streaks
    went 540 queries / 1.2 s). This is NOT the batched variant removed on 2026-08-22:
    every goal still comes from this function.
    """
    if shared is not None and "resolved" in shared:
        resolved = shared["resolved"]
    else:
        resolved = await resolve_calorie_goal(db, settings)
        if shared is not None:
            shared["resolved"] = resolved
    if resolved["mode"] == "fixed":
        return {**resolved, "goal": resolved["value"], "rmr": None,
                "active": None, "k": None}

    if shared is not None and "rmr_info" in shared:
        rmr_info, k = shared["rmr_info"], shared["k"]
    else:
        rmr_info = await resolve_rmr(db, settings)
        k = await current_activity_factor(db)
        if shared is not None:
            shared["rmr_info"], shared["k"] = rmr_info, k
    act = await projected_active(db, day, hour, shared=shared)
    rmr = rmr_info["rmr"]
    goal = None
    if rmr is not None:
        goal = round(rmr + k * act["projected"] + resolved["daily_offset"])
    return {
        **resolved,
        "goal": goal,
        "rmr": rmr,
        "rmr_basis": rmr_info["basis"],
        "active": act,
        "k": k,
    }
