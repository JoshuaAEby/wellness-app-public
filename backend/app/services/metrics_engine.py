"""
Compute and cache derived metrics (ATL/CTL/ACWR, HRV/RHR z-scores, readiness score)
into the daily_metrics table.
"""
import logging
import math
from datetime import date, datetime, timedelta
from statistics import mean, stdev
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DailyMetric, HealthMetric, SleepSession, UserSettings, Workout
from app.services.hr_zones import zone_bounds
from app.timeutils import local_date_sql, local_day_bounds_utc

logger = logging.getLogger(__name__)

# EWMA decay constants
ALPHA_ATL = 1 - math.exp(-1 / 7)    # 7-day EWMA
ALPHA_CTL = 1 - math.exp(-1 / 42)   # 42-day EWMA
ALPHA_BASELINE = 1 - math.exp(-1 / 30)  # 30-day EWMA for HRV/RHR baselines

# Days of training-load history before ACWR is trusted (otherwise tiny CTL → ratio blows up)
WARMUP_DAYS = 14

# Minimum recorded nights (of the 14-night window) before sleep debt is computed.
# Below this, debt is None and readiness renormalizes over the other components.
MIN_SLEEP_NIGHTS = 5


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def compute_readiness(
    hrv_z: Optional[float],
    rhr_z: Optional[float],
    sleep_debt_hours: Optional[float],
    acwr: Optional[float],
) -> tuple[Optional[float], Optional[str]]:
    """
    RECOVERY score (0-100) + label. Pure overnight-recovery construct since
    2026-07-12: HRV z 45%, RHR z 25%, sleep debt 30%. ACWR was REMOVED from the
    score — it measures load state, not recovery state, its sweet-spot evidence
    base is contested, and it was empirically wrong-signed for this user
    (higher load → better next-day energy, 2026-07-10 validation). ACWR still
    displays on Today (Workload row) and drives the training rec; the `acwr`
    arg is kept for signature stability. The heuristic weights are
    pre-registered for empirical refit at n≥150 finer-grained check-ins vs
    morning energy + evening day score (adopt only if better out-of-sample;
    candidates and decision rule fixed in docs/READINESS_REFIT.md, 2026-11-01).

    MISSING DATA (added 2026-07-30 after a dead-watch gap exposed the old bug):
    every component is INDIVIDUALLY DROPPABLE — a None input is excluded and the
    remaining weights are renormalized, NOT coerced to z=0 (which the old
    `(hrv_z or 0)` did, silently scoring a missing marker as a perfectly average
    day and fabricating ~70% of the score on watch-dead days).

    Suppression rule: if BOTH HRV and RHR are missing, return (None, None). A
    score driven only by sleep debt is not a recovery score, and presenting one
    as such — or letting it define readiness strata in correlations.py — would
    misrepresent the day. Callers must treat None as "insufficient data" and
    store NULL, so those days are naturally excluded downstream.
    """
    parts = []
    if hrv_z is not None:
        parts.append((0.45, _clip((hrv_z + 2) / 4 * 100, 0, 100)))
    if rhr_z is not None:
        parts.append((0.25, _clip((-rhr_z + 2) / 4 * 100, 0, 100)))
    if sleep_debt_hours is not None:
        parts.append((0.30, _clip(100 - (sleep_debt_hours / 2) * 100, 0, 100)))

    # Both autonomic markers absent → not a recovery score. Suppress.
    if hrv_z is None and rhr_z is None:
        return None, None

    score = sum(w * c for w, c in parts) / sum(w for w, _ in parts)

    if score >= 75:
        label = "High"
    elif score >= 50:
        label = "Moderate"
    elif score >= 25:
        label = "Low"
    else:
        label = "Rest Day"

    return round(score, 1), label


async def _get_settings(db: AsyncSession) -> UserSettings:
    result = await db.execute(select(UserSettings).limit(1))
    row = result.scalar_one_or_none()
    if row is None:
        row = UserSettings()
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


