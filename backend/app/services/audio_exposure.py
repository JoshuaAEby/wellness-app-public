"""Noise-dose baseline from Apple's audio-exposure metrics.

WHY DOSE AND NOT dB. Hearing damage accumulates as level x time, like a radiation
dose, and it is irreversible. A daily average dB is close to meaningless — thirty
seconds of a siren and four hours of a loud restaurant can produce the same mean and
completely different risk. So exposure is expressed against a reference limit.

NIOSH: 85 dBA for 8 hours = 100% of the recommended daily dose, with a 3 dB exchange
rate, so every extra 3 dB halves the permitted time (88 dBA for 4 h is also 100%).

    allowed_hours(L) = 8 / 2 ** ((L - 85) / 3)
    dose_fraction    = hours_at_L / allowed_hours(L)

ENVIRONMENTAL AND HEADPHONE ARE KEPT SEPARATE, deliberately. Headphone exposure is
almost entirely under your control and is where avoidable damage happens; environmental
exposure mostly is not. A combined number would hide the only one worth acting on.

HONEST LIMITS: a watch microphone is not a dosimeter — Apple's values are approximate,
sampling is intermittent, and a phone in a bag under-reads. Treat the level as a TREND
signal, not an occupational measurement. The purpose here is detecting a change in
acoustic environment over months, not certifying a workplace.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import HealthMetric
from app.timeutils import local_date_sql, local_day_bounds_utc, local_today

NIOSH_REF_DB = 85.0
NIOSH_REF_HOURS = 8.0
EXCHANGE_RATE_DB = 3.0

# A sample's duration is taken as the gap to the next sample, capped. Apple's sampling
# is irregular, so an uncapped gap would let one reading stand in for an entire night
# and dominate the day. Mirrors the 30-second inter-sample cap already used for workout
# HR in metrics_engine._workout_metrics.
MAX_SAMPLE_MINUTES = 30.0
# Below this, exposure contributes effectively nothing and Apple's low-end readings are
# least reliable; including them adds noise, not dose.
MIN_DB = 55.0

METRICS = {"environmental": "environmental_audio_exposure",
           "headphone": "headphone_audio_exposure"}


def allowed_hours(db_level: float) -> float:
    return NIOSH_REF_HOURS / (2 ** ((db_level - NIOSH_REF_DB) / EXCHANGE_RATE_DB))


async def daily_dose(db: AsyncSession, metric_type: str,
                     start: date, end: date) -> dict[str, float]:
    """{local_date: dose_pct} where 100 = the NIOSH recommended daily limit."""
    rows = (await db.execute(
        select(local_date_sql(HealthMetric.timestamp).label("day"),
               HealthMetric.timestamp, HealthMetric.value)
        .where(HealthMetric.metric_type == metric_type)
        .where(HealthMetric.timestamp >= local_day_bounds_utc(start)[0])
        .where(HealthMetric.timestamp < local_day_bounds_utc(end)[1])
        .order_by(HealthMetric.timestamp)
    )).all()

    out: dict[str, float] = {}
    for i, r in enumerate(rows):
        level = float(r.value or 0.0)
        if level < MIN_DB:
            continue
        if i + 1 < len(rows):
            gap = (rows[i + 1].timestamp - r.timestamp).total_seconds() / 60.0
        else:
            gap = MAX_SAMPLE_MINUTES
        minutes = max(0.0, min(gap, MAX_SAMPLE_MINUTES))
        hours = minutes / 60.0
        allowed = allowed_hours(level)
        if allowed <= 0:
            continue
        key = str(r.day)
        out[key] = out.get(key, 0.0) + (hours / allowed) * 100.0
    return out


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


async def weekly_series(db: AsyncSession, weeks: int = 52) -> dict:
    """Per-week mean daily dose for each source, plus a drift verdict.

    Reported as MEAN DAILY dose within the week rather than a weekly total, so the
    number keeps its meaning: 100 = a day at the recommended limit.
    """
    end = local_today()
    start = _monday(end) - timedelta(weeks=weeks - 1)

    series: dict[str, dict[str, dict]] = {}
    for label, metric_type in METRICS.items():
        per_day = await daily_dose(db, metric_type, start, end)
        buckets: dict[str, list[float]] = {}
        for day_str, dose in per_day.items():
            wk = str(_monday(date.fromisoformat(day_str)))
            buckets.setdefault(wk, []).append(dose)
        series[label] = {
            wk: {"mean_dose_pct": round(sum(v) / len(v), 1),
                 "max_dose_pct": round(max(v), 1),
                 "days": len(v),
                 "days_over_limit": sum(1 for x in v if x >= 100.0)}
            for wk, v in buckets.items()
        }

    weeks_axis = sorted({wk for s in series.values() for wk in s})
    out_weeks = [
        {"week": wk,
         **{label: series[label].get(wk) for label in METRICS}}
        for wk in weeks_axis
    ]
    return {"weeks": out_weeks,
            "reference": {"db": NIOSH_REF_DB, "hours": NIOSH_REF_HOURS,
                          "exchange_db": EXCHANGE_RATE_DB},
            "drift": {label: _drift([series[label].get(wk, {}).get("mean_dose_pct")
                                     for wk in weeks_axis])
                      for label in METRICS}}


def _drift(values: list[float | None], halves: int = 12) -> dict:
    """Recent vs preceding block of weeks. A baseline is for spotting a CHANGE.

    Deliberately blunt — a median-to-median comparison of two adjacent blocks, not a
    significance test. With ~52 weekly points and a slow variable, a formal trend test
    would mostly report the seasonality of one person's life; what matters is whether
    the recent level has moved materially, and by how much.
    """
    vals = [v for v in values if v is not None]
    if len(vals) < halves + 4:
        return {"status": "accruing", "weeks": len(vals), "need": halves + 4}
    recent, prior = vals[-halves:], vals[-(halves * 2):-halves] or vals[:-halves]
    if not prior:
        return {"status": "accruing", "weeks": len(vals), "need": halves + 4}
    med = lambda x: sorted(x)[len(x) // 2]
    r, p = med(recent), med(prior)
    if p <= 0:
        return {"status": "flat", "recent": round(r, 1), "prior": round(p, 1)}
    change = (r - p) / p * 100.0
    status = "rising" if change >= 25 else "falling" if change <= -25 else "flat"
    return {"status": status, "recent": round(r, 1), "prior": round(p, 1),
            "change_pct": round(change, 1)}
