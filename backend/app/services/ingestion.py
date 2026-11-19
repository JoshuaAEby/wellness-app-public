"""Parse Health Auto Export JSON payload and upsert into the database."""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import HealthMetric, Workout
from app.services.workout_qc import suspect_reason
from app.services.metrics_engine import recompute_dates
from app.timeutils import to_local_date

logger = logging.getLogger(__name__)

# Above this many affected dates, the recompute is treated as a BACKFILL and moved
# off the request path. Health Auto Export applies a client-side timeout to the
# webhook response, and recompute_dates rebuilds ATL/CTL, z-scores and readiness for
# every date it is given — a 235-date historical export took minutes, so HAE reported
# "request timed out" at ~80% even though every row had already been ingested and
# committed. The upload was never the problem; the reply was.
#
# Ordinary syncs (1-3 dates) stay INLINE deliberately: they finish in well under a
# second, and the event-driven morning pipeline fires straight after ingestion on the
# assumption that today's metrics are already current.
BACKFILL_RECOMPUTE_THRESHOLD = 14

# Serialises backfill recomputes. Several export chunks posted back-to-back would
# otherwise each spawn a task and thrash SQLite, which has one writer.
_backfill_lock = asyncio.Lock()


async def _recompute_backfill(dates: list[date]) -> None:
    """Recompute a large date range off the request path, with its own session.

    A fresh session is mandatory — the request-scoped `db` is closed as soon as the
    webhook responds, so reusing it here would fail once the response has gone out.
    """
    async with _backfill_lock:
        try:
            from app.database import AsyncSessionLocal
            async with AsyncSessionLocal() as bg_db:
                await recompute_dates(bg_db, dates)
            logger.info("Backfill recompute finished (%d dates: %s … %s)",
                        len(dates), dates[0], dates[-1])
        except Exception as exc:
            logger.error("Backfill recompute failed (%d dates): %s", len(dates), exc)

