"""
Illness / overtraining early-warning.

Combines four independent overnight recovery markers — suppressed HRV, elevated
resting HR, elevated wrist (skin) temperature, and elevated respiratory rate — into a
single composite flag. Each on its own is noisy (a low-HRV day is common); the value is
in their *co-occurrence*, which is a well-validated illness/overreaching signal (the
basis of Oura/Whoop "strain"/illness alerts). We require ≥2 markers to declare
"elevated" — specificity over sensitivity, so it doesn't cry wolf.

Marker units (from this dataset): wrist temp deviation is in °F; respiratory rate in
breaths/min; HRV/RHR are 30-day rolling z-scores.
"""
import logging
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DailyMetric, HealthMetric
from app.timeutils import local_today, on_local_days

logger = logging.getLogger(__name__)

# Marker thresholds (calibrated against this athlete's distributions)
HRV_Z_SUPPRESSED = -1.0   # HRV ≥1 SD below baseline
RHR_Z_ELEVATED = 1.0      # RHR ≥1 SD above baseline
WRIST_TEMP_F_ELEVATED = 1.5   # skin temp ≥1.5 °F above 30-day baseline
RESP_RATE_DEV_ELEVATED = 1.5  # respiratory rate ≥1.5 bpm above 30-day baseline


async def _resp_rate_today_and_baseline(
    db: AsyncSession, target_date: date
) -> tuple[Optional[float], Optional[float]]:
    """Today's mean respiratory rate and the mean over the prior 30 days."""
    # Index-served local-day ranges (on_local_days), not a local_date_sql comparison,
    # which scanned every respiratory_rate row twice per call (2026-10-21).
    today_avg = (await db.execute(
        select(func.avg(HealthMetric.value))
        .where(HealthMetric.metric_type == "respiratory_rate")
        .where(on_local_days(HealthMetric.timestamp, target_date))
    )).scalar()

    since = target_date - timedelta(days=30)
    baseline = (await db.execute(
        select(func.avg(HealthMetric.value))
        .where(HealthMetric.metric_type == "respiratory_rate")
        .where(on_local_days(HealthMetric.timestamp, since, target_date - timedelta(days=1)))
    )).scalar()
    return today_avg, baseline


async def assess_recovery_alarm(db: AsyncSession, target_date: Optional[date] = None) -> dict:
    """
    Return the composite recovery-alarm state for the most recent day on/before
    target_date. Shape:
      {date, level: clear|watch|elevated, flagged_count, markers: [{key,label,value,
       detail,flagged,available}]}
    A marker with no data is `available: False` and never contributes to the count.
    """
    target_date = target_date or local_today()

    dm = (await db.execute(
        select(DailyMetric)
        .where(DailyMetric.date <= target_date)
        .order_by(DailyMetric.date.desc())
        .limit(1)
    )).scalar_one_or_none()
    eval_date = dm.date if dm else target_date

    markers: list[dict] = []

    def add(key, label, value, detail, flagged, available):
        markers.append({
            "key": key, "label": label, "value": value, "detail": detail,
            "flagged": bool(flagged and available), "available": available,
        })

    # HRV suppressed
    if dm and dm.hrv_z_score is not None:
        v = dm.hrv_z_score
        add("hrv", "HRV suppressed", round(v, 2), f"z {v:+.1f}", v <= HRV_Z_SUPPRESSED, True)
    else:
        add("hrv", "HRV", None, "no data", False, False)

    # RHR elevated
    if dm and dm.rhr_z_score is not None:
        v = dm.rhr_z_score
        add("rhr", "Resting HR elevated", round(v, 2), f"z {v:+.1f}", v >= RHR_Z_ELEVATED, True)
    else:
        add("rhr", "Resting HR", None, "no data", False, False)

    # Wrist/skin temperature elevated (°F deviation from 30-day baseline)
    if dm and dm.wrist_temp_deviation is not None:
        v = dm.wrist_temp_deviation
        add("wrist_temp", "Skin temp elevated", round(v, 2), f"{v:+.1f}°F",
            v >= WRIST_TEMP_F_ELEVATED, True)
    else:
        add("wrist_temp", "Skin temp", None, "no data", False, False)

    # Respiratory rate elevated vs 30-day baseline
    rr_today, rr_base = await _resp_rate_today_and_baseline(db, eval_date)
    if rr_today is not None and rr_base is not None:
        dev = rr_today - rr_base
        add("resp_rate", "Respiratory rate elevated", round(rr_today, 1),
            f"{rr_today:.1f} vs {rr_base:.1f} bpm", dev >= RESP_RATE_DEV_ELEVATED, True)
    else:
        add("resp_rate", "Respiratory rate", round(rr_today, 1) if rr_today else None,
            "no baseline yet", False, False)

    flagged_count = sum(1 for m in markers if m["flagged"])
    level = "elevated" if flagged_count >= 2 else "watch" if flagged_count == 1 else "clear"

    return {
        "date": str(eval_date),
        "level": level,
        "flagged_count": flagged_count,
        "markers": markers,
    }
