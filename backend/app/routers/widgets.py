"""
Widget summary endpoint.

Authentication: static WIDGET_TOKEN from .env (Bearer token).
Intended for Widgy / home-screen widgets that poll on a timer and
can't manage JWT expiry in their config.
"""
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.acwr import acwr_band
from app.auth import bearer_matches
from app.config import settings
from app.database import get_db
# `local_date_sql` is used below but was NEVER imported — /api/widgets/summary has
# raised NameError (HTTP 500) on every call since at least the repo's initial commit
# (2026-07-17). Found 2026-09-07 while routing the protein target through the shared
# derivation: the endpoint 500s before it can return anything. The iOS widget has been
# dead this whole time, silently — nothing monitors this route.
from app.timeutils import local_date_sql, local_today, local_now
from app.foodday import food_date_sql, food_today
from app.models import (
    BodyWeightLog,
    DailyMetric,
    FoodLog,
    HealthMetric,
    SleepSession,
    UserSettings,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["widgets"])

# ── colour constants ──────────────────────────────────────────────────────────
GREEN = "#34d399"
AMBER = "#f59e0b"
RED   = "#ef4444"
GREY  = "#94a3b8"   # "no data" — never a verdict colour


def metric_color(value: float, thresholds: list[float], colors: list[str]) -> str:
    """
    Return the appropriate colour string for *value* given ascending *thresholds*.

    thresholds : [t1, t2]  — upper bounds (strict <)
    colors     : [c0, c1, c2]  — colour when value < t1 / t1 <= value < t2 / value >= t2
    """
    for threshold, color in zip(thresholds, colors):
        if value < threshold:
            return color
    return colors[-1]


def _readiness_color(score: float) -> str:
    # green >= 70, amber >= 40, red < 40
    return metric_color(score, [40.0, 70.0], [RED, AMBER, GREEN])


def _sleep_color(sleep_hours: float, target_hours: float) -> str:
    shortfall = max(0.0, target_hours - sleep_hours)
    # green: on target (shortfall < ~0), amber: within 1 h, red: > 1 h short
    return metric_color(shortfall, [0.001, 1.001], [GREEN, AMBER, RED])


def _hrv_color(z: float) -> str:
    # higher z = better; green > 0, amber (-1, 0], red <= -1
    return metric_color(z, [-1.0, 0.0], [RED, AMBER, GREEN])


def _hrv_label(z: float) -> str:
    if z > 0:
        return "Above baseline"
    if z > -1.0:
        return "Below baseline"
    return "Well below baseline"


def _rhr_color(z: float) -> str:
    # inverted — lower z = better (RHR below baseline is good)
    # green z < 0, amber 0–1, red > 1
    return metric_color(z, [0.0, 1.001], [GREEN, AMBER, RED])


def _rhr_label(z: float) -> str:
    if z < 0:
        return "Below baseline"
    if z < 1.0:
        return "Above baseline"
    return "Well above baseline"


def _acwr_color_label(acwr: float) -> tuple[str, str]:
    # One rule app-wide (app/acwr.py, 2026-11-04): a light week is fine, Rho eases off above 2.0.
    return {"light": (GREEN, "Light load"), "normal": (GREEN, "Normal load"),
            "high": (AMBER, "High load"), "very_high": (RED, "Very high load")}[acwr_band(acwr)]


def _sleep_debt_color_label(debt: float) -> tuple[str, str]:
    # green < 0.5 h, amber 0.5–1.5 h, red > 1.5 h
    color = metric_color(debt, [0.5, 1.501], [GREEN, AMBER, RED])
    if debt < 0.5:
        label = "On track"
    elif debt < 1.5:
        label = "Slight deficit"
    else:
        label = "Significant deficit"
    return color, label


def _protein_color(protein_g: float, target_g: float) -> str:
    if target_g <= 0:
        return AMBER
    pct = protein_g / target_g
    # green >= target, amber >= 50 %, red < 50 %
    return metric_color(pct, [0.5, 1.0], [RED, AMBER, GREEN])


# ── auth dependency ───────────────────────────────────────────────────────────

def _verify_widget_token(authorization: str = Header(default="")) -> None:
    if not settings.widget_token or settings.widget_token == "changeme":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WIDGET_TOKEN not configured on server",
        )
    if not bearer_matches(authorization, settings.widget_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid widget token",
        )