async def _get_prior_daily(db: AsyncSession, before_date: date) -> Optional[DailyMetric]:
    result = await db.execute(
        select(DailyMetric)
        .where(DailyMetric.date < before_date)
        .order_by(DailyMetric.date.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _daily_avg_metric(
    db: AsyncSession, metric_type: str, target_date: date
) -> Optional[float]:
    """Return the average value for a metric_type on a given LOCAL calendar date."""
    day_start, day_end = local_day_bounds_utc(target_date)
    result = await db.execute(
        select(func.avg(HealthMetric.value))
        .where(HealthMetric.metric_type == metric_type)
        .where(HealthMetric.timestamp >= day_start)
        .where(HealthMetric.timestamp < day_end)
    )
    return result.scalar_one_or_none()


async def _recent_daily_values(
    db: AsyncSession, metric_type: str, before_date: date, days: int,
    skip: frozenset = frozenset(),
) -> list[float]:
    """Return up to `days` most recent daily-average values for metric_type, oldest first.

    Days are LOCAL calendar days; the window includes before_date itself (matching
    the previous behaviour, where the z-score series contains today's value).
    `skip`: local dates to leave out of the series (see _baseline_skip_days).
    """
    since = before_date - timedelta(days=days)
    day_col = local_date_sql(HealthMetric.timestamp)
    result = await db.execute(
        select(
            day_col.label("day"),
            func.avg(HealthMetric.value).label("avg_val"),
        )
        .where(HealthMetric.metric_type == metric_type)
        .where(HealthMetric.timestamp >= local_day_bounds_utc(since)[0])
        .where(HealthMetric.timestamp < local_day_bounds_utc(before_date)[1])
        .group_by(day_col)
        .order_by(day_col)
    )
    rows = result.all()
    skip_s = {d.isoformat() for d in skip}
    return [r.avg_val for r in rows if r.avg_val is not None and str(r.day) not in skip_s]


async def _baseline_skip_days(db: AsyncSession, target_date: date, days: int = 30) -> frozenset:
    """Travel + post-trip recovery days in the baseline window, EXCEPT target_date.

    Added 2026-10-19 after a two-week trip: jet-lag nights (HRV down, RHR up) sat in
    the 30-day baseline for a month afterwards, so the first weeks home were scored
    against a depressed reference and read as unusually good. The day itself is
    still z-scored — only its contribution to OTHER days' baselines is removed.
    Sick days are deliberately not skipped (unchanged behaviour; a separate question).
    """
    from app.models import DayFlag
    from app.services.day_flags import AWAY_REASON, RECOVERY_REASON
    rows = (await db.execute(
        select(DayFlag.date)
        .where(DayFlag.date >= target_date - timedelta(days=days))
        .where(DayFlag.date < target_date)
        .where(DayFlag.reason.in_((AWAY_REASON, RECOVERY_REASON)))
    )).all()
    return frozenset(r[0] for r in rows)


# Nightly HRV aggregation (2026-10-28, note 55 audit). Apple writes only ~3–5 SDNN spot
# readings per night (325 normal nights: 280 had 3–4), and one morning's call (HRV
# z ≈ +2.8, "strongly favor training") came from TWO of them before the rest counted; the
# settled night was unremarkable. So:
#   * at least HRV_NIGHT_MIN_SAMPLES readings, or the night has no HRV (readiness drops the
#     component and renormalises; a later recompute fills it in) — historically this drops
#     2 of 325 nights;
#   * GEOMETRIC mean (mean of ln SDNN): HRV is ~log-normal and one high reading should not
#     carry a 4-sample night. On 365 d it beat the arithmetic mean on every check — lag-1
#     persistence +0.12 vs +0.08, alcohol → HRV −0.21 vs −0.18, load −0.11 vs −0.09 (small,
#     same direction throughout); median was worse on all three. History moved little:
#     median |Δz| 0.13, one day > 1 z.
HRV_NIGHT_MIN_SAMPLES = 3


def _night_hrv(vals: list[float]) -> Optional[float]:
    vals = [v for v in vals if v is not None and v > 0]
    if len(vals) < HRV_NIGHT_MIN_SAMPLES:
        return None
    return math.exp(mean(math.log(v) for v in vals))


async def _sleep_window_hrv(
    db: AsyncSession, start: date, end: date
) -> dict[date, float]:
    """Geometric-mean hrv_sdnn inside each night's ACTUAL sleep window, start..end inclusive
    (≥ HRV_NIGHT_MIN_SAMPLES readings — see _night_hrv).

    HRV is a recovery marker, so it has to be measured over a fixed physiological
    window rather than the calendar day. Averaging every sample in a local day
    mixes overnight parasympathetic values with daytime ones (overnight ran
    ~1.35x higher) — so the day's mean depended on how many daytime
    samples had landed by the time the metric was computed. The morning pipeline
    scored each day off the ~4 overnight samples, then the value fell over the next
    week as the other ~7 arrived: snapshot-vs-settled readiness drifted on 30 of 40
    experiment days, by up to 46 points (diagnosed 2026-08-30). Sleeping HRV is also
    the standard construct (Whoop/Oura/Garmin).

    Days with a sleep session but no sample inside it are OMITTED, not backfilled
    from the daily average — mixing the two constructs in one series is precisely
    the defect this replaces. A missing HRV is already handled downstream:
    compute_readiness drops the component and renormalizes the remaining weights.
    """
    sessions = (await db.execute(
        select(SleepSession.date, SleepSession.bedtime, SleepSession.wake_time)
        .where(SleepSession.date >= start)
        .where(SleepSession.date <= end)
        .where(SleepSession.bedtime.isnot(None))
        .where(SleepSession.wake_time.isnot(None))
    )).all()
    if not sessions:
        return {}

    samples = (await db.execute(
        select(HealthMetric.timestamp, HealthMetric.value)
        .where(HealthMetric.metric_type == "hrv_sdnn")
        .where(HealthMetric.timestamp >= min(s.bedtime for s in sessions))
        .where(HealthMetric.timestamp <= max(s.wake_time for s in sessions))
        .order_by(HealthMetric.timestamp)
    )).all()
    if not samples:
        return {}

    out: dict[date, float] = {}
    for s in sessions:
        v = _night_hrv([r.value for r in samples if s.bedtime <= r.timestamp <= s.wake_time])
        if v is not None:
            out[s.date] = v
    return out


# Overnight RHR (note 90, 2026-10-27): the 10th percentile of heart rate inside each
# night's sleep window — the low, sustained sleeping HR. Replaced Apple's daily
# `resting_heart_rate` as readiness's RHR input because Apple's is computed from the WHOLE
# day and written late: it was missing at the morning briefing on 17 of 23 days (and the
# morning value, when present, was later revised), and over 326 normal nights it showed NO
# response to the previous evening's alcohol (rho +0.05) where every overnight measure did
# (p10 +0.36). p10 had Apple's stability (day-to-day 3.7 bpm, SD 4.0) and less overlap with
# sleep_hr_elevation than the overnight mean (whose alcohol response is partly that).
RHR_NIGHT_PCTL = 0.10
RHR_NIGHT_MIN_SAMPLES = 20


async def _sleep_window_rhr(db: AsyncSession, start: date, end: date) -> dict[date, float]:
    """10th-percentile HR inside each night's actual sleep window, for start..end inclusive.

    Same rules as _sleep_window_hrv: keyed by the session's (wake) date; nights with too
    few samples are OMITTED rather than filled from Apple's daily value — mixing the two
    constructs in one series is the defect this replaces.
    """
    sessions = (await db.execute(
        select(SleepSession.date, SleepSession.bedtime, SleepSession.wake_time)
        .where(SleepSession.date >= start)
        .where(SleepSession.date <= end)
        .where(SleepSession.bedtime.isnot(None))
        .where(SleepSession.wake_time.isnot(None))
    )).all()
    if not sessions:
        return {}
    # One indexed range read PER NIGHT, not one read of the whole span: HR is dense all day
    # (~1,000 samples), so reading 31 days to use 31 nights made the full-history recompute
    # 705 s instead of ~40 s.
    out: dict[date, float] = {}
    for s in sessions:
        vals = sorted(v for (v,) in (await db.execute(
            select(HealthMetric.value)
            .where(HealthMetric.metric_type == "heart_rate")
            .where(HealthMetric.timestamp >= s.bedtime)
            .where(HealthMetric.timestamp <= s.wake_time)
        )).all() if v is not None)
        if len(vals) >= RHR_NIGHT_MIN_SAMPLES:
            out[s.date] = vals[max(0, int(RHR_NIGHT_PCTL * len(vals)) - 1)]
    return out


async def _apple_rhr_baseline(db: AsyncSession, d: date) -> Optional[float]:
    """Apple-RHR EWMA baseline as of day `d` — EXACTLY what daily_metrics.rhr_baseline held
    before note 90, recomputed here because TRIMP's resting HR depends on it.

    The baseline column now holds the OVERNIGHT construct (~8 bpm lower), and TRIMP read
    the prior row's baseline as its resting HR — switching it would have silently rescaled
    every workout's load, ACWR, ATL and CTL. Training load keeps Apple's daytime RHR.
    """
    val = await _daily_avg_metric(db, "resting_heart_rate", d)
    if val is None:
        return None
    series = await _recent_daily_values(db, "resting_heart_rate", d, 30,
                                        skip=await _baseline_skip_days(db, d))
    return _ewma_from_series(series, ALPHA_BASELINE) if series else val


def _ewma_from_series(values: list[float], alpha: float) -> float:
    """Compute EWMA from a chronological series. Returns last value."""
    if not values:
        return 0.0
    ewma = values[0]
    for v in values[1:]:
        ewma = alpha * v + (1 - alpha) * ewma
    return ewma


def _z_score(value: float, series: list[float]) -> Optional[float]:
    """Compute z-score of value against the given series."""
    if len(series) < 2:
        return None
    try:
        mu = mean(series)
        sd = stdev(series)
        if sd == 0:
            return 0.0
        return (value - mu) / sd
    except Exception:
        return None


async def recompute_dates(db: AsyncSession, dates: list[date]) -> None:
    """
    Recompute and upsert daily_metrics for each date in `dates` (chronological order).
    Processes in order so EWMA state can cascade from one day to the next.
    """
    settings = await _get_settings(db)

    for target_date in sorted(dates):
        try:
            await _recompute_one(db, target_date, settings)
        except Exception as exc:
            logger.error("Failed to recompute metrics for %s: %s", target_date, exc)


# HR recovery is measured from the END OF EFFORT, not from when the workout is stopped
# (note 87, 2026-10-27). The user's classes end with 2-3 min of stretching before he stops
# the watch: the last hard effort comes a median 201 s before the stop, so the old
# "max of the last 120 s before stop" sat mid-stretch, partway down the descent, and
# "60 s after the stop" was really ~3 min after the effort (10-24 read 40 bpm; the true
# 60-s drop was ~25). Now:
#   late peak  = max HR in the final HRR_LATE_S before the stop (covers the stretch)
#   end of effort = the LAST sample still within HRR_NEAR_BPM of that late peak — not
#                 the peak itself: on 10-21 HR stayed 156-167 for 2 min after a 168
#                 spike (still working) and anchoring on the spike read 10 instead of 33
#   HRR60      = HR at end of effort − the LOWEST HR 50-70 s later (a window, because
#                sampling is irregular). That window now falls mostly INSIDE the workout,
#                where the watch samples every few seconds.
#   no clear hard finish (late peak < HRR_MIN_FRAC of the session max — most walks) →
#                None, not a misleading number.
# Over 99 gym sessions (2025-06..2026-10): session-to-session |Δ| 13.7 → 9.2 bpm, SD
# 13.1 → 9.1, 95 still measurable; NEAR_BPM 5/8/10 gave near-identical results. Apple's
# own `cardio_recovery` could not adjudicate — only 3 values, all walks.
HRR_LATE_S = 300
HRR_NEAR_BPM = 8
HRR_MIN_FRAC = 0.85
HRR_LO_S, HRR_HI_S = 50, 70


async def _hr_recovery(db: AsyncSession, w) -> tuple[Optional[float], Optional[float]]:
    """(hr_at_end_of_effort, hrr60) for a finished workout, or (None, None).

    See the HRR_* block above for the method and why it moved off the stop time. A
    NEGATIVE value is kept, not discarded: HR genuinely rises after some efforts (stairs,
    heat), and silently dropping those would bias the series toward good recoveries.
    `hr_peak` now holds the HR the recovery is measured FROM (the end-of-effort sample),
    which is what the recovery card's HRR-vs-peak fit needs.
    """
    if not w.end_time or not w.start_time:
        return None, None
    session_max = (await db.execute(
        select(func.max(HealthMetric.value))
        .where(HealthMetric.metric_type == "heart_rate")
        .where(HealthMetric.timestamp >= w.start_time)
        .where(HealthMetric.timestamp <= w.end_time)
    )).scalar()
    if session_max is None:
        return None, None
    rows = (await db.execute(
        select(HealthMetric.timestamp, HealthMetric.value)
        .where(HealthMetric.metric_type == "heart_rate")
        .where(HealthMetric.timestamp > w.end_time - timedelta(seconds=HRR_LATE_S))
        .where(HealthMetric.timestamp <= w.end_time + timedelta(seconds=HRR_HI_S))
        .order_by(HealthMetric.timestamp)
    )).all()
    late = [(t, v) for t, v in rows if t <= w.end_time]
    if not late:
        return None, None
    late_peak = max(v for _, v in late)
    if late_peak < HRR_MIN_FRAC * session_max:
        return None, None
    t_end, v_end = [(t, v) for t, v in late if v >= late_peak - HRR_NEAR_BPM][-1]
    after = [v for t, v in rows
             if t_end + timedelta(seconds=HRR_LO_S) < t <= t_end + timedelta(seconds=HRR_HI_S)]
    if not after:
        return round(v_end, 1), None
    return round(v_end, 1), round(v_end - min(after), 1)


async def _workout_metrics(
    db: AsyncSession, w, hr_rest: float, hr_max: float, sex, zbounds
) -> tuple[float, Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]:
    """
    Returns (trimp, zone1_min, zone2_min, zone3_min, high_intensity_min) from a
    single HR query.

    Zone boundaries come from services/hr_zones (Karvonen %HRR at 60/70/80/90),
    derived from settings.max_hr and the FROZEN settings.hrr_rest_ref — one source of
    truth shared with the training_zones endpoint. They were hardcoded absolute bpm
    (115/135/155) until 2026-08-08, which for this athlete ran 1-2 zones low.

    Still NOT Apple's zones: Apple's own user-configurable %-based zone data is not
    ingested at all; this is computed from the raw `heart_rate` samples, which is what
    makes it stable, transparent and retro-recomputable.

    Z1 was added 2026-08-06. Before it, everything under 115 bpm fell through the
    if/elif with nowhere to go: ~35% of HR-tracked workout time (and ~100% of walks)
    belonged to no zone, so the minutes never summed to the session duration. With
    Z1 the four buckets partition the session, modulo the 30 s gap cap.

    Zone minutes are None when no sample-level HR series is available.
    TRIMP falls back to the avg-HR formula in that case.
    Inter-sample gaps are capped at 30 s to avoid inflating time during Apple Watch pauses.
    """
    from app.services.training_load import trimp_from_samples, trimp_from_avg

    if w.start_time and w.end_time:
        rows = (await db.execute(
            select(HealthMetric.timestamp, HealthMetric.value)
            .where(HealthMetric.metric_type == "heart_rate")
            .where(HealthMetric.timestamp >= w.start_time)
            .where(HealthMetric.timestamp <= w.end_time)
            .order_by(HealthMetric.timestamp)
        )).all()
        if len(rows) >= 2:
            samples = [(r.timestamp, r.value) for r in rows]
            # Backfill the workout's HR SUMMARY from the samples when the export
            # omitted it. Health Auto Export stopped sending avgHeartRate/maxHeartRate
            # in June 2026 (0% missing through May, then 28% / 71% / 67% Jun-Aug), and
            # it drops both together. TRIMP never depended on them — it is computed
            # from these very samples — but the fields are read directly by the widget,
            # the workout list and the AI summaries, where a NULL reads as "no HR".
            # Derived here rather than at ingestion because the samples and the workout
            # can arrive in different payloads, so at insert time they may not exist yet.
            if w.avg_hr is None:
                w.avg_hr = round(sum(v for _, v in samples) / len(samples), 1)
            if w.max_hr is None:
                w.max_hr = max(v for _, v in samples)
            trimp = trimp_from_samples(samples, hr_rest, hr_max, sex)
            z2_start, z3_start, z4_start, z5_start = zbounds
            z1_secs = z2_secs = z3_secs = z4_secs = z5_secs = 0.0
            for i in range(len(rows) - 1):
                bpm = rows[i].value
                dt = min((rows[i + 1].timestamp - rows[i].timestamp).total_seconds(), 30.0)
                if bpm < z2_start:
                    z1_secs += dt
                elif bpm < z3_start:
                    z2_secs += dt
                elif bpm < z4_start:
                    z3_secs += dt
                elif bpm < z5_start:
                    z4_secs += dt           # threshold work
                else:
                    z5_secs += dt           # VO2max work
            return (trimp, z1_secs / 60.0, z2_secs / 60.0, z3_secs / 60.0,
                    z4_secs / 60.0, z5_secs / 60.0)

    # Fallback: TRIMP from avg HR; zone minutes unknown without sample-level HR data
    if w.avg_hr and w.duration_sec:
        return trimp_from_avg(w.avg_hr, w.duration_sec / 60.0, hr_rest, hr_max, sex), None, None, None, None, None
    return 0.0, None, None, None, None, None


async def _first_load_date(db: AsyncSession) -> Optional[date]:
    """Earliest date that carries any training load — used to warm up ACWR."""
    r = await db.execute(select(func.min(DailyMetric.date)).where(DailyMetric.daily_load > 0))
    return r.scalar_one_or_none()


async def _recompute_one(db: AsyncSession, target_date: date, settings: UserSettings) -> None:
    target_sleep_hours = settings.target_sleep_hours
    hr_max = settings.max_hr or 185

    # 1. Daily load = Σ per-workout Banister TRIMP (HR-based internal load).
    #    TRIMP is the single consistent currency for ATL/CTL/ACWR. Per-sample integration
    #    from the intra-workout HR series (fallback to avg-HR). RPE is NOT summed into load
    #    (kept as a displayed signal only) — measured RPE coverage was ~23%, too sparse to
    #    be the consistent metric the ACWR ratio requires. See services/training_load.py.
    day_start, day_end = local_day_bounds_utc(target_date)
    workouts_today = (await db.execute(
        select(Workout)
        .where(Workout.start_time >= day_start)
        .where(Workout.start_time < day_end)
    )).scalars().all()

    # 2. ATL / CTL — seed from prior row. Resting HR for HR-reserve: explicit override,
    #    else the most recent rolling baseline, else 60.
    prior = await _get_prior_daily(db, target_date)
    # Apple-RHR baseline as of the prior row's day — the value rhr_baseline used to hold
    # (see _apple_rhr_baseline: that column is the overnight construct since note 90).
    apple_base = await _apple_rhr_baseline(db, prior.date) if prior else None
    hr_rest = settings.resting_hr_override or apple_base or 60.0

    # Zone edges from the FROZEN %HRR reference — never the rolling rhr_baseline
    # (which is what hr_rest is), or the zone definitions would drift day to day.
    zbounds = zone_bounds(hr_max, settings.hrr_rest_ref)

    daily_load = 0.0
    for w in workouts_today:
        # A watch left running is not training load. Its metrics are still computed
        # and stored (so the row stays inspectable and the flag stays reversible),
        # but it contributes nothing to daily_load. See services/workout_qc.
        if w.suspect:
            continue
        trimp, z1_min, z2_min, z3_min, z4_min, z5_min = await _workout_metrics(
            db, w, hr_rest, hr_max, settings.biological_sex, zbounds
        )
        hi_min = None if (z4_min is None and z5_min is None) else (z4_min or 0.0) + (z5_min or 0.0)
        w.trimp = round(trimp, 2)
        w.zone1_min = round(z1_min, 2) if z1_min is not None else None
        w.zone2_min = round(z2_min, 2) if z2_min is not None else None
        w.zone3_min = round(z3_min, 2) if z3_min is not None else None
        w.zone4_min = round(z4_min, 2) if z4_min is not None else None
        w.zone5_min = round(z5_min, 2) if z5_min is not None else None
        w.high_intensity_min = round(hi_min, 2) if hi_min is not None else None

        # HR recovery — computed here so a recompute backfills the whole history.
        w.hr_peak, w.hr_recovery_60 = await _hr_recovery(db, w)

        daily_load += trimp

    # 7-day rolling zone percentages. Only workouts with an HR series contribute
    # (zone2_min IS NOT NULL as the proxy).
    #
    # CAVEAT the old comment got wrong: this filter excludes activities with NO heart
    # rate, but a WALK has one — it just sits under 115 bpm, so it used to score 0.0
    # in every bucket while still adding its full duration to the denominator, pulling
    # every percentage down. Now that Z1 exists that time is attributed instead of
    # vanishing, and z1+z2+z3+hi sums to ~100% of HR-tracked duration.
    window_start = local_day_bounds_utc(target_date - timedelta(days=6))[0]
    zone_row = (await db.execute(
        select(
            func.sum(Workout.zone1_min).label("z1"),
            func.sum(Workout.zone2_min).label("z2"),
            func.sum(Workout.zone3_min).label("z3"),
            func.sum(Workout.zone4_min).label("z4"),
            func.sum(Workout.zone5_min).label("z5"),
            func.sum(Workout.high_intensity_min).label("hi"),
            func.sum(Workout.duration_sec).label("total_sec"),
        )
        .where(Workout.start_time >= window_start)
        .where(Workout.start_time < day_end)
        .where(Workout.zone2_min.isnot(None))   # proxy for "has HR series"
        .where(Workout.duration_sec.isnot(None))
        .where(Workout.duration_sec > 0)
        # Excluded here too, and this is the one that mattered most: a watch left
        # running contributed 381 Zone 1 minutes of RESTING heart rate, which the
        # distribution counted as training time for a fortnight.
        .where((Workout.suspect.is_(None)) | (Workout.suspect == False))  # noqa: E712
    )).one()

    # Denominator is the sum of the zone minutes themselves — i.e. MEASURED HR time —
    # not duration_sec. Fixed 2026-08-06.
    #
    # duration_sec is ACTIVE duration (pauses excluded) while the zone minutes accrue
    # over the wall-clock span start_time..end_time, so on a paused workout the
    # numerator could exceed the denominator: a 29.8-min walk reported 40.0 tracked
    # minutes, i.e. >100%. Dividing by measured time makes the four zones a true
    # distribution summing to exactly 100%, which is what a zone breakdown should be.
    #
    # The time this drops is time we have no HR evidence for (pauses, dropped samples
    # capped by the 30 s gap rule). Excluding it is honest: we are not claiming the
    # athlete was in Zone 1 during a gap, we are declining to guess. `hr_coverage`
    # below reports how much of the session that was, so the omission is visible
    # rather than silent.
    measured_min = sum(v or 0.0 for v in (zone_row.z1, zone_row.z2, zone_row.z3, zone_row.hi))

    def _zone_pct(zone_min) -> Optional[float]:
        if measured_min <= 0:
            return None
        return round((zone_min or 0.0) / measured_min * 100, 1)

    total_sec = zone_row.total_sec
    zone1_pct_7d = _zone_pct(zone_row.z1)
    zone2_pct_7d = _zone_pct(zone_row.z2)
    zone3_pct_7d = _zone_pct(zone_row.z3)
    zone4_pct_7d = _zone_pct(zone_row.z4)
    zone5_pct_7d = _zone_pct(zone_row.z5)
    high_intensity_pct_7d = _zone_pct(zone_row.hi)
    # Share of logged training time that produced usable HR samples. Low values mean
    # the zone split is drawn from a thin slice of the session and should be trusted
    # less — a 21.4-min walk once yielded only 4.4 min of measured HR.
    hr_coverage_pct_7d = (
        round(measured_min / (total_sec / 60.0) * 100, 1)
        if total_sec and total_sec > 0 else None
    )

    prev_atl = prior.atl if prior and prior.atl is not None else 0.0
    prev_ctl = prior.ctl if prior and prior.ctl is not None else 0.0

    atl = ALPHA_ATL * daily_load + (1 - ALPHA_ATL) * prev_atl
    ctl = ALPHA_CTL * daily_load + (1 - ALPHA_CTL) * prev_ctl
    # ACWR: gate on history length, not CTL magnitude. TRIMP's smaller numeric scale means
    # CTL crosses any fixed floor within a day or two, which would make early-history ACWR
    # explode (ATL/CTL ≈ α_atl/α_ctl ≈ 5.7 on the first big day). Stay neutral until
    # WARMUP_DAYS of load history have accrued.
    #
    # Store None — NOT 1.0 — when the gate fails. 1.0 is the "perfectly balanced"
    # value, so pinning it fabricates a real reading out of missing data: the same
    # bug class as the old `(hrv_z or 0)` in compute_readiness, which scored a dead
    # watch as a perfectly average day. It mattered: 1,343 of 1,909 stored rows
    # (70%) were exactly 1.000 — every day before load history existed — which fed
    # the correlation engine a variable that is constant over most of its range and
    # made /today report ACWR as present on days it could not be computed.
    first_load = await _first_load_date(db)
    if ctl > 0 and first_load is not None and (target_date - first_load).days >= WARMUP_DAYS:
        acwr = atl / ctl
    else:
        acwr = None

    # 4. HRV — sleep-window mean, NOT the calendar-day average (see _sleep_window_hrv).
    # Value and baseline series are built from the same window so the z-score compares
    # like with like; the old code z-scored a part-day mean against full-day baselines.
    baseline_skip = await _baseline_skip_days(db, target_date)
    hrv_by_day = await _sleep_window_hrv(db, target_date - timedelta(days=30), target_date)
    hrv_value = hrv_by_day.get(target_date)
    hrv_series = [hrv_by_day[d] for d in sorted(hrv_by_day) if d not in baseline_skip]
    hrv_baseline: Optional[float] = None
    hrv_z: Optional[float] = None
    if hrv_value is not None:
        if hrv_series:
            hrv_baseline = _ewma_from_series(hrv_series, ALPHA_BASELINE)
            hrv_z = _z_score(hrv_value, hrv_series)
        else:
            hrv_baseline = hrv_value
            hrv_z = 0.0

    # 5. RHR — OVERNIGHT 10th-percentile HR (note 90), built like HRV: value and baseline
    # from the same sleep-window construct, travel/recovery nights out of the baseline.
    rhr_by_day = await _sleep_window_rhr(db, target_date - timedelta(days=30), target_date)
    rhr_value = rhr_by_day.get(target_date)
    rhr_series = [rhr_by_day[d] for d in sorted(rhr_by_day) if d not in baseline_skip]
    rhr_baseline: Optional[float] = None
    rhr_z: Optional[float] = None
    if rhr_value is not None:
        if rhr_series:
            rhr_baseline = _ewma_from_series(rhr_series, ALPHA_BASELINE)
            rhr_z = _z_score(rhr_value, rhr_series)
        else:
            rhr_baseline = rhr_value
            rhr_z = 0.0

    # 6. Sleep debt over prior 7 nights
    sleep_debt = await _compute_sleep_debt(db, target_date, target_sleep_hours)

    # 7. Wrist temperature deviation from 30-day baseline
    wrist_temp = await _daily_avg_metric(db, "wrist_temperature", target_date)
    wrist_temp_dev: Optional[float] = None
    if wrist_temp is not None:
        wrist_series = await _recent_daily_values(db, "wrist_temperature", target_date, 30,
                                                  skip=baseline_skip)
        if wrist_series:
            baseline = mean(wrist_series)
            wrist_temp_dev = wrist_temp - baseline

    # 8. Readiness score
    readiness_score, readiness_label = compute_readiness(hrv_z, rhr_z, sleep_debt, acwr)

    # 9. Upsert into daily_metrics
    stmt = sqlite_insert(DailyMetric).values(
        date=target_date,
        daily_load=daily_load,
        atl=round(atl, 2),
        ctl=round(ctl, 2),
        acwr=round(acwr, 3) if acwr is not None else None,
        hrv_value=hrv_value,
        hrv_baseline=hrv_baseline,
        hrv_z_score=hrv_z,
        rhr_value=rhr_value,
        rhr_baseline=rhr_baseline,
        rhr_z_score=rhr_z,
        sleep_debt_hours=sleep_debt,
        wrist_temp_deviation=wrist_temp_dev,
        zone1_pct_7d=zone1_pct_7d,
        hr_coverage_pct_7d=hr_coverage_pct_7d,
        zone2_pct_7d=zone2_pct_7d,
        zone3_pct_7d=zone3_pct_7d,
        zone4_pct_7d=zone4_pct_7d,
        zone5_pct_7d=zone5_pct_7d,
        high_intensity_pct_7d=high_intensity_pct_7d,
        readiness_score=readiness_score,
        readiness_label=readiness_label,
        computed_at=datetime.utcnow(),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["date"],
        set_={
            "daily_load": daily_load,
            "atl": round(atl, 2),
            "ctl": round(ctl, 2),
            "acwr": round(acwr, 3) if acwr is not None else None,
            "hrv_value": hrv_value,
            "hrv_baseline": hrv_baseline,
            "hrv_z_score": hrv_z,
            "rhr_value": rhr_value,
            "rhr_baseline": rhr_baseline,
            "rhr_z_score": rhr_z,
            "sleep_debt_hours": sleep_debt,
            "wrist_temp_deviation": wrist_temp_dev,
            "zone1_pct_7d": zone1_pct_7d,
            "hr_coverage_pct_7d": hr_coverage_pct_7d,
            "zone2_pct_7d": zone2_pct_7d,
            "zone3_pct_7d": zone3_pct_7d,
            "zone4_pct_7d": zone4_pct_7d,
            "zone5_pct_7d": zone5_pct_7d,
            "high_intensity_pct_7d": high_intensity_pct_7d,
            "readiness_score": readiness_score,
            "readiness_label": readiness_label,
            "computed_at": datetime.utcnow(),
        },
    )
    await db.execute(stmt)
    await db.commit()
    logger.debug("Recomputed metrics for %s: load=%.0f atl=%.1f ctl=%.1f readiness=%.0f (%s)",
                 target_date, daily_load, atl, ctl, readiness_score, readiness_label)


async def _compute_sleep_debt(
    db: AsyncSession, target_date: date, target_hours: float
) -> Optional[float]:
    """
    Average nightly sleep deficit over the RECORDED nights of the prior 14.

    Uses a 14-day window based on sleep literature (Walker, Czeisler, Oura methodology):
    acute debt accumulates over ~2 weeks before becoming chronic. Averaging rather than
    summing keeps the value interpretable and bounded:
      0.0  = meeting target on average
      0.5  = half hour short per night on average
      1.0+ = consistently an hour+ short

    Missing nights are EXCLUDED from the average (they are missing data, not zero
    sleep — the old full-deficit policy imputed worst-case values that were
    missing-not-at-random and biased readiness down through gaps in watch wear).
    Fewer than MIN_SLEEP_NIGHTS recorded nights returns None ("can't judge");
    compute_readiness then renormalizes the remaining component weights.
    """
    since = target_date - timedelta(days=14)
    result = await db.execute(
        select(SleepSession)
        .where(SleepSession.date >= since)
        .where(SleepSession.date < target_date)
        .where(SleepSession.total_min.isnot(None))
    )
    sessions = result.scalars().all()
    if len(sessions) < MIN_SLEEP_NIGHTS:
        return None

    deficits = [max(0.0, target_hours - s.total_min / 60.0) for s in sessions]
    return round(sum(deficits) / len(deficits), 2)
