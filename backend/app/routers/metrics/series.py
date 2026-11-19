"""Time series for charts: daily metrics, raw trends, sleep, events, screen time, weather,
monthly history, day-of-week, daylight timing, audio exposure.

Part of the /api/metrics package (split out of routers/metrics.py 2026-10-21)."""
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import DailyMetric, HealthMetric, ScreentimeEvent, SleepSession, Workout
from app.timeutils import local_date_sql, local_day_bounds_utc, to_local_date, local_today, to_local, local_food_date_sql
from app.routers.metrics._common import _DOW_OUTCOME_LABELS, _group_stats, _offset_min, _outcome_points, _serialize_daily

router = APIRouter()


@router.get("/screentime")
async def get_screentime_range(
    start: date = Query(default_factory=lambda: local_today() - timedelta(days=30)),
    end: date = Query(default_factory=date.today),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Per-day screen-time minutes by category and device, for the trend charts.
    Buckets by LOCAL day. Returns one row per day with `phone`/`mac` category
    dicts plus per-device totals. Phone is the headline; Mac is included so the
    UI can optionally fold it in."""
    rows = (await db.execute(
        select(
            local_date_sql(ScreentimeEvent.start_time).label("d"),
            ScreentimeEvent.device,
            ScreentimeEvent.category,
            func.sum(ScreentimeEvent.duration_sec),
        )
        .where(local_date_sql(ScreentimeEvent.start_time) >= start.isoformat())
        .where(local_date_sql(ScreentimeEvent.start_time) <= end.isoformat())
        .group_by("d", ScreentimeEvent.device, ScreentimeEvent.category)
    )).all()

    days: dict[str, dict] = {}
    for d, device, category, sec in rows:
        entry = days.setdefault(d, {"date": d, "phone": {}, "mac": {}})
        key = "phone" if device == "iPhone" else "mac"
        cat = (category or "Other").lower()
        entry[key][cat] = entry[key].get(cat, 0) + round((sec or 0) / 60)

    out = []
    for d in sorted(days):
        e = days[d]
        e["phone_total"] = sum(e["phone"].values())
        e["mac_total"] = sum(e["mac"].values())
        out.append(e)
    return out


@router.get("/weather")
async def get_weather(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Current outdoor conditions + air quality for the Today card.

    null ONLY when WEATHER_LAT/LON aren't configured — then the strip correctly
    doesn't render. A configured-but-failed fetch used to return the same null, so
    an outage looked exactly like the feature being switched off (note 53); it now
    returns `available: false` with a reason, and the strip says so."""
    from app.services.weather import get_current, is_configured
    snap = await get_current(db)
    if snap is None and is_configured():
        return {"available": False, "reason": "the Open-Meteo fetch failed"}
    return snap


@router.get("/screentime/apps")
async def get_screentime_apps(
    start: date = Query(default_factory=lambda: local_today() - timedelta(days=30)),
    end: date = Query(default_factory=date.today),
    category: Optional[str] = None,
    device: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Per-app screen-time totals over a range, optionally filtered by category
    and/or device — powers the category drill-down table. Sorted desc by minutes."""
    total = func.sum(ScreentimeEvent.duration_sec)
    q = (
        select(
            ScreentimeEvent.app,
            ScreentimeEvent.category,
            ScreentimeEvent.device,
            total.label("secs"),
        )
        .where(local_date_sql(ScreentimeEvent.start_time) >= start.isoformat())
        .where(local_date_sql(ScreentimeEvent.start_time) <= end.isoformat())
        .group_by(ScreentimeEvent.app, ScreentimeEvent.category, ScreentimeEvent.device)
        .order_by(total.desc())
    )
    if category:
        q = q.where(func.lower(ScreentimeEvent.category) == category.lower())
    if device:
        q = q.where(ScreentimeEvent.device == device)
    rows = (await db.execute(q)).all()
    # Collapse the same app across devices (unless a device filter was given).
    agg: dict[str, dict] = {}
    for app, cat, dev, secs in rows:
        e = agg.setdefault(app, {"app": app, "category": (cat or "Other").lower(), "seconds": 0})
        e["seconds"] += int(secs or 0)
    out = sorted(agg.values(), key=lambda e: e["seconds"], reverse=True)
    return [{"app": e["app"], "category": e["category"], "minutes": round(e["seconds"] / 60)} for e in out]


@router.get("/daily")
async def get_daily_range(
    start: date = Query(default_factory=lambda: local_today() - timedelta(days=30)),
    end: date = Query(default_factory=date.today),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    result = await db.execute(
        select(DailyMetric)
        .where(DailyMetric.date >= start)
        .where(DailyMetric.date <= end)
        .order_by(DailyMetric.date)
    )
    rows = result.scalars().all()
    return [_serialize_daily(r) for r in rows]


@router.get("/events")
async def get_events(
    start: date = Query(default_factory=lambda: local_today() - timedelta(days=90)),
    end: date = Query(default_factory=date.today),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """
    Discrete day-events for the cross-chart "event spine" overlay: hard workouts
    and alcohol. Bucketed by LOCAL calendar day (matching /daily and
    /raw-trend, which key their dates the same way), so markers line up with the
    lines they annotate. Returns a flat list of {date, type, label, detail, color};
    a day may carry multiple events (one per type).
    """
    start_dt = local_day_bounds_utc(start)[0]
    end_dt = local_day_bounds_utc(end)[1]

    events: list[dict] = []

    # ── Hard workouts: per-day max TRIMP, flagged when in the window's top tercile ──
    wresult = await db.execute(
        select(Workout)
        .where(Workout.start_time >= start_dt)
        .where(Workout.start_time < end_dt)
        .order_by(Workout.start_time)
    )
    workouts = wresult.scalars().all()
    trimps = sorted(w.trimp for w in workouts if w.trimp is not None)
    # 67th-percentile TRIMP threshold for "hard"; falls back to high-intensity minutes.
    hard_trimp = None
    if len(trimps) >= 3:
        hard_trimp = trimps[int(len(trimps) * 0.67)]
    by_day: dict[str, Workout] = {}
    for w in workouts:
        is_hard = (
            (hard_trimp is not None and w.trimp is not None and w.trimp >= hard_trimp)
            or (w.high_intensity_min is not None and w.high_intensity_min >= 10)
        )
        if not is_hard:
            continue
        day = to_local_date(w.start_time).isoformat()
        # Keep the highest-TRIMP workout when a day has several.
        if day not in by_day or (w.trimp or 0) > (by_day[day].trimp or 0):
            by_day[day] = w
    for day, w in by_day.items():
        load = f"TRIMP {round(w.trimp)}" if w.trimp is not None else (w.activity_type or "workout")
        events.append({
            "date": day, "type": "hard_workout",
            "label": "Hard workout", "detail": load, "color": "#818cf8",
        })

    # ── Alcohol: standard drink units summed per FOOD day (04:00 boundary) ─────────
    hm_day = local_food_date_sql(HealthMetric.timestamp)
    aresult = await db.execute(
        select(
            hm_day.label("day"),
            func.sum(HealthMetric.value).label("total"),
        )
        .where(HealthMetric.metric_type == "alcohol")
        .where(HealthMetric.timestamp >= start_dt)
        .where(HealthMetric.timestamp < end_dt)
        .group_by(hm_day)
    )
    for r in aresult.all():
        if r.total and r.total > 0:
            sdu = round(r.total, 1)
            events.append({
                "date": str(r.day), "type": "alcohol",
                "label": "Alcohol", "detail": f"{sdu} SDU", "color": "#f59e0b",
            })

    events.sort(key=lambda e: e["date"])
    return events


@router.get("/sleep")
async def get_sleep(
    start: date = Query(default_factory=lambda: local_today() - timedelta(days=30)),
    end: date = Query(default_factory=date.today),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    result = await db.execute(
        select(SleepSession)
        .where(SleepSession.date >= start)
        .where(SleepSession.date <= end)
        .order_by(SleepSession.date)
    )
    rows = result.scalars().all()
    return [
        {
            "date": s.date.isoformat(),
            "bedtime": s.bedtime.isoformat() if s.bedtime else None,
            "wake_time": s.wake_time.isoformat() if s.wake_time else None,
            # UTC offset in force at each instant where the user WAS (tz phase 3), so the
            # schedule charts draw a 22:00 bedtime abroad at 22:00, not at the phone's
            # current-zone reading of it (15:00 Eastern, off the chart axis).
            "bed_offset_min": _offset_min(s.bedtime),
            "wake_offset_min": _offset_min(s.wake_time),
            "total_min": s.total_min,
            "deep_min": s.deep_min,
            "core_min": s.core_min,
            "rem_min": s.rem_min,
            "awake_min": s.awake_min,
        }
        for s in rows
    ]


@router.get("/raw-trend")
async def get_raw_trend(
    metric_type: str,
    start: date = Query(default_factory=lambda: local_today() - timedelta(days=30)),
    end: date = Query(default_factory=date.today),
    agg: str = Query(default="avg"),  # "avg" or "sum"
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Return LOCAL-day aggregates for any health_metrics type — used for charting raw trends."""
    agg_fn = func.sum if agg == "sum" else func.avg
    # Alcohol follows the 04:00 food day (a 00:30 drink is the night before's).
    day_col = (local_food_date_sql if metric_type == "alcohol" else local_date_sql)(HealthMetric.timestamp)
    result = await db.execute(
        select(
            day_col.label("date"),
            agg_fn(HealthMetric.value).label("value"),
        )
        .where(HealthMetric.metric_type == metric_type)
        .where(HealthMetric.timestamp >= local_day_bounds_utc(start)[0])
        .where(HealthMetric.timestamp < local_day_bounds_utc(end)[1])
        .group_by(day_col)
        .order_by(day_col)
    )
    rows = result.all()
    return [
        {"date": str(r.date), "value": round(r.value, 2) if r.value is not None else None}
        for r in rows
    ]


@router.get("/monthly-history")
async def get_monthly_history(
    months: int = Query(default=12, ge=1, le=24),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """
    Per-month aggregate stats for the past `months` months.
    Returns workout count, active time, calories, avg HRV/readiness/sleep, top activity.
    Uses 3 batch queries instead of N×3 sequential queries.
    """
    from calendar import monthrange
    from collections import Counter, defaultdict

    today = local_today()

    # Build the ordered list of (year, month) buckets we want
    buckets = []
    for i in range(months - 1, -1, -1):
        month_offset = today.month - 1 - i
        year  = today.year + month_offset // 12
        month = month_offset % 12 + 1
        month_start = date(year, month, 1)
        if month_start <= today:
            buckets.append((year, month))

    if not buckets:
        return []

    earliest_year, earliest_month = buckets[0]
    range_start = date(earliest_year, earliest_month, 1)
    last_year, last_month = buckets[-1]
    range_end = date(last_year, last_month, monthrange(last_year, last_month)[1])

    # ── Batch query 1: all workouts in the full range ─────────────────────────
    wo_rows = (await db.execute(
        select(
            Workout.start_time,
            Workout.duration_sec,
            Workout.active_calories,
            Workout.activity_type,
        )
        .where(Workout.start_time >= local_day_bounds_utc(range_start)[0])
        .where(Workout.start_time < local_day_bounds_utc(range_end)[1])
    )).all()

    # Group workouts by local "YYYY-MM"
    wo_by_month: dict[str, list] = defaultdict(list)
    for w in wo_rows:
        key = to_local_date(w.start_time).strftime("%Y-%m")
        wo_by_month[key].append(w)

    # ── Batch query 2: monthly avg readiness/HRV/RHR from daily_metrics ──────
    dm_rows = (await db.execute(
        select(
            func.strftime("%Y-%m", DailyMetric.date).label("month"),
            func.avg(DailyMetric.readiness_score).label("avg_readiness"),
            func.avg(DailyMetric.hrv_value).label("avg_hrv"),
            func.avg(DailyMetric.rhr_value).label("avg_rhr"),
        )
        .where(DailyMetric.date >= range_start)
        .where(DailyMetric.date <= range_end)
        .group_by(func.strftime("%Y-%m", DailyMetric.date))
    )).all()
    dm_by_month = {r.month: r for r in dm_rows}

    # ── Batch query 3: monthly avg sleep ─────────────────────────────────────
    sl_rows = (await db.execute(
        select(
            func.strftime("%Y-%m", SleepSession.date).label("month"),
            func.avg(SleepSession.total_min).label("avg_min"),
        )
        .where(SleepSession.date >= range_start)
        .where(SleepSession.date <= range_end)
        .where(SleepSession.total_min > 0)
        .group_by(func.strftime("%Y-%m", SleepSession.date))
    )).all()
    sl_by_month = {r.month: r.avg_min for r in sl_rows}

    # ── Assemble results ──────────────────────────────────────────────────────
    def _r(v, dec=1):
        if v is None: return None
        return round(float(v), dec) if dec else round(float(v))

    results = []
    for year, month in buckets:
        key = f"{year}-{month:02d}"
        month_start = date(year, month, 1)
        workouts = wo_by_month.get(key, [])
        dm = dm_by_month.get(key)
        avg_min = sl_by_month.get(key)

        activity_counts = Counter(w.activity_type for w in workouts if w.activity_type)
        top = activity_counts.most_common(1)

        results.append({
            "month":              key,
            "label":              month_start.strftime("%b %Y"),
            "short_label":        month_start.strftime("%b"),
            "workout_count":      len(workouts),
            "active_min":         round(sum((w.duration_sec or 0) for w in workouts) / 60),
            "active_calories":    round(sum(w.active_calories or 0 for w in workouts)),
            "avg_readiness":      _r(dm.avg_readiness, 0) if dm else None,
            "avg_hrv":            _r(dm.avg_hrv, 0) if dm else None,
            "avg_rhr":            _r(dm.avg_rhr, 0) if dm else None,
            "avg_sleep_h":        _r(float(avg_min) / 60 if avg_min else None),
            "top_activity":       top[0][0] if top else None,
            "top_activity_count": top[0][1] if top else None,
        })

    return results


_DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@router.get("/day-of-week")
async def get_day_of_week(
    outcome: str = Query(default="readiness_score"),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """
    Descriptive day-of-week contrast for a chosen outcome.
    Returns two_way (weekday/weekend), seven_way (Mon-Sun), and by_behavior
    (training-day vs rest-day) cuts, each with mean + naive 95% CI.

    FRAMING: these are exploratory descriptions, NOT significance tests.
    Day-of-week is frequently a proxy for behaviour (e.g. 'bad Tuesdays' =
    interval-training day); by_behavior is usually the more informative cut.
    No p-values are returned or should be inferred from these CIs.
    """
    if outcome not in _DOW_OUTCOME_LABELS:
        raise HTTPException(
            status_code=400,
            detail=f"outcome must be one of: {sorted(_DOW_OUTCOME_LABELS)}",
        )

    # ── Fetch outcome values ──────────────────────────────────────────────────
    points = await _outcome_points(db, outcome)

    # ── Training-day dates (non-walk workout; matches worked_out derived habit) ─
    from app.workout_categories import LOW_INTENSITY_TYPES
    wo_rows = (await db.execute(
        select(local_date_sql(Workout.start_time).label("day"))
        .where(Workout.activity_type.notin_(LOW_INTENSITY_TYPES))
        .distinct()
    )).all()
    training_dates: set[date] = {date.fromisoformat(str(r.day)) for r in wo_rows}

    # ── Group values ──────────────────────────────────────────────────────────
    by_dow: dict[int, list[float]] = {i: [] for i in range(7)}
    weekday_vals: list[float] = []
    weekend_vals: list[float] = []
    training_vals: list[float] = []
    rest_vals: list[float] = []

    for d, v in points.items():
        dow = d.weekday()           # 0 = Mon, 6 = Sun
        by_dow[dow].append(v)
        (weekend_vals if dow >= 5 else weekday_vals).append(v)
        (training_vals if d in training_dates else rest_vals).append(v)

    # ── Build response ────────────────────────────────────────────────────────
    seven_stats = [_group_stats(by_dow[i]) for i in range(7)]
    seven_available = all(s["n"] >= 10 for s in seven_stats)
    min_dow_n = min(s["n"] for s in seven_stats) if seven_stats else 0

    wd_stats = _group_stats(weekday_vals)
    we_stats = _group_stats(weekend_vals)
    tr_stats = _group_stats(training_vals)
    re_stats = _group_stats(rest_vals)

    return {
        "outcome":       outcome,
        "outcome_label": _DOW_OUTCOME_LABELS[outcome],
        "total_n":       len(points),
        "two_way": {
            "available": wd_stats["n"] >= 8 and we_stats["n"] >= 8,
            "weekday":   wd_stats,
            "weekend":   we_stats,
        },
        "seven_way": {
            "available": seven_available,
            "min_n":     min_dow_n,
            "days": [
                {"day": _DOW_NAMES[i], **seven_stats[i]}
                for i in range(7)
            ],
        },
        "by_behavior": {
            "available":    tr_stats["n"] >= 8 and re_stats["n"] >= 8,
            "training_day": tr_stats,
            "rest_day":     re_stats,
        },
    }


@router.get("/daylight-timing")
async def get_daylight_timing(
    start: date = Query(...),
    end: date = Query(...),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Per-day daylight TIMING, derived from the minute-level `time_in_daylight` series.

    Dose has always been available (total minutes); timing has not, even though it was
    sitting in the same rows — 60 min at 08:00 and 60 min at 18:00 are identical totals
    and very different circadian signals.

    Derived on read, never stored, so it is correct across the whole history without a
    migration or backfill. Mirrors services/correlations exactly (>= 0.5 counts a minute
    as daylight, morning = before MORNING_LIGHT_CUTOFF_H local); if one changes, change
    both or the chart and the correlations will quietly disagree.
    """
    from zoneinfo import ZoneInfo

    from app.config import settings as app_settings
    from app.services.correlations import MORNING_LIGHT_CUTOFF_H
    from app.timeutils import local_date_sql, local_day_bounds_utc

    day_col = local_date_sql(HealthMetric.timestamp)
    rows = (await db.execute(
        select(day_col.label("day"), HealthMetric.timestamp, HealthMetric.value)
        .where(HealthMetric.metric_type == "time_in_daylight")
        .where(HealthMetric.timestamp >= local_day_bounds_utc(start)[0])
        .where(HealthMetric.timestamp < local_day_bounds_utc(end)[1])
        .order_by(HealthMetric.timestamp)
    )).all()

    tz = ZoneInfo(app_settings.tz)
    out: dict[str, dict] = {}
    for r in rows:
        key = str(r.day)
        local = to_local(r.timestamp)
        hour = local.hour + local.minute / 60.0
        val = float(r.value or 0.0)
        d = out.setdefault(key, {"date": key, "first_light_hour": None,
                                 "morning_light_min": 0.0, "total_min": 0.0,
                                 "_num": 0.0, "_den": 0.0})
        if val >= 0.5 and d["first_light_hour"] is None:
            d["first_light_hour"] = round(hour, 3)
        if hour < MORNING_LIGHT_CUTOFF_H:
            d["morning_light_min"] += val
        d["total_min"] += val
        d["_num"] += hour * val
        d["_den"] += val

    series = []
    for key in sorted(out):
        d = out[key]
        centroid = round(d["_num"] / d["_den"], 3) if d["_den"] > 0 else None
        series.append({"date": d["date"], "first_light_hour": d["first_light_hour"],
                       "morning_light_min": round(d["morning_light_min"], 1),
                       "total_min": round(d["total_min"], 1),
                       "light_centroid_hour": centroid})
    return {"morning_cutoff_hour": MORNING_LIGHT_CUTOFF_H, "days": series}


@router.get("/audio-exposure")
async def get_audio_exposure(
    weeks: int = Query(default=52, ge=8, le=260),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Weekly noise-dose baseline. See services/audio_exposure for the NIOSH maths.

    Long-horizon by design: the value is detecting a change in acoustic environment
    over months, which is why this is weekly and lives in Body rather than Today.
    """
    from app.services.audio_exposure import weekly_series
    return await weekly_series(db, weeks=weeks)