# Map Health Auto Export metric name → internal metric_type identifier
METRIC_NAME_MAP: dict[str, str] = {
    "heart_rate_variability": "hrv_sdnn",
    "resting_heart_rate": "resting_heart_rate",
    "active_energy": "active_calories",
    "basal_energy_burned": "resting_calories",
    "weight_body_mass": "weight",
    "heart_rate": "heart_rate",
    "step_count": "steps",
    "vo2_max": "vo2_max",
    "apple_sleeping_wrist_temperature": "wrist_temperature",
    "respiratory_rate": "respiratory_rate",
    "apple_exercise_time": "exercise_minutes",
    "apple_stand_hour": "stand_hours",
    "alcohol_consumption": "alcohol",     # standard drink units (SDU) per entry
    "time_in_daylight": "time_in_daylight",  # minutes outdoors in daylight (samples timestamped)
    # ── Recovery / health signals added 2026-07-02 (Tier 1 + 2) ─────────────────
    # The export keys below are best-guess snake_cased HealthKit identifiers and are
    # NOT yet confirmed against a real Health Auto Export payload. After the first
    # sync with these metrics enabled, verify with:
    #     SELECT DISTINCT metric_type FROM health_metrics;
    # Any block whose key we don't match is logged at DEBUG ("Unrecognised metric
    # block") and silently stored nowhere — so an unconfirmed key = missing data,
    # not an error. Entries flagged ⚠ are the ones most likely to differ.
    "walking_heart_rate_average": "walking_heart_rate_avg",  # submaximal HR proxy; complements RHR
    # Restored 2026-08-19. 6,664 rows accrued to 2026-07-05 and then stopped — not
    # because Apple stopped recording, but because HAE sends it as
    # `walking_asymmetry_percentage` and the map had no such key, so every sync since
    # has silently discarded it. Found via the "Unmapped metric blocks" log line, which
    # is the ground truth for what is arriving and being dropped — check it after any
    # HAE update.
    "walking_asymmetry_percentage": "walking_asymmetry",
    # Audio exposure (dBA). Ingested as a long-horizon BASELINE — the value is detecting
    # a change in acoustic environment over months, not any single day. Kept as two
    # separate metrics on purpose: headphone exposure is almost entirely under your
    # control and is where avoidable damage happens, environmental mostly is not, and
    # merging them would hide the only one worth acting on.
    # Apple "Mobility" suite — iPhone-carried, not watch-derived, so coverage depends on
    # having the phone on you. Slow-moving functional markers; ingested as baselines.
    # walking_speed is the headline: gait speed is among the best-validated functional
    # measures there is, and it is the only thing that can separate "fitter" from
    # "walking slower" when walk HR falls at constant duration.
    "walking_speed": "walking_speed",
    "walking_running_distance": "walking_running_distance",
    "walking_double_support_percentage": "walking_double_support",
    "stair_speed_up": "stair_speed_up",
    "stair_speed_down": "stair_speed_down",
    # Deliberately NOT mapped, and the reasons are worth keeping so they are not
    # "fixed" later by someone reading the unmapped-blocks log as a to-do list:
    #   walking_step_length — speed ~= cadence x step length, so it is near-collinear
    #     with walking_speed and would add 12 FDR pairs to restate it.
    #   physical_effort     — redundant with active_calories + exercise_minutes, both
    #     already ingested and already in the frame.
    #   apple_stand_time    — stand_hours is the same signal in a coarser unit.
    "environmental_audio_exposure": "environmental_audio_exposure",
    "headphone_audio_exposure": "headphone_audio_exposure",
    # Apple's OWN one-minute heart-rate recovery. Mapped 2026-08-22 as an independent
    # check on `workouts.hr_recovery_60`, which this app derives itself from raw HR
    # samples (metrics_engine._hr_recovery, 192 sessions since 2025-01).
    #
    # Deliberately kept SEPARATE rather than filling in our column: Apple only emits
    # this for workouts it considers qualifying, and computes it its own undocumented
    # way, so the two are different estimators of the same quantity. Two estimators
    # that agree are corroboration; one silently overwriting the other is just a
    # number with an unknown provenance. Where they DISAGREE is the useful signal —
    # that is where our sample-window assumptions (HRR_LATE_S / HRR_NEAR_BPM / HRR_LO_S /
    # HRR_HI_S) are probably wrong. (Only 3 Apple values by 2026-10-27, all walks.)
    "cardio_recovery": "cardio_recovery",
    "flights_climbed": "flights_climbed",
    "dietary_caffeine": "caffeine",                  # ⚠ verify key (HK: DietaryCaffeine); needs manual/app logging to populate
    # Verified 2026-08-19: one row per session, value = duration in minutes, timestamp
    # = session start. Two back-to-back 5-minute sits produced two 5.0 rows. The old
    # "may need a custom path" warning was precautionary and is not borne out.
    "mindful_minutes": "mindful_minutes",
    "blood_oxygen_saturation": "blood_oxygen",       # ⚠ verify key + unit (fraction vs %); noisy in healthy adults at sea level
    "breathing_disturbances": "breathing_disturbances",  # sleep-apnea screen — only meaningful paired with blood_oxygen
    # ── Body composition added 2026-07-03 (keys confirmed present in the unmapped-block log) ──
    # Fed by a bioimpedance scale via Apple Health (~every-other-day cadence). Verify units on
    # first sync: body_fat is fraction (0.18) vs percent (18); lean mass is lb vs kg (weight is lb).
    "body_fat_percentage": "body_fat_percentage",
    "lean_body_mass": "lean_body_mass",
    "body_mass_index": "bmi",
}

# Sleep stage value → internal metric_type suffix
SLEEP_STAGE_MAP: dict[str, str] = {
    "INBED": "sleep_inbed",
    "ASLEEP": "sleep_asleep",
    "DEEP": "sleep_deep",
    "CORE": "sleep_core",
    "REM": "sleep_rem",
    "AWAKE": "sleep_awake",
}


def _parse_date(date_str: str) -> datetime:
    """Parse Health Auto Export date string to naive UTC datetime."""
    dt = datetime.strptime(date_str.strip(), "%Y-%m-%d %H:%M:%S %z")
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


