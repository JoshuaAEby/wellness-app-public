"""Sleep processing — HALF OF THIS FILE IS DEAD, AND THE OTHER HALF IS LOAD-BEARING.

DEAD (never runs, and cannot): `process_sleep_for_dates`, `_process_one_date`,
`_group_into_sessions`, `_session_wake_time`, `_sum_stages`, `SLEEP_STAGE_TYPES`.
These reconstruct a hypnogram from per-stage samples, and those samples do not exist —
Health Auto Export sends PRE-SUMMARISED nightly totals, so `sleep_stage` and
`sleep_analysis` have 0 rows and always have. Verified 2026-08-17. Do not try to revive
this by "fixing" ingestion; the data is not in the export to begin with.

LIVE and used: `compute_sleep_hr_elevation` — imported by `services/morning.py`, produces
the `sleep_hr_elevation` metric (592 rows) which carries real signal in the correlation
cache. DO NOT delete this file on the grounds that the stage code is dead.

Kept rather than deleted because the stage machinery is the correct implementation for
the day Apple or HAE starts exporting stage samples; it is waiting on data, not broken.
"""
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import HealthMetric, SleepSession

logger = logging.getLogger(__name__)

# Stage metric_types that indicate actual sleep (used for bedtime/wake boundaries)
SLEEP_STAGE_TYPES = {"sleep_inbed", "sleep_asleep", "sleep_deep", "sleep_core", "sleep_rem", "sleep_awake"}

# Gap threshold for splitting into separate sessions (minutes)
SESSION_GAP_MINUTES = 30


async def process_sleep_for_dates(db: AsyncSession, dates: list[date]) -> None:
    """
    For each date in `dates`, query raw sleep stage samples in a ±2 day window
    (to capture overnight sessions crossing midnight), group into sessions,
    and upsert into sleep_sessions keyed on the wake date.
    """
    for target_date in dates:
        try:
            await _process_one_date(db, target_date)
        except Exception as exc:
            logger.error("Sleep processing failed for %s: %s", target_date, exc)


async def _process_one_date(db: AsyncSession, target_date: date) -> None:
    # Query a ±36-hour window around the target date to capture overnight sessions
    window_start = datetime.combine(target_date - timedelta(days=1), datetime.min.time()) + timedelta(hours=12)
    window_end = datetime.combine(target_date + timedelta(days=1), datetime.min.time()) + timedelta(hours=12)

    result = await db.execute(
        select(HealthMetric)
        .where(HealthMetric.metric_type.in_(SLEEP_STAGE_TYPES))
        .where(HealthMetric.timestamp >= window_start)
        .where(HealthMetric.timestamp < window_end)
        .order_by(HealthMetric.timestamp)
    )
    samples = result.scalars().all()

    if not samples:
        return

    # Group samples into contiguous sessions using gap threshold
    sessions = _group_into_sessions(samples)

    # Find the session whose wake_time falls on target_date
    for session_samples in sessions:
        wake_time = _session_wake_time(session_samples)
        if wake_time is None:
            continue
        if wake_time.date() != target_date:
            continue

        bedtime = session_samples[0].timestamp
        stage_totals = _sum_stages(session_samples)

        total_min = sum(stage_totals.values())
        if total_min < 30:  # ignore sub-30-min noise
            continue

        stmt = sqlite_insert(SleepSession).values(
            date=target_date,
            bedtime=bedtime,
            wake_time=wake_time,
            total_min=int(total_min),
            deep_min=int(stage_totals.get("sleep_deep", 0)),
            core_min=int(stage_totals.get("sleep_core", 0) + stage_totals.get("sleep_asleep", 0)),
            rem_min=int(stage_totals.get("sleep_rem", 0)),
            awake_min=int(stage_totals.get("sleep_awake", 0)),
            source="apple_health",
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["date"],
            set_={
                "bedtime": bedtime,
                "wake_time": wake_time,
                "total_min": int(total_min),
                "deep_min": int(stage_totals.get("sleep_deep", 0)),
                "core_min": int(stage_totals.get("sleep_core", 0) + stage_totals.get("sleep_asleep", 0)),
                "rem_min": int(stage_totals.get("sleep_rem", 0)),
                "awake_min": int(stage_totals.get("sleep_awake", 0)),
                "source": "apple_health",
            },
        )
        await db.execute(stmt)
        logger.debug("Upserted sleep session for %s: %d min total", target_date, int(total_min))
        break  # only one session per date


def _group_into_sessions(samples: list[HealthMetric]) -> list[list[HealthMetric]]:
    """Split samples into sessions separated by gaps > SESSION_GAP_MINUTES."""
    if not samples:
        return []

    sessions: list[list[HealthMetric]] = [[samples[0]]]
    for sample in samples[1:]:
        last = sessions[-1][-1]
        # Approximate end of last sample = timestamp + duration (value is minutes)
        last_end = last.timestamp + timedelta(minutes=last.value)
        gap = (sample.timestamp - last_end).total_seconds() / 60.0
        if gap > SESSION_GAP_MINUTES:
            sessions.append([sample])
        else:
            sessions[-1].append(sample)
    return sessions


def _session_wake_time(samples: list[HealthMetric]) -> datetime | None:
    """Compute the wake time as the end of the last sample in the session."""
    if not samples:
        return None
    last = samples[-1]
    return last.timestamp + timedelta(minutes=last.value)