# ── endpoint ──────────────────────────────────────────────────────────────────

@router.get("/api/widgets/summary")
async def widget_summary(
    _: None = Depends(_verify_widget_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Aggregated wellness snapshot for home-screen widgets.
    Authenticated with a static WIDGET_TOKEN (Bearer) rather than JWT.
    Falls back to most-recent daily_metrics row if today's hasn't been
    computed yet; signals this via ``data_freshness = "stale"``.
    """
    _tz = ZoneInfo(settings.tz)
    today: date = local_today()
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

    # ── user settings ─────────────────────────────────────────────────────────
    settings_result = await db.execute(select(UserSettings).limit(1))
    user_settings = settings_result.scalar_one_or_none()

    target_sleep = (user_settings.target_sleep_hours or 8.0) if user_settings else 8.0
    # Was resolve_calorie_goal()["value"] or 500. In balance/pace mode "value" is None
    # by design (the goal moves daily with activity), so the fallback silently scored
    # every day against a 500 kcal target — not a missing number but a wrong one, which
    # is worse. effective_calorie_goal computes the figure actually in force today.
    from app.services.energy import effective_calorie_goal
    calorie_goal = None
    if user_settings:
        calorie_goal = (await effective_calorie_goal(db, user_settings, today))["goal"]
    steps_goal   = (user_settings.steps_goal   or 10000) if user_settings else 10000

    # ── daily_metrics — today, fallback to most recent ────────────────────────
    dm_result = await db.execute(
        select(DailyMetric).where(DailyMetric.date == today)
    )
    dm = dm_result.scalar_one_or_none()
    data_freshness = "today"

    if dm is None:
        dm_result = await db.execute(
            select(DailyMetric).order_by(DailyMetric.date.desc()).limit(1)
        )
        dm = dm_result.scalar_one_or_none()
        data_freshness = "stale"

    # ── sleep — today first, then yesterday, then most recent ─────────────────
    sleep_result = await db.execute(
        select(SleepSession).where(SleepSession.date == today)
    )
    sleep = sleep_result.scalar_one_or_none()

    if sleep is None:
        yesterday = today - timedelta(days=1)
        sleep_result = await db.execute(
            select(SleepSession).where(SleepSession.date == yesterday)
        )
        sleep = sleep_result.scalar_one_or_none()

    if sleep is None:
        sleep_result = await db.execute(
            select(SleepSession).order_by(SleepSession.date.desc()).limit(1)
        )
        sleep = sleep_result.scalar_one_or_none()

    sleep_hours    = round((sleep.total_min or 0) / 60, 2) if sleep else 0.0
    sleep_deep_min = sleep.deep_min or 0 if sleep else 0
    sleep_rem_min  = sleep.rem_min  or 0 if sleep else 0

    # ── food log — today's totals ─────────────────────────────────────────────
    food_result = await db.execute(
        select(
            func.coalesce(func.sum(FoodLog.protein_g), 0).label("protein"),
            func.coalesce(func.sum(FoodLog.calories),  0).label("calories"),
        ).where(food_date_sql(FoodLog.timestamp) == food_today().isoformat())
    )
    food_row = food_result.one()
    protein_g = round(food_row.protein or 0)

    # ── protein target ────────────────────────────────────────────────────────
    # ONE derivation, shared — see services/nutrition_goals.
    #
    # This copy was also wrong, not merely duplicated: it consulted only
    # `BodyWeightLog`, which is empty for this user (every weight arrives from Apple
    # into `health_metrics`), so it always fell through to the hardcoded 147 g while
    # every other surface said 167. Moot until now only because the endpoint itself
    # 500s — see the local_date_sql note at the top of the file.
    from app.services.nutrition_goals import latest_weight_lbs, resolve_protein_goal
    protein_target_g = await resolve_protein_goal(db, user_settings) or 147

    # ── steps & active calories — today ───────────────────────────────────────
    hm_result = await db.execute(
        select(
            HealthMetric.metric_type,
            func.coalesce(func.sum(HealthMetric.value), 0).label("total"),
        )
        .where(
            HealthMetric.metric_type.in_(["steps", "active_calories"]),
            local_date_sql(HealthMetric.timestamp) == today.isoformat(),
        )
        .group_by(HealthMetric.metric_type)
    )
    hm_rows = {row.metric_type: row.total for row in hm_result}
    steps           = int(hm_rows.get("steps", 0))
    active_calories = int(hm_rows.get("active_calories", 0))

    # ── most recent weight ────────────────────────────────────────────────────
    # Same root cause as the protein target above: this consulted only
    # `BodyWeightLog`, which is empty here because every weight arrives from Apple into
    # `health_metrics`. The widget reported weight as null. `latest_weight_lbs` checks
    # both sources and is unit-aware.
    weight_lbs = await latest_weight_lbs(db)
    weight_lbs = round(weight_lbs, 1) if weight_lbs is not None else None
    weight_kg  = round(weight_lbs / 2.20462, 1) if weight_lbs is not None else None

    # ── synced_minutes_ago ────────────────────────────────────────────────────
    last_ts_result = await db.execute(select(func.max(HealthMetric.timestamp)))
    last_ts = last_ts_result.scalar()
    synced_minutes_ago = (
        int((now_utc - last_ts).total_seconds() / 60) if last_ts else None
    )

    # ── derived values from daily_metrics ─────────────────────────────────────
    # readiness_score is now NULL on days with insufficient data (no HRV+RHR), so
    # a present dm can still carry a None score — don't render that as a real 0.
    has_readiness = bool(dm and dm.readiness_score is not None)
    readiness_score = round(dm.readiness_score) if has_readiness else 0
    readiness_label = dm.readiness_label if has_readiness else "No data"
    hrv_z           = round(dm.hrv_z_score or 0, 2)    if dm else 0.0
    rhr_z           = round(dm.rhr_z_score or 0, 2)    if dm else 0.0
    # ACWR is None until WARMUP_DAYS of load history exist. `or 0` would render that
    # as 0.00 -> "Undertrained", inventing a verdict from absent data.
    has_acwr        = bool(dm and dm.acwr is not None)
    acwr            = round(dm.acwr, 2) if has_acwr else 0.0
    sleep_debt      = round(dm.sleep_debt_hours or 0, 2) if dm else 0.0

    # ── colours & labels ──────────────────────────────────────────────────────
    acwr_color, acwr_label = (
        _acwr_color_label(acwr) if has_acwr else (GREY, "No data")
    )
    sleep_debt_color, sleep_debt_label = _sleep_debt_color_label(sleep_debt)

    return {
        # Readiness
        "readiness_score": readiness_score,
        "readiness_label": readiness_label,
        "readiness_color": _readiness_color(readiness_score),

        # Sleep
        "sleep_hours":    sleep_hours,
        "sleep_deep_min": sleep_deep_min,
        "sleep_rem_min":  sleep_rem_min,
        "sleep_color":    _sleep_color(sleep_hours, target_sleep),

        # HRV
        "hrv_z":     hrv_z,
        "hrv_label": _hrv_label(hrv_z),
        "hrv_color": _hrv_color(hrv_z),

        # RHR (inverted — lower is better)
        "rhr_z":     rhr_z,
        "rhr_label": _rhr_label(rhr_z),
        "rhr_color": _rhr_color(rhr_z),

        # Training load
        "acwr":       acwr,
        "acwr_label": acwr_label,
        "acwr_color": acwr_color,

        # Sleep debt
        "sleep_debt_hours": sleep_debt,
        "sleep_debt_label": sleep_debt_label,
        "sleep_debt_color": sleep_debt_color,

        # Nutrition
        "protein_g":        protein_g,
        "protein_target_g": protein_target_g,
        "protein_color":    _protein_color(protein_g, protein_target_g),

        # Body weight
        "weight_kg":  weight_kg,
        "weight_lbs": weight_lbs,

        # Activity
        "steps":           steps,
        "steps_goal":      steps_goal,
        "active_calories": active_calories,
        "calories_goal":   calorie_goal,

        # Metadata
        "synced_minutes_ago": synced_minutes_ago,
        "data_freshness":     data_freshness,
        "generated_at":       local_now().strftime("%Y-%m-%dT%H:%M:%S"),
    }
