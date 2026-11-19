"""Health events (note 93, 2026-10-31): the days around a vaccine, procedure or
medication change, against his own recent baseline.

Descriptive only. One event a year cannot size a personal effect, so this prints the
numbers and the obvious rivals (drinks and calories the day before) and draws no
conclusion. Nothing here touches baselines, correlations or experiments: an event is not
a day flag, and an exclusion rule invented after the fact would be another forking path.

Nights are keyed by WAKE date, so offset +1 is the first night after the event; intake is
the FOOD day before each row, the day that could have moved that night.
"""
from datetime import date, timedelta
from statistics import mean, stdev
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.foodday import food_date_sql, food_day_bounds_utc, on_food_days_utc
from app.models import (DailyMetric, DayFlag, FoodLog, HealthEvent, HealthMetric, SleepSession,
                        SubjectiveLog)
from app.services.day_flags import excluded_dates
from app.timeutils import local_food_date_sql

OFFSETS = (-1, 0, 1, 2, 3)
SICK_OFFSETS = (-3, -2, -1, 0, 1)   # the run-up matters more than the days he already knew
SICK_GAP_DAYS = 2                   # sick days at most this far apart are one episode
BASELINE_DAYS = 60          # long enough that a two-week trip still leaves a baseline
BASELINE_MIN = 7
UNUSUAL_Z = 2.0
NOTE_DAYS = 2               # mornings after the event that the briefing mentions it


async def _overnight_breathing(db: AsyncSession, start: date, end: date) -> dict:
    """{wake date: mean respiratory_rate between bedtime and wake}. The daily mean the
    alarm reads mixes in waking samples, which move with activity."""
    nights = (await db.execute(
        select(SleepSession.date, SleepSession.bedtime, SleepSession.wake_time)
        .where(SleepSession.date >= start).where(SleepSession.date <= end)
    )).all()
    nights = [n for n in nights if n.bedtime and n.wake_time]
    if not nights:
        return {}
    rows = (await db.execute(
        select(HealthMetric.timestamp, HealthMetric.value)
        .where(HealthMetric.metric_type == "respiratory_rate")
        .where(HealthMetric.timestamp >= min(n.bedtime for n in nights))
        .where(HealthMetric.timestamp <= max(n.wake_time for n in nights))
    )).all()
    out = {}
    for n in nights:
        vals = [v for t, v in rows if n.bedtime <= t <= n.wake_time and v is not None]
        if vals:
            out[n.date] = mean(vals)
    return out


async def sick_episodes(db: AsyncSession) -> list[dict]:
    """Runs of sick days, newest first, read from both places sickness is stored: the
    `sick` day flag and the legacy subjective_log.sick. A view only; flags stay the record."""
    days = set((await db.execute(
        select(DayFlag.date).where(DayFlag.reason == "sick"))).scalars())
    days |= set((await db.execute(
        select(SubjectiveLog.date).where(SubjectiveLog.sick.is_(True)))).scalars())
    runs: list[list[date]] = []
    for d in sorted(days):
        if runs and (d - runs[-1][-1]).days <= SICK_GAP_DAYS:
            runs[-1].append(d)
        else:
            runs.append([d])
    return [{"start": r[0].isoformat(), "end": r[-1].isoformat(), "days": len(r)}
            for r in reversed(runs)]