def _sum_stages(samples: list[HealthMetric]) -> dict[str, float]:
    """Sum duration (minutes) per stage type."""
    totals: dict[str, float] = {}
    for s in samples:
        # Only count actual sleep stages, not INBED (which overlaps with stages)
        if s.metric_type == "sleep_inbed":
            continue
        totals[s.metric_type] = totals.get(s.metric_type, 0.0) + s.value
    return totals


# ── Early-night HR elevation (late-digestion / arousal signature) ──────────────
#
# Per night: mean heart rate over the FIRST third of the sleep window minus the
# LAST third. The last third approximates the person's stable overnight floor,
# so night-to-night variation in the difference is dominated by early-night
# elevation — the physiological signature of late meals, alcohol, and pre-bed
# arousal. HIGHER = worse. Stored as health_metrics rows (metric_type
# "sleep_hr_elevation", timestamped at wake, source "derived") so it rides the
# normal machinery: correlations outcome, trends registry, Recovery chart.

MIN_SAMPLES_PER_THIRD = 5


async def compute_sleep_hr_elevation(db: AsyncSession, wake_days: list[date]) -> int:
    """Compute + upsert sleep_hr_elevation for the given wake dates. Idempotent."""
    from app.services.ingestion import _upsert_metric

    written = 0
    for day in wake_days:
        session = (await db.execute(
            select(SleepSession).where(SleepSession.date == day)
        )).scalar_one_or_none()
        if session is None or not session.bedtime or not session.wake_time:
            continue
        span = (session.wake_time - session.bedtime).total_seconds()
        if span < 3 * 3600:  # < 3h of sleep: thirds are meaningless
            continue
        third = timedelta(seconds=span / 3)

        async def _mean_hr(t0, t1):
            rows = (await db.execute(
                select(HealthMetric.value)
                .where(HealthMetric.metric_type == "heart_rate")
                .where(HealthMetric.timestamp >= t0)
                .where(HealthMetric.timestamp < t1)
            )).scalars().all()
            if len(rows) < MIN_SAMPLES_PER_THIRD:
                return None
            return sum(rows) / len(rows)

        early = await _mean_hr(session.bedtime, session.bedtime + third)
        late = await _mean_hr(session.wake_time - third, session.wake_time)
        if early is None or late is None:
            continue
        await _upsert_metric(db, "sleep_hr_elevation", session.wake_time,
                             round(early - late, 1), "bpm", "derived")
        written += 1
    await db.commit()
    return written


# ── Early-night HR, FIXED window (2026-10-27, correlation review #6) ───────────
#
# sleep_hr_elevation's windows are THIRDS of the night, so both scale with night length:
# a longer night moves "late" later and widens "early". Sleep duration → elevation and
# bedtime → elevation (both TRADE-OFF rows in Findings) could therefore be arithmetic
# rather than physiology. This variant removes the length dependence:
#
#     mean HR over the first EARLY_FIXED_H hours after sleep onset
#   − the night's 10th-percentile HR (the overnight RHR floor, note 90)
#
# Sleep onset, not a clock time: a fixed CLOCK window would confound with bedtime, which
# is one of the pairs under test. Stored ALONGSIDE sleep_hr_elevation (metric
# "sleep_hr_early_fixed", same timestamp/source conventions); it is NOT a correlation
# outcome until the user decides — adding it would widen the BH family for every pair.
EARLY_FIXED_H = 2.0
EARLY_FIXED_MIN_SPAN_H = 3.0     # the 2 h window must not be most of the night


def early_minus_floor(early_vals: list[float], night_vals: list[float]) -> float | None:
    """Mean of the early window minus the night's p10, or None when either is too thin.

    The p10 uses metrics_engine's exact rule (RHR_NIGHT_PCTL / RHR_NIGHT_MIN_SAMPLES), so
    the floor here is the same number readiness calls overnight RHR.
    """
    from app.services.metrics_engine import RHR_NIGHT_MIN_SAMPLES, RHR_NIGHT_PCTL
    if len(early_vals) < MIN_SAMPLES_PER_THIRD or len(night_vals) < RHR_NIGHT_MIN_SAMPLES:
        return None
    vals = sorted(night_vals)
    floor = vals[max(0, int(RHR_NIGHT_PCTL * len(vals)) - 1)]
    return round(sum(early_vals) / len(early_vals) - floor, 1)


async def compute_sleep_hr_early_fixed(db: AsyncSession, wake_days: list[date]) -> int:
    """Compute + upsert sleep_hr_early_fixed for the given wake dates. Idempotent."""
    from app.services.ingestion import _upsert_metric

    written = 0
    for day in wake_days:
        session = (await db.execute(
            select(SleepSession).where(SleepSession.date == day)
        )).scalar_one_or_none()
        if session is None or not session.bedtime or not session.wake_time:
            continue
        if (session.wake_time - session.bedtime).total_seconds() < EARLY_FIXED_MIN_SPAN_H * 3600:
            continue
        rows = (await db.execute(
            select(HealthMetric.timestamp, HealthMetric.value)
            .where(HealthMetric.metric_type == "heart_rate")
            .where(HealthMetric.timestamp >= session.bedtime)
            .where(HealthMetric.timestamp <= session.wake_time)
        )).all()
        cut = session.bedtime + timedelta(hours=EARLY_FIXED_H)
        value = early_minus_floor([v for t, v in rows if t < cut and v is not None],
                                  [v for _, v in rows if v is not None])
        if value is None:
            continue
        await _upsert_metric(db, "sleep_hr_early_fixed", session.wake_time, value, "bpm", "derived")
        written += 1
    await db.commit()
    return written