async def _upsert_metric(
    db: AsyncSession,
    metric_type: str,
    timestamp: datetime,
    value: float,
    unit: str | None,
    source: str | None,
) -> bool:
    """Upsert a health_metrics row. Returns True on success.

    Was INSERT OR REPLACE, which had two problems:

    1. REPLACE DELETES the conflicting row and inserts a new one, so the row gets a NEW
       id. Anything referencing it by id — food_log.source_id on drink-linked nutrition
       rows — is silently orphaned. ON CONFLICT DO UPDATE keeps the id.
    2. It would overwrite HAND-LOGGED entries. `alcohol` is both ingested from Apple
       (alcohol_consumption) and written by the in-app logger, so an Apple sample at the
       same timestamp could replace a manual one, changing its value and its source. The
       WHERE clause makes ingestion yield to anything this app wrote.
    """
    await db.execute(
        text(
            "INSERT INTO health_metrics "
            "(metric_type, timestamp, value, unit, source) "
            "VALUES (:metric_type, :timestamp, :value, :unit, :source) "
            "ON CONFLICT(metric_type, timestamp) DO UPDATE SET "
            "  value = excluded.value, unit = excluded.unit, source = excluded.source "
            "WHERE health_metrics.source IS NOT 'wellness_app'"
        ),
        {
            "metric_type": metric_type,
            "timestamp": timestamp,
            "value": value,
            "unit": unit,
            "source": source,
        },
    )
    return True


def _extract_sample_value(sample: dict) -> float | None:
    """
    Extract the primary numeric value from a Health Auto Export sample.
    Handles multiple formats:
      - Standard quantity:  {"qty": 72.5, ...}
      - Heart rate / avg:   {"Avg": 67, "Min": 65, "Max": 70, ...}
    """
    if "qty" in sample:
        return float(sample["qty"])
    if "Avg" in sample:
        return float(sample["Avg"])
    return None


async def _ingest_scalar_metric(
    db: AsyncSession,
    metric_type: str,
    unit: str | None,
    data: list[dict],
    dates_seen: set,
) -> int:
    """Ingest a list of scalar time-series samples."""
    count = 0
    for sample in data:
        try:
            ts = _parse_date(sample["date"])
            value = _extract_sample_value(sample)
            if value is None:
                logger.warning("Skipping sample for %s — no extractable value: %s", metric_type, sample)
                continue
            await _upsert_metric(db, metric_type, ts, value, unit, "apple_health")
            dates_seen.add(to_local_date(ts))
            count += 1
        except Exception as exc:
            logger.warning("Skipping sample for %s: %s — %s", metric_type, sample, exc)
    return count


def _qty(field) -> float | None:
    """Extract a numeric value from either a scalar or {"qty": ..., "units": ...} dict."""
    if field is None:
        return None
    if isinstance(field, dict):
        return field.get("qty")
    return float(field)


