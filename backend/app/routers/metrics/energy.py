"""Energy balance / TDEE and the calibrated predicted-vs-actual weight model.

Part of the /api/metrics package (split out of routers/metrics.py 2026-10-21)."""
from datetime import date, timedelta
from statistics import mean
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import BodyWeightLog, FoodLog, HealthMetric, UserSettings
from app.timeutils import local_date_sql, local_day_bounds_utc, to_local_date, local_today
from app.foodday import food_date_sql, food_day_start, food_day_end

router = APIRouter()


@router.get("/energy")
async def get_energy(
    start: date = Query(default_factory=lambda: local_today() - timedelta(days=30)),
    end: date = Query(default_factory=date.today),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """
    Energy balance / TDEE.

    - **Prior:** Mifflin-St Jeor RMR + measured Apple active calories per day.
    - **Adaptive (headline):** weight-trend-calibrated TDEE over the window.

    Requires height / birth_year / biological_sex in settings (returns
    available=False otherwise). Weight is normalised to kg (Apple reports lb).
    """
    from app.services.energy import mifflin_bmr, to_kg, adaptive_tdee, LB_TO_KG

    s = (await db.execute(select(UserSettings).limit(1))).scalar_one_or_none()
    if not s or s.height_cm is None or s.birth_year is None:
        return {
            "available": False,
            "reason": "Set height, birth year and biological sex in Settings to enable energy balance.",
        }

    height_cm = s.height_cm
    sex = s.biological_sex
    age = local_today().year - s.birth_year

    # ── Weight readings → kg, carry-forward. Manual log (kg) preferred, else Apple (lb→kg). ──
    weight_events: list[tuple[date, float]] = []
    for r in (await db.execute(
        select(BodyWeightLog.timestamp, BodyWeightLog.weight_kg)
        .where(BodyWeightLog.timestamp < local_day_bounds_utc(end)[1])
        .order_by(BodyWeightLog.timestamp)
    )).all():
        weight_events.append((to_local_date(r.timestamp), r.weight_kg))
    for r in (await db.execute(
        select(HealthMetric.timestamp, HealthMetric.value, HealthMetric.unit)
        .where(HealthMetric.metric_type == "weight")
        .where(HealthMetric.timestamp < local_day_bounds_utc(end)[1])
        .order_by(HealthMetric.timestamp)
    )).all():
        kg = to_kg(r.value, r.unit)
        if kg is not None:
            weight_events.append((to_local_date(r.timestamp), kg))
    weight_events.sort(key=lambda e: e[0])

    def weight_asof(day: date) -> Optional[float]:
        w = None
        for d0, kg in weight_events:
            if d0 <= day:
                w = kg
            else:
                break
        return w

    # ── Active calories per LOCAL day (TDEE integrates over the window) ──
    ac_day = local_date_sql(HealthMetric.timestamp)
    ac_rows = (await db.execute(
        select(ac_day.label("d"), func.sum(HealthMetric.value).label("v"))
        .where(HealthMetric.metric_type == "active_calories")
        .where(HealthMetric.timestamp >= local_day_bounds_utc(start)[0])
        .where(HealthMetric.timestamp < local_day_bounds_utc(end)[1])
        .group_by(ac_day)
    )).all()
    active_by_day = {date.fromisoformat(str(r.d)): (r.v or 0.0) for r in ac_rows}

    # ── Intake per day (food logged at local noon, so func.date is the local day) ──
    intake_rows = (await db.execute(
        select(food_date_sql(FoodLog.timestamp).label("d"), func.sum(FoodLog.calories).label("v"))
        .where(FoodLog.timestamp >= food_day_start(start))
        .where(FoodLog.timestamp < food_day_end(end))
        .group_by(food_date_sql(FoodLog.timestamp))
    )).all()
    intake_by_day = {date.fromisoformat(str(r.d)): (r.v or 0.0) for r in intake_rows}

    # ── Build per-day series ──
    series = []
    cur = start
    while cur <= end:
        wkg = weight_asof(cur)
        bmr = mifflin_bmr(wkg, height_cm, age, sex) if wkg is not None else None
        active = active_by_day.get(cur)
        intake = intake_by_day.get(cur)
        tdee_prior = (bmr + active) if (bmr is not None and active is not None) else None
        balance = (intake - tdee_prior) if (intake and tdee_prior is not None) else None
        series.append({
            "date": cur,
            "weight_kg": round(wkg, 2) if wkg is not None else None,
            "bmr": round(bmr) if bmr is not None else None,
            "active_calories": round(active) if active is not None else None,
            "intake": round(intake) if intake else None,
            "tdee_prior": round(tdee_prior) if tdee_prior is not None else None,
            "balance": round(balance) if balance is not None else None,
        })
        cur += timedelta(days=1)

    # Partially-logged days would drag mean intake (and so adaptive TDEE) down; leave
    # them out of the fit only — the series above still shows what was logged.
    from app.services.day_flags import nutrition_excluded_dates
    _partial = await nutrition_excluded_dates(db, start, end)
    adaptive = adaptive_tdee(
        [{"date": d["date"], "intake": None if d["date"] in _partial else d["intake"],
          "weight_kg": d["weight_kg"]} for d in series]
    )

    latest_w = weight_events[-1][1] if weight_events else None
    latest_bmr = mifflin_bmr(latest_w, height_cm, age, sex) if latest_w is not None else None
    # Active-calorie component of the prior = trailing average over COMPLETE days
    # (exclude today, which is partial and would understate the prior mid-day).
    today = local_today()
    recent_active = [d["active_calories"] for d in series
                     if d["active_calories"] is not None and d["date"] < today]
    latest_active = round(mean(recent_active[-7:])) if recent_active else None
    tdee_prior_latest = (latest_bmr + latest_active) if (latest_bmr is not None and latest_active is not None) else None

    return {
        "available": True,
        "profile": {"height_cm": height_cm, "age": age, "sex": sex},
        "latest": {
            "weight_kg": round(latest_w, 1) if latest_w is not None else None,
            "weight_lbs": round(latest_w / LB_TO_KG, 1) if latest_w is not None else None,
            "bmr": round(latest_bmr) if latest_bmr is not None else None,
            "active_calories": latest_active,
            "tdee_prior": round(tdee_prior_latest) if tdee_prior_latest is not None else None,
        },
        "adaptive": adaptive,
        "series": [{**d, "date": d["date"].isoformat()} for d in series],
    }


# Weight-prediction look-back (see energy_calibration). 28 d: long enough for the
# energy signal (~150 kcal/day ≈ 1.2 lb) to rise toward the water noise, short enough
# that a constant input bias cannot build a multi-pound drift.
PREDICTION_HORIZON_DAYS = 28


BASE_HALFWIDTH = 10        # ± days of scale readings smoothed into the starting weight


MIN_BASE_READINGS = 4


MIN_KNOWN_FRACTION = 0.7   # share of horizon days with a usable balance


@router.get("/energy/calibration")
async def energy_calibration(
    days: int = 120,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """
    Predicted vs actual weight under the calibrated model, plus the fit history for k.

    FIXED LOOK-BACK, NOT A RUNNING SUM (2026-09-23, notes 47/48). Each day's prediction
    is the smoothed weight PREDICTION_HORIZON_DAYS earlier plus the energy balance
    logged over those days:

        predicted(d) = smoothed_weight(d − H) + Σ balance(d−H+1 .. d) / 7700

    The old curve summed the balance from the first logged day IN THE WINDOW and fitted
    its origin to that window's scale readings. Two consequences, both measured on
    2026-09-23:
      - every window drew the same line at a different height — on shared days the
        90-day and 180-day curves differed by a constant offset of a few lb (note 48);
      - any per-day bias integrated without limit — 1 lb of drift over 90 days needs
        the balance right to 39 kcal/day, ~9x finer than the inputs are.
    With a fixed horizon the answer for a given day does not depend on the window, and
    a constant bias can accumulate for at most H days. Each point asks one question:
    did the last H days of logging explain the last H days of weight change?

    `smoothed_weight` is the OLS-fitted weight at d − H from readings within
    ±BASE_HALFWIDTH days. A symmetric window's fitted centre is unbiased for a locally
    linear trend, so borrowing readings from after d − H does not leak the outcome.

    Partially-logged days (incomplete_data) count as unknown and are imputed at the
    mean of the known days in the horizon — as zero they read as ~700 kcal deficits,
    and a handful of such days made up most of one summer's under-prediction (note 47).
    Sick and travel days stay in: their logs are complete.

    `residual_vs_active` is the model check. k multiplies active calories, which assumes
    the bias is proportional to activity. If a flat under-log is being absorbed instead,
    the residuals will tilt against the horizon's mean active calories and the form
    should become additive.
    """
    from sqlalchemy import select as _select

    from app.models import EnergyCalibration
    from app.services.day_flags import nutrition_excluded_dates
    from app.services.energy import (KCAL_PER_KG, LB_TO_KG, current_activity_factor,
                                     regression_weight_at, resolve_rmr, to_kg)
    from app.timeutils import local_date_sql, local_day_bounds_utc, local_today

    H = PREDICTION_HORIZON_DAYS
    settings = (await db.execute(_select(UserSettings).limit(1))).scalar_one_or_none()
    rmr_info = await resolve_rmr(db, settings)
    k = await current_activity_factor(db)
    if rmr_info["rmr"] is None:
        return {"available": False, "reason": "no RMR (needs lean mass or physical profile)"}

    end = local_today()
    start = end - timedelta(days=days)
    # Data reaches back one horizon plus the smoothing half-width before the window,
    # so the first drawn day is predicted exactly like the last.
    fetch_start = start - timedelta(days=H + BASE_HALFWIDTH)

    lday = local_date_sql(HealthMetric.timestamp)
    act_rows = (await db.execute(
        _select(lday.label("d"), func.sum(HealthMetric.value).label("v"))
        .where(HealthMetric.metric_type == "active_calories")
        .where(HealthMetric.timestamp >= local_day_bounds_utc(fetch_start)[0])
        .where(HealthMetric.timestamp < local_day_bounds_utc(end)[0])
        .group_by(lday)
    )).all()
    active_by = {date.fromisoformat(str(r.d)): (r.v or 0.0) for r in act_rows}

    intake_rows = (await db.execute(
        _select(food_date_sql(FoodLog.timestamp).label("d"), func.sum(FoodLog.calories).label("v"))
        .where(FoodLog.timestamp >= food_day_start(fetch_start))
        .where(FoodLog.timestamp < food_day_end(end))
        .where((FoodLog.skipped == False) | (FoodLog.skipped.is_(None)))  # noqa: E712
        .group_by(food_date_sql(FoodLog.timestamp))
    )).all()
    intake_by = {date.fromisoformat(str(r.d)): (r.v or 0.0) for r in intake_rows}
    partial = await nutrition_excluded_dates(db, fetch_start, end)

    weight_by: dict[date, float] = {}
    for r in (await db.execute(
        _select(HealthMetric.timestamp, HealthMetric.value, HealthMetric.unit)
        .where(HealthMetric.metric_type == "weight")
        .where(HealthMetric.timestamp >= local_day_bounds_utc(fetch_start)[0])
        .order_by(HealthMetric.timestamp)
    )).all():
        kg = to_kg(r.value, r.unit)
        if kg is not None:
            weight_by[to_local_date(r.timestamp)] = kg
    weight_pts = sorted((d.toordinal(), kg) for d, kg in weight_by.items())

    def balance(d: date) -> Optional[float]:
        """Logged intake minus modelled expenditure, or None when unknown."""
        intake, active = intake_by.get(d), active_by.get(d)
        if d in partial or not intake or intake <= 0 or active is None:
            return None
        return intake - (rmr_info["rmr"] + k * active)

    def base_weight(d0: date) -> Optional[float]:
        x0 = d0.toordinal()
        near = [(x, kg) for x, kg in weight_pts if abs(x - x0) <= BASE_HALFWIDTH]
        if len(near) < MIN_BASE_READINGS:
            return None
        return regression_weight_at(near, x0)

    series, resid = [], []
    n_partial_in_window = 0
    cur = start
    while cur < end:
        d0 = cur - timedelta(days=H)
        horizon = [d0 + timedelta(days=i) for i in range(1, H + 1)]
        known = [b for b in (balance(x) for x in horizon) if b is not None]
        base = base_weight(d0)
        predicted_kg = None
        if base is not None and len(known) >= MIN_KNOWN_FRACTION * H:
            predicted_kg = base + (sum(known) / len(known)) * H / KCAL_PER_KG
        # Only days where the exclusion changed something: a flagged day with nothing
        # logged at all was never counted as intake anyway.
        if cur in partial and intake_by.get(cur):
            n_partial_in_window += 1

        actual = weight_by.get(cur)
        if predicted_kg is not None and actual is not None:
            acts = [active_by[x] for x in horizon if x in active_by]
            if acts:
                resid.append({"active": round(sum(acts) / len(acts)),
                              "residual_kg": round(actual - predicted_kg, 3)})
        intake = intake_by.get(cur)
        series.append({
            "date": cur.isoformat(),
            "predicted_kg": round(predicted_kg, 3) if predicted_kg is not None else None,
            "predicted_lbs": round(predicted_kg / LB_TO_KG, 2) if predicted_kg is not None else None,
            "actual_kg": round(actual, 3) if actual is not None else None,
            "actual_lbs": round(actual / LB_TO_KG, 2) if actual is not None else None,
            "intake": round(intake) if intake else None,
            "active": round(active_by[cur]) if cur in active_by else None,
            "partial_log": cur in partial,
        })
        cur += timedelta(days=1)

    hist = (await db.execute(
        _select(EnergyCalibration).order_by(EnergyCalibration.fitted_at.desc()).limit(24)
    )).scalars().all()

    # Gaps are only meaningful where both curves exist on the same day.
    gaps = [p["actual_lbs"] - p["predicted_lbs"] for p in series
            if p["actual_lbs"] is not None and p["predicted_lbs"] is not None]
    final = next((p for p in reversed(series)
                  if p["actual_lbs"] is not None and p["predicted_lbs"] is not None), None)
    return {
        "available": True,
        "k": k,
        "rmr": rmr_info["rmr"],
        "rmr_basis": rmr_info["basis"],
        "window_days": days,
        "horizon_days": H,
        "series": series,
        "residual_vs_active": resid,
        # Latest day's gap: how far the last H days of logging missed the last H days
        # of weight change. Positive = actual above predicted = the model over-estimated
        # the deficit.
        "final_gap_lbs": (round(final["actual_lbs"] - final["predicted_lbs"], 2)
                          if final else None),
        # Mean and spread of the gap over the window. The mean is the bias; the RMS is
        # mostly water noise (~1–1.5 lb) and is the scale a gap must beat to mean much.
        "mean_gap_lbs": round(sum(gaps) / len(gaps), 2) if gaps else None,
        "rms_gap_lbs": round((sum(g * g for g in gaps) / len(gaps)) ** 0.5, 2) if gaps else None,
        "n_compared": len(gaps),
        "n_partial_days": n_partial_in_window,
        "history": [{
            "fitted_at": h.fitted_at.isoformat(), "k": h.k, "k_raw": h.k_raw,
            "k_ci_low": h.k_ci_low, "k_ci_high": h.k_ci_high,
            "rmr": h.rmr, "rmr_basis": h.rmr_basis,
            "adaptive_tdee": h.adaptive_tdee, "n_intake_days": h.n_intake_days,
        } for h in hist],
    }