async def event_response(db: AsyncSession, day: date, offsets: tuple = OFFSETS) -> dict:
    """The rows around `day`, each metric with its z against the prior BASELINE_DAYS,
    ending the day before the first row so a run-up cannot leak into its own baseline
    (flagged days out)."""
    last = day + timedelta(days=max(offsets))
    base_end = day + timedelta(days=min(offsets) - 1)
    start = base_end - timedelta(days=BASELINE_DAYS - 1)
    excluded = await excluded_dates(db, start, base_end)

    series: dict = {k: {} for k in ("hrv", "rhr", "wrist_temp", "energy", "mood")}
    for r in (await db.execute(select(DailyMetric).where(DailyMetric.date >= start)
                               .where(DailyMetric.date <= last))).scalars():
        series["hrv"][r.date] = r.hrv_value
        series["rhr"][r.date] = r.rhr_value
        series["wrist_temp"][r.date] = r.wrist_temp_deviation
    for r in (await db.execute(select(SubjectiveLog).where(SubjectiveLog.date >= start)
                               .where(SubjectiveLog.date <= last))).scalars():
        series["energy"][r.date] = r.energy
        series["mood"][r.date] = r.mood
    series["breathing"] = await _overnight_breathing(db, start, last)

    baseline = {}
    for key, by_day in series.items():
        vals = [v for d, v in by_day.items()
                if v is not None and d <= base_end and d not in excluded]
        ok = len(vals) >= BASELINE_MIN and stdev(vals) > 0
        baseline[key] = {"n": len(vals), "mean": round(mean(vals), 2) if ok else None,
                         "sd": round(stdev(vals), 2) if ok else None}

    first_intake = base_end
    alc_day = local_food_date_sql(HealthMetric.timestamp)
    drinks = {r.d: r.total for r in (await db.execute(
        select(alc_day.label("d"), func.sum(HealthMetric.value).label("total"))
        .where(HealthMetric.metric_type == "alcohol")
        .where(on_food_days_utc(HealthMetric.timestamp, first_intake, last))
        .group_by(alc_day)
    )).all()}
    food_day = food_date_sql(FoodLog.timestamp)
    kcal = {r.d: r.total for r in (await db.execute(
        select(food_day.label("d"), func.sum(FoodLog.calories).label("total"))
        .where(FoodLog.timestamp >= food_day_bounds_utc(first_intake)[0] - timedelta(days=1))
        .where(FoodLog.timestamp < food_day_bounds_utc(last)[1] + timedelta(days=1))
        .group_by(food_day)
    )).all()}

    def cell(key: str, d: date) -> dict:
        v = series[key].get(d)
        b = baseline[key]
        z = None if v is None or b["sd"] is None else round((v - b["mean"]) / b["sd"], 1)
        return {"value": None if v is None else round(v, 2), "z": z,
                "unusual": z is not None and abs(z) >= UNUSUAL_Z}

    rows = []
    for off in offsets:
        d = day + timedelta(days=off)
        before = (d - timedelta(days=1)).isoformat()
        rows.append({
            "offset": off, "date": d.isoformat(),
            **{k: cell(k, d) for k in ("hrv", "rhr", "breathing", "wrist_temp", "energy", "mood")},
            # Absent alcohol is a real zero (alcohol is logged completely); absent food is not.
            "drinks_day_before": round(drinks.get(before) or 0, 1),
            "kcal_day_before": round(kcal[before]) if kcal.get(before) else None,
        })
    return {"date": day.isoformat(), "baseline": baseline, "baseline_days": BASELINE_DAYS,
            "unusual_z": UNUSUAL_Z, "rows": rows}


async def recent_event_note(db: AsyncSession, day: date) -> str:
    """'' unless an event falls 1–NOTE_DAYS days before `day`. Not on the event's own day:
    that morning's numbers come from the night before it."""
    rows = (await db.execute(
        select(HealthEvent)
        .where(HealthEvent.date >= day - timedelta(days=NOTE_DAYS))
        .where(HealthEvent.date < day)
        .order_by(HealthEvent.date.desc(), HealthEvent.id)
    )).scalars().all()
    if not rows:
        return ""
    ago = lambda n: "yesterday" if n == 1 else f"{n} days ago"
    what = "; ".join(f"{r.name} ({r.kind}) {ago((day - r.date).days)}" for r in rows)
    return (f"HEALTH EVENT: {what} — it can shift HRV, RHR, breathing rate, wrist temp and "
            f"energy for a day or two. Mention it as one possible contributor, not the proven "
            f"cause, and keep anything else from yesterday in view.")