async def _ingest_workouts(
    db: AsyncSession, data: list[dict], dates_seen: set
) -> int:
    """Ingest workout records, upsert by apple_uuid."""
    count = 0
    for sample in data:
        try:
            uuid = sample.get("id") or sample.get("uuid") or sample.get("UUID")
            if not uuid:
                logger.warning("Workout missing id/UUID, skipping")
                continue

            start_time = _parse_date(sample["start"])
            end_raw = sample.get("end")
            end_time = _parse_date(end_raw) if end_raw else None
            duration_sec = int(sample.get("duration", 0))

            active_calories = _qty(sample.get("activeEnergyBurned")) or _qty(sample.get("activeEnergy"))

            # Filter out low-quality segments (auto-detected warmups / noise).
            # Only skip if BOTH thresholds are below minimum — a long low-cal session
            # (e.g. yoga) or a short high-cal burst should still be recorded.
            if (active_calories is not None and active_calories < 100
                    and duration_sec < 900):
                logger.debug(
                    "Skipping low-quality workout: %s %ds %.0fkcal",
                    sample.get("name") or sample.get("activityType"), duration_sec, active_calories,
                )
                continue

            avg_hr = _qty(sample.get("avgHeartRate")) or _qty(sample.get("averageHeartRate"))
            max_hr = _qty(sample.get("maxHeartRate"))

            logger.debug("Workout sample keys: %s", list(sample.keys()))

            # HKWorkoutEffortScore (the user-logged 1–10 effort rating) is not exported
            # by Health Auto Export — it does not appear in the workout payload or any
            # metric block.  RPE must be entered manually in the app.  The CASE WHEN
            # below preserves any existing manual entry across re-syncs.
            rpe = None

            # Metabolic intensity: {'qty': X, 'units': 'kcal/hr·kg'} ≈ MET
            intensity_raw = sample.get("intensity")
            intensity_met = float(intensity_raw["qty"]) if isinstance(intensity_raw, dict) and "qty" in intensity_raw else None

            # Catch a watch left running at ingestion, so it never enters the
            # load or zone aggregates in the first place. Re-evaluated on every
            # upsert, so a corrected export clears the flag by itself.
            _suspect_reason = suspect_reason(duration_sec, start_time, end_time)
            if _suspect_reason:
                logger.warning("Flagging suspect workout %s: %s", uuid, _suspect_reason)

            await db.execute(
                text(
                    "INSERT INTO workouts "
                    "(apple_uuid, activity_type, start_time, end_time, duration_sec, "
                    " active_calories, avg_hr, max_hr, hr_samples, source, rpe, intensity_met, "
                    " suspect, suspect_reason) "
                    "VALUES (:apple_uuid, :activity_type, :start_time, :end_time, :duration_sec, "
                    "        :active_calories, :avg_hr, :max_hr, :hr_samples, :source, :rpe, :intensity_met, "
                    "        :suspect, :suspect_reason) "
                    "ON CONFLICT(apple_uuid) DO UPDATE SET "
                    "  activity_type   = excluded.activity_type, "
                    "  start_time      = excluded.start_time, "
                    "  end_time        = excluded.end_time, "
                    "  duration_sec    = excluded.duration_sec, "
                    "  active_calories = excluded.active_calories, "
                    # avg/max HR: same protection as rpe below. HAE re-exports a rolling
                    # window of past workouts, and since June 2026 its payload often omits
                    # the HR summary entirely — an unconditional assignment would let a
                    # re-sync NULL out a value that was ingested correctly the first time.
                    "  avg_hr          = CASE WHEN excluded.avg_hr IS NOT NULL THEN excluded.avg_hr ELSE workouts.avg_hr END, "
                    "  max_hr          = CASE WHEN excluded.max_hr IS NOT NULL THEN excluded.max_hr ELSE workouts.max_hr END, "
                    "  source          = excluded.source, "
                    "  intensity_met   = excluded.intensity_met, "
                    "  suspect         = excluded.suspect, "
                    "  suspect_reason  = excluded.suspect_reason, "
                    # rpe: take incoming if non-null, else preserve existing manual entry
                    "  rpe             = CASE WHEN excluded.rpe IS NOT NULL THEN excluded.rpe ELSE workouts.rpe END"
                ),
                {
                    "apple_uuid": uuid,
                    "activity_type": sample.get("name") or sample.get("activityType"),
                    "start_time": start_time,
                    "end_time": end_time,
                    "duration_sec": duration_sec,
                    "active_calories": active_calories,
                    "avg_hr": avg_hr,
                    "max_hr": max_hr,
                    "hr_samples": None,
                    "source": "apple_health",
                    "rpe": rpe,
                    "intensity_met": intensity_met,
                    "suspect": _suspect_reason is not None,
                    "suspect_reason": _suspect_reason,
                },
            )
            dates_seen.add(to_local_date(start_time))
            count += 1
        except Exception as exc:
            logger.warning("Skipping workout sample: %s — %s", sample, exc)
    return count


# A single sleep session longer than this is an aggregation artifact (see _ingest_sleep).
MAX_SLEEP_SPAN_H = 16


