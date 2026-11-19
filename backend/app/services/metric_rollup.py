"""Collapse high-frequency metrics that are only ever read as daily totals.

Built 2026-08-30. `resting_calories` was 866,084 rows — 35.7% of health_metrics
and roughly a third of a 422 MB database — arriving at ~1,434 samples/day for a
metric NOTHING reads at sub-daily resolution:

  * `resolve_rmr` deliberately refuses it (energy.py) — Apple does not measure
    basal rate, it models it from the same anthropometrics Mifflin uses, and it
    reads ~380 kcal/day high here.
  * The three places that DO use it — /metrics/activity, /metrics/today and the
    streaks builder — all call `func.sum(...)` grouped by local day.

So every consumer sees an identical number after a rollup, and ~865k rows go
away. This is storage, not semantics: the daily total is preserved exactly.

WHY A SAFETY LAG. Health Auto Export re-sends historical windows, and ingestion
upserts on (metric_type, timestamp). A resend for an already-rolled-up day would
recreate the intraday rows NEXT TO the rollup row and silently double the day's
total. Two defences: only roll up days older than ROLLUP_LAG_DAYS, and make the
job self-healing — if intraday rows reappear for a rolled-up day, they are
treated as the authoritative copy and the day is rebuilt from them.

The rollup row is tagged `source=ROLLUP_SOURCE` so it is always distinguishable
from real samples. Never sum a rollup row together with intraday rows.
"""
import logging
from datetime import date, datetime, time, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import HealthMetric
from app.timeutils import local_day_bounds_utc, local_today

logger = logging.getLogger(__name__)

ROLLUP_SOURCE = "daily_rollup"

# Days that must have elapsed before a day is eligible. Comfortably beyond the
# window Health Auto Export re-sends, so a rollup is not racing an incoming batch.
ROLLUP_LAG_DAYS = 14

# Metrics safe to collapse: every consumer reads them as a per-local-day sum.
# Do NOT add a metric here without checking for sub-daily readers first —
# `heart_rate` and `hrv_sdnn`, for instance, are read intraday and must stay.
ROLLUP_METRICS = ("resting_calories",)

# HOURLY rollup (2026-10-21: "we don't need minute-level details from more than 14
# days ago"). Summed metrics whose readers are all per-day sums or, for the energy
# projection (energy.projected_active: "accrued before this hour"), per-HOUR — so an
# hourly total per UTC hour preserves every number the app computes. 791k active_calories +
# 230k steps + 111k distance + 16k exercise rows → ~one row per hour each.
# Deliberately NOT here: heart_rate (workout TRIMP, HR recovery and early-night HR are
# recomputed from raw samples, e.g. after a max-HR change), time_in_daylight (morning
# light compares exact times to sunrise), and any AVERAGED metric (a sum would be wrong).
# Hours are UTC hours, which align with local hours for every whole-hour UTC offset.
HOURLY_ROLLUP_SOURCE = "hourly_rollup"
HOURLY_ROLLUP_METRICS = ("active_calories", "steps", "walking_running_distance", "exercise_minutes")


def _rollup_timestamp(day: date) -> datetime:
    """Local noon for `day`, as naive UTC.

    Noon rather than the day's start so the row sits unambiguously inside its own
    local day under any bucketing — `local_date_sql` and half-open [start, end)
    windows alike. A boundary timestamp would be one DST shift away from landing
    on the wrong day.
    """
    start, end = local_day_bounds_utc(day)
    return start + (end - start) / 2


async def rollup_metric_day(
    db: AsyncSession, metric_type: str, day: date
) -> int | None:
    """Collapse one metric-day to a single row. Returns rows removed, or None if
    there was nothing to do. Idempotent, and self-healing if samples reappear."""
    start, end = local_day_bounds_utc(day)
    rows = (await db.execute(
        select(HealthMetric)
        .where(HealthMetric.metric_type == metric_type)
        .where(HealthMetric.timestamp >= start)
        .where(HealthMetric.timestamp < end)
    )).scalars().all()
    if not rows:
        return None

    intraday = [r for r in rows if r.source != ROLLUP_SOURCE]
    if not intraday:
        return None            # already collapsed, nothing new arrived

    # Intraday samples are authoritative: if they reappeared after a rollup, the
    # resend is the fresher copy and the stale rollup row is discarded with them.
    total = sum(r.value for r in intraday)
    unit = next((r.unit for r in intraday if r.unit), None)

    await db.execute(
        delete(HealthMetric)
        .where(HealthMetric.metric_type == metric_type)
        .where(HealthMetric.timestamp >= start)
        .where(HealthMetric.timestamp < end)
    )
    db.add(HealthMetric(
        metric_type=metric_type,
        timestamp=_rollup_timestamp(day),
        value=total,
        unit=unit,
        source=ROLLUP_SOURCE,
    ))
    await db.flush()
    return len(rows)