async def _ingest_sleep(
    db: AsyncSession, data: list[dict], dates_seen: set
) -> int:
    """
    Ingest sleep sessions from Health Auto Export.
    Each row is already a pre-summarized session with stage durations in hours.
    Upserts directly into sleep_sessions keyed on date.
    """
    count = 0
    for sample in data:
        try:
            sleep_start_raw = sample.get("sleepStart") or sample.get("inBedStart")
            sleep_end_raw = sample.get("sleepEnd") or sample.get("inBedEnd")

            bedtime = _parse_date(sleep_start_raw) if sleep_start_raw else None
            wake_time = _parse_date(sleep_end_raw) if sleep_end_raw else None

            # KEY = WAKE DATE where the user WAS (2026-10-19, revised 2026-10-20). The old
            # key, HAE's `date` field converted to UTC, broke twice: after the HAE re-setup
            # on 2026-10-01 it carried the EVENING date (every night filed a day early, so
            # the briefing never found last night), and the same session was re-sent under
            # different dates, overwriting neighbours.
            #
            # The first fix read the wake date off sleepEnd's own UTC offset. WRONG for
            # re-exports: HAE stamps EVERY timestamp with the phone's zone AT EXPORT TIME
            # (proved 2026-10-20 — a re-export from home carried the home offset on every
            # sample, nights abroad included). So the wake date comes from the
            # app's own zone timeline (timeutils.to_local_date: away segments, else home).
            hae_date = _parse_date(sample["date"]).date() if sample.get("date") else None
            if wake_time is not None:
                session_date = to_local_date(wake_time)
            else:
                session_date = hae_date
            if session_date is None:
                raise ValueError("no sleepEnd and no date")
            if hae_date != session_date:
                # Verification log for the re-keying fix — confirms what HAE's `date`
                # actually holds. Safe to drop once a few weeks look as expected.
                logger.info("Sleep key: HAE date=%r sleepStart=%r sleepEnd=%r → %s",
                            sample.get("date"), sleep_start_raw, sleep_end_raw, session_date)

            total_hours = float(sample.get("totalSleep") or 0)
            deep_hours = float(sample.get("deep") or 0)
            core_hours = float(sample.get("core") or 0)
            rem_hours = float(sample.get("rem") or 0)
            awake_hours = float(sample.get("awake") or 0)

            if total_hours < 0.5:
                continue  # ignore noise

            # Guards against HAE re-aggregation (2026-10-20). A re-export groups samples
            # into "sleep days" in the EXPORT zone, so nights spent in another zone come back
            # as composites stitched from two nights (00:30 → 00:30 next day, 24 h span) or
            # as fragments of one. With keep-the-longer, two composites overwrote real
            # nights (10-13, 10-16). Neither shape is a sleep session:
            if bedtime and wake_time and (wake_time - bedtime) > timedelta(hours=MAX_SLEEP_SPAN_H):
                logger.warning("Skipping sleep sample with a %.1f h span (%s → %s) — a "
                               "multi-night aggregate, not a session",
                               (wake_time - bedtime).total_seconds() / 3600, bedtime, wake_time)
                continue
            if bedtime and wake_time:
                container = (await db.execute(text(
                    "SELECT date, total_min FROM sleep_sessions WHERE bedtime <= :b AND "
                    "wake_time >= :w AND total_min >= :t AND date != :d LIMIT 1"),
                    {"b": bedtime, "w": wake_time, "t": int(total_hours * 60),
                     "d": session_date})).first()
                if container is not None:
                    logger.warning("Skipping sleep sample %s → %s (%.1f h): a fragment of "
                                   "the stored night %s", bedtime, wake_time, total_hours,
                                   container[0])
                    continue

            # Upsert on the wake date, but never let a SHORTER session replace a longer
            # one: HAE sometimes re-sends a partial aggregation of a night already
            # stored (2026-10-18: 270 min over the full night's 423, same wake time),
            # and a daytime nap shares the night's wake date. Provisional → final
            # revisions only grow, so keep-the-larger keeps the final one.
            await db.execute(
                text(
                    "INSERT INTO sleep_sessions "
                    "(date, bedtime, wake_time, total_min, deep_min, core_min, rem_min, awake_min, source) "
                    "VALUES (:date, :bedtime, :wake_time, :total_min, :deep_min, :core_min, :rem_min, :awake_min, :source) "
                    "ON CONFLICT(date) DO UPDATE SET "
                    "bedtime=excluded.bedtime, wake_time=excluded.wake_time, "
                    "total_min=excluded.total_min, deep_min=excluded.deep_min, "
                    "core_min=excluded.core_min, rem_min=excluded.rem_min, "
                    "awake_min=excluded.awake_min, source=excluded.source "
                    "WHERE excluded.total_min >= sleep_sessions.total_min"
                ),
                {
                    "date": session_date,
                    "bedtime": bedtime,
                    "wake_time": wake_time,
                    "total_min": int(total_hours * 60),
                    "deep_min": int(deep_hours * 60),
                    "core_min": int(core_hours * 60),
                    "rem_min": int(rem_hours * 60),
                    "awake_min": int(awake_hours * 60),
                    "source": "apple_health",
                },
            )
            dates_seen.add(session_date)
            count += 1
        except Exception as exc:
            logger.warning("Skipping sleep sample: %s — %s", sample, exc)
    return count


async def ingest_health_export(db: AsyncSession, data: list[dict], workouts: list[dict] | None = None) -> int:
    """
    Parse a Health Auto Export payload and upsert all records.
    Triggers recompute_dates for all dates that received new data.
    Returns total records processed.
    """
    total = 0
    dates_seen: set = set()
    unmapped_names: list[str] = []
    empty_names: list[str] = []

    # Ingest workouts passed separately from the metrics list
    if workouts:
        try:
            total += await _ingest_workouts(db, workouts, dates_seen)
        except Exception as exc:
            logger.error("Error ingesting workouts: %s", exc)

    for metric_block in data:
        name = metric_block.get("name", "")
        unit = metric_block.get("units")
        samples = metric_block.get("data", [])

        try:
            if name == "sleep_analysis":
                total += await _ingest_sleep(db, samples, dates_seen)
            elif name in METRIC_NAME_MAP:
                metric_type = METRIC_NAME_MAP[name]
                if not samples:
                    # Mapped, arrived, and CARRIED NOTHING. Previously invisible in
                    # both directions: it never reached the unmapped list (we do
                    # recognise the name) and it wrote no rows, so a metric that
                    # Apple has no samples for looks identical to one the exporter
                    # never sends. `cardio_recovery` is exactly this case — mapped
                    # 2026-08-22, zero rows since, and every HAE metric enabled.
                    empty_names.append(name)
                total += await _ingest_scalar_metric(db, metric_type, unit, samples, dates_seen)
            else:
                logger.debug("Unrecognised metric block '%s', skipping", name)
                unmapped_names.append(name)
        except Exception as exc:
            logger.error("Error ingesting metric block '%s': %s", name, exc)

    # One INFO line per webhook listing every block name we did NOT map — this is
    # the ground truth for adding/correcting METRIC_NAME_MAP keys without needing
    # DEBUG logging. Low-noise: one line per sync, only when something is unmapped.
    if unmapped_names:
        logger.info("Unmapped metric blocks (%d): %s", len(unmapped_names), sorted(unmapped_names))
    # Blocks we DO map that arrived carrying no samples. Distinguishes "the
    # exporter never sends this" from "Apple has no data for it" — the two look
    # identical from the outside, and only one of them is fixable here.
    if empty_names:
        logger.info("Mapped-but-empty metric blocks (%d): %s",
                    len(empty_names), sorted(empty_names))

    await db.commit()

    if dates_seen:
        sorted_dates = sorted(dates_seen)
        logger.info("Triggering recompute for %d dates: %s … %s", len(sorted_dates), sorted_dates[0], sorted_dates[-1])
        if len(sorted_dates) > BACKFILL_RECOMPUTE_THRESHOLD:
            # Reply now, recompute after. See BACKFILL_RECOMPUTE_THRESHOLD.
            logger.info("Backfill detected — recomputing in the background so the "
                        "webhook can respond before the client times out")
            asyncio.create_task(_recompute_backfill(sorted_dates))
        else:
            try:
                await recompute_dates(db, sorted_dates)
            except Exception as exc:
                logger.error("recompute_dates failed: %s", exc)

    return total