def _ts19(dt: datetime) -> str:
    """The storage format of every ingested timestamp ('YYYY-MM-DD HH:MM:SS', 19 chars)."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


async def rollup_metric_day_hourly(db: AsyncSession, metric_type: str, day: date) -> int:
    """Collapse one metric-day to one row per UTC hour. Returns rows removed (net).

    Self-healing PER HOUR: an hour that has raw samples again (a Health Auto Export
    resend) is rebuilt from those samples PLUS its existing rollup row is replaced; hours
    without new samples keep their rollup. (The daily rollup heals per day, which would
    drop untouched hours on a partial resend.)

    EXACT BY CONSTRUCTION (2026-10-21). Rows are stored as 19-char strings, but SQLAlchemy
    binds a datetime as 'YYYY-MM-DD HH:MM:SS.ffffff'; SQLite compares text, so a sample
    exactly on the hour sorts BEFORE its own hour's lower bound. A range-based delete missed
    those rows while the sum counted them — measured double-counting on 1,419 days in the
    first test run. So: day bounds are literal 19-char strings, and the delete removes
    exactly the row ids that were summed.
    """
    from collections import defaultdict
    from sqlalchemy import literal_column, text
    start, end = local_day_bounds_utc(day)
    lo, hi = literal_column(f"'{_ts19(start)}'"), literal_column(f"'{_ts19(end)}'")
    rows = (await db.execute(
        select(HealthMetric.id, HealthMetric.timestamp, HealthMetric.value,
               HealthMetric.unit, HealthMetric.source)
        .where(HealthMetric.metric_type == metric_type)
        .where(HealthMetric.timestamp >= lo)
        .where(HealthMetric.timestamp < hi)
    )).all()
    by_hour: dict = defaultdict(list)
    for r in rows:
        by_hour[r.timestamp.replace(minute=0, second=0, microsecond=0)].append(r)
    removed = 0
    for hour, hrows in by_hour.items():
        raw = [r for r in hrows if r.source != HOURLY_ROLLUP_SOURCE]
        if not raw:
            continue                               # already collapsed, nothing new
        # A re-sent hour: the new raw samples are authoritative, the old rollup is stale.
        total = sum(r.value for r in raw)
        unit = next((r.unit for r in raw if r.unit), None)
        ids = [r.id for r in hrows]
        for i in range(0, len(ids), 500):
            await db.execute(delete(HealthMetric).where(HealthMetric.id.in_(ids[i:i + 500])))
        await db.execute(text(
            "INSERT INTO health_metrics (metric_type, timestamp, value, unit, source) "
            "VALUES (:m, :t, :v, :u, :s)"),
            # Mid-hour, clamped into the local day: in a half-hour-offset zone a UTC hour
            # straddles local midnight and the plain midpoint would land in the next day.
            {"m": metric_type,
             "t": _ts19(max(start, min(hour + timedelta(minutes=30), end - timedelta(seconds=1)))),
             "v": total,
             "u": unit, "s": HOURLY_ROLLUP_SOURCE})
        removed += len(hrows) - 1
    return removed


async def rollup_old_metrics(
    db: AsyncSession, metrics: tuple[str, ...] = ROLLUP_METRICS,
    lag_days: int = ROLLUP_LAG_DAYS, since: date | None = None,
    hourly: tuple[str, ...] = HOURLY_ROLLUP_METRICS,
) -> dict:
    """Roll up every eligible day for each metric. Safe to re-run at any time."""
    cutoff = local_today() - timedelta(days=lag_days)
    out: dict = {"cutoff": cutoff.isoformat(), "metrics": {}}

    for metric_type in metrics:
        first = (await db.execute(
            select(func.min(HealthMetric.timestamp))
            .where(HealthMetric.metric_type == metric_type)
        )).scalar()
        if first is None:
            out["metrics"][metric_type] = {"days": 0, "rows_removed": 0}
            continue

        day = since or (first - timedelta(days=1)).date()
        days = rows_removed = 0
        while day < cutoff:
            removed = await rollup_metric_day(db, metric_type, day)
            if removed:
                days += 1
                rows_removed += removed - 1     # net of the row we wrote back
            day += timedelta(days=1)
        await db.commit()
        out["metrics"][metric_type] = {"days": days, "rows_removed": rows_removed}
        if days:
            logger.info("Rollup: %s collapsed %d days, removed %d rows",
                        metric_type, days, rows_removed)

    for metric_type in hourly:
        first = (await db.execute(
            select(func.min(HealthMetric.timestamp))
            .where(HealthMetric.metric_type == metric_type)
        )).scalar()
        if first is None:
            out["metrics"][metric_type] = {"days": 0, "rows_removed": 0, "granularity": "hour"}
            continue
        day = since or (first - timedelta(days=1)).date()
        days = rows_removed = 0
        while day < cutoff:
            n = await rollup_metric_day_hourly(db, metric_type, day)
            if n:
                days += 1
                rows_removed += n
            day += timedelta(days=1)
            if days and days % 30 == 0:
                await db.commit()                  # keep transactions (and the WAL) small
        await db.commit()
        out["metrics"][metric_type] = {"days": days, "rows_removed": rows_removed, "granularity": "hour"}
        if days:
            logger.info("Rollup (hourly): %s collapsed %d days, removed %d rows",
                        metric_type, days, rows_removed)
    return out
