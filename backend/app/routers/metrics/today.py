"""The Today dashboard: GET /today (one big payload) and the activity-for-date view.

Part of the /api/metrics package (split out of routers/metrics.py 2026-10-21)."""
import logging
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import BodyWeightLog, DailyMetric, HealthMetric, MonthlySummary, ScreentimeEvent, SleepSession, SubjectiveLog, UserSettings, Workout, WeeklySummary
from app.timeutils import local_day_bounds_utc, local_today, local_now, on_local_days, to_local_date
from app.routers.metrics._common import _serialize_daily
from app.services.training_rec import _strength_hint, _training_rec

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/activity-for-date")
async def get_activity_for_date(
    date_str: str = Query(alias="date"),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Return activity totals (steps, active_calories, etc.) for any calendar date."""
    try:
        target = date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format, expected YYYY-MM-DD")

    day_start, day_end = local_day_bounds_utc(target)
    rows = (await db.execute(
        select(HealthMetric.metric_type, func.sum(HealthMetric.value).label("v"))
        .where(HealthMetric.metric_type.in_(["steps", "active_calories", "resting_calories", "exercise_minutes", "stand_hours"]))
        .where(HealthMetric.timestamp >= day_start)
        .where(HealthMetric.timestamp < day_end)
        .group_by(HealthMetric.metric_type)
    )).all()
    totals = {r.metric_type: r.v for r in rows}
    return {
        "date": date_str,
        "steps": round(totals["steps"]) if totals.get("steps") else None,
        "active_calories": round(totals["active_calories"]) if totals.get("active_calories") else None,
        "resting_calories": round(totals["resting_calories"]) if totals.get("resting_calories") else None,
        "exercise_minutes": round(totals["exercise_minutes"]) if totals.get("exercise_minutes") else None,
        "stand_hours": round(totals["stand_hours"]) if totals.get("stand_hours") else None,
    }


async def _screentime_today(db: AsyncSession, day: date) -> dict | None:
    """Aggregate today's screen-time by category and device, in minutes.

    Buckets by LOCAL calendar day (start_time is naive UTC → local via
    `local_date_sql`), consistent with the activity rings. Category keys are
    lower-cased (social/entertainment/…). Returns None only when screen-time has
    NEVER been collected; on a day with no rows yet (Mac asleep overnight +
    iPhone sync lag) it still returns a zeroed block so the Today card persists
    with its "synced through" note instead of vanishing day to day.
    """
    rows = (await db.execute(
        select(
            ScreentimeEvent.category,
            ScreentimeEvent.device,
            func.sum(ScreentimeEvent.duration_sec),
        )
        .where(on_local_days(ScreentimeEvent.start_time, day))
        .group_by(ScreentimeEvent.category, ScreentimeEvent.device)
    )).all()

    # Latest local day with any iPhone data — drives the "synced through" note.
    # Local date of the latest start_time: the instant→local-day mapping is monotonic
    # (away segments end on local midnights), so this equals MAX(local_date_sql(...))
    # without converting every row.
    _last_phone = (await db.execute(
        select(func.max(ScreentimeEvent.start_time))
        .where(ScreentimeEvent.device == "iPhone")
    )).scalar()
    phone_synced_through = to_local_date(_last_phone).isoformat() if _last_phone else None

    if not rows:
        ever = phone_synced_through is not None or ((await db.execute(
            select(func.count()).select_from(ScreentimeEvent)
        )).scalar() or 0) > 0
        if not ever:
            return None
        return {
            "total_minutes": 0,
            "by_category": {},
            "phone_minutes": 0,
            "mac_minutes": 0,
            "hourly": [{"hour": h, "phone": {}, "mac": {}} for h in range(24)],
            "phone_synced_through": phone_synced_through,
        }

    by_category: dict[str, int] = {}
    phone_sec = mac_sec = total_sec = 0
    for category, device, sec in rows:
        sec = int(sec or 0)
        total_sec += sec
        cat = (category or "Other").lower()
        by_category[cat] = by_category.get(cat, 0) + sec
        if device == "iPhone":
            phone_sec += sec
        elif device == "Mac":
            mac_sec += sec

    # Per-hour breakdown (local hour-of-day) for the Today histogram, split by
    # device and category. Each session is attributed to its START hour (sessions
    # are short, so boundary-straddling is negligible).
    hrows = (await db.execute(
        select(
            func.strftime("%H", ScreentimeEvent.start_time, "localtime").label("hr"),
            ScreentimeEvent.category,
            ScreentimeEvent.device,
            func.sum(ScreentimeEvent.duration_sec),
        )
        .where(on_local_days(ScreentimeEvent.start_time, day))
        .group_by("hr", ScreentimeEvent.category, ScreentimeEvent.device)
    )).all()
    hbuf: dict[int, dict[str, dict[str, int]]] = {}
    for hr, category, device, sec in hrows:
        h = int(hr)
        slot = hbuf.setdefault(h, {"phone": {}, "mac": {}})
        key = "phone" if device == "iPhone" else "mac"
        cat = (category or "Other").lower()
        slot[key][cat] = slot[key].get(cat, 0) + int(sec or 0)
    hourly = [
        {
            "hour": h,
            "phone": {c: round(s / 60) for c, s in hbuf.get(h, {}).get("phone", {}).items()},
            "mac": {c: round(s / 60) for c, s in hbuf.get(h, {}).get("mac", {}).items()},
        }
        for h in range(24)
    ]

    return {
        "total_minutes": round(total_sec / 60),
        "by_category": {k: round(v / 60) for k, v in by_category.items()},
        "phone_minutes": round(phone_sec / 60),
        "mac_minutes": round(mac_sec / 60),
        "hourly": hourly,
        "phone_synced_through": phone_synced_through,
    }


@router.get("/today")
async def get_today(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Return today's daily_metrics row (or most recent), plus latest weight and yesterday's workout."""
    today = local_today()

    # Today's or most recent daily metrics
    result = await db.execute(
        select(DailyMetric)
        .where(DailyMetric.date <= today)
        .order_by(DailyMetric.date.desc())
        .limit(1)
    )
    dm = result.scalar_one_or_none()

    # Latest weight: prefer manual body_weight_log, fall back to health_metrics
    wt_result = await db.execute(
        select(BodyWeightLog).order_by(BodyWeightLog.timestamp.desc()).limit(1)
    )
    latest_weight_row = wt_result.scalar_one_or_none()
    if latest_weight_row:
        latest_weight = {"value": latest_weight_row.weight_kg, "unit": "kg", "timestamp": latest_weight_row.timestamp}
    else:
        from app.models import HealthMetric
        hm_result = await db.execute(
            select(HealthMetric)
            .where(HealthMetric.metric_type == "weight")
            .order_by(HealthMetric.timestamp.desc())
            .limit(1)
        )
        hm_row = hm_result.scalar_one_or_none()
        latest_weight = {"value": hm_row.value, "unit": hm_row.unit or "lbs", "timestamp": hm_row.timestamp} if hm_row else None

    # Activity totals for today (local calendar day — used for activity rings).
    # Timestamps are stored naive-UTC, so convert the local-day window to UTC or
    # the rings roll over at the UTC-offset boundary (~20:00 local in US Eastern).
    today_start, tomorrow_start = local_day_bounds_utc(today)

    steps_today = (await db.execute(
        select(func.sum(HealthMetric.value))
        .where(HealthMetric.metric_type == "steps")
        .where(HealthMetric.timestamp >= today_start)
        .where(HealthMetric.timestamp < tomorrow_start)
    )).scalar()

    cals_today = (await db.execute(
        select(func.sum(HealthMetric.value))
        .where(HealthMetric.metric_type == "active_calories")
        .where(HealthMetric.timestamp >= today_start)
        .where(HealthMetric.timestamp < tomorrow_start)
    )).scalar()

    resting_cals_today = (await db.execute(
        select(func.sum(HealthMetric.value))
        .where(HealthMetric.metric_type == "resting_calories")
        .where(HealthMetric.timestamp >= today_start)
        .where(HealthMetric.timestamp < tomorrow_start)
    )).scalar()

    exercise_today = (await db.execute(
        select(func.sum(HealthMetric.value))
        .where(HealthMetric.metric_type == "exercise_minutes")
        .where(HealthMetric.timestamp >= today_start)
        .where(HealthMetric.timestamp < tomorrow_start)
    )).scalar()

    stand_today = (await db.execute(
        select(func.sum(HealthMetric.value))
        .where(HealthMetric.metric_type == "stand_hours")
        .where(HealthMetric.timestamp >= today_start)
        .where(HealthMetric.timestamp < tomorrow_start)
    )).scalar()

    # ── Recovery / health signals (Tier 1 + 2, added 2026-07-02) ────────────────
    # Walking HR is sparse and not tied to the calendar day, so we surface the most
    # recent reading rather than a today-only aggregate. Blood oxygen and breathing
    # disturbances are nightly/continuous, so we take today's average.
    walking_hr_row = (await db.execute(
        select(HealthMetric.value, HealthMetric.timestamp)
        .where(HealthMetric.metric_type == "walking_heart_rate_avg")
        .order_by(HealthMetric.timestamp.desc())
        .limit(1)
    )).first()

    blood_oxygen_today = (await db.execute(
        select(func.avg(HealthMetric.value))
        .where(HealthMetric.metric_type == "blood_oxygen")
        .where(HealthMetric.timestamp >= today_start)
        .where(HealthMetric.timestamp < tomorrow_start)
    )).scalar()

    breathing_disturbances_today = (await db.execute(
        select(func.avg(HealthMetric.value))
        .where(HealthMetric.metric_type == "breathing_disturbances")
        .where(HealthMetric.timestamp >= today_start)
        .where(HealthMetric.timestamp < tomorrow_start)
    )).scalar()

    # Yesterday's workouts (local calendar day → naive-UTC bounds)
    yesterday = today - timedelta(days=1)
    yday_start, yday_end = local_day_bounds_utc(yesterday)
    wo_result = await db.execute(
        select(Workout)
        .where(Workout.start_time >= yday_start)
        .where(Workout.start_time < yday_end)
        .order_by(Workout.start_time.desc())
    )
    yesterday_workouts = wo_result.scalars().all()

    # Strength progression hint — only when Hevy history exists (last 30 days).
    # "Lifted yesterday" from Hevy first, Apple strength-category workouts as fallback.
    from app.models import HevyWorkout
    from app.workout_categories import categorize_activity
    has_recent_hevy = bool((await db.execute(
        select(func.count(HevyWorkout.id))
        .where(HevyWorkout.start_time >= datetime.utcnow() - timedelta(days=30))
    )).scalar())
    strength_hint = None
    if has_recent_hevy:
        lifted_yesterday = bool((await db.execute(
            select(func.count(HevyWorkout.id))
            .where(HevyWorkout.start_time >= yday_start)
            .where(HevyWorkout.start_time < yday_end)
        )).scalar()) or any(
            categorize_activity(w.activity_type) == "strength" for w in yesterday_workouts
        )
        strength_hint = _strength_hint(dm, lifted_yesterday)

    # Coach: the BLINDED always-on recommendation. Deliberately carries no
    # mechanism (randomized vs metric-based) and no number-citing rationale —
    # revealing either would let compliance differ by mechanism and unblind the
    # experiment. Unblind on purpose via the /api/experiments endpoints.
    from app.services.coach import get_coach_recommendation
    coach = await get_coach_recommendation(db, today)

    # Last Apple Health sync — prefer webhook receipt time (set when data actually arrives);
    # fall back to max metric timestamp for bootstrap before first post-deploy webhook.
    settings_result = await db.execute(select(UserSettings).limit(1))
    user_settings_row = settings_result.scalar_one_or_none()
    last_sync_at = user_settings_row.last_webhook_at if user_settings_row else None
    if last_sync_at is None:
        fallback = await db.execute(select(func.max(HealthMetric.timestamp)))
        last_sync_at = fallback.scalar()

    # Most recent weekly summary (show up to 8 days old)
    cutoff = today - timedelta(days=8)
    ws_result = await db.execute(
        select(WeeklySummary)
        .where(WeeklySummary.week_start >= cutoff)
        .order_by(WeeklySummary.week_start.desc())
        .limit(1)
    )
    weekly = ws_result.scalar_one_or_none()

    # Most recent monthly summary (show up to 45 days old — covers most of current month)
    month_cutoff = today - timedelta(days=45)
    ms_result = await db.execute(
        select(MonthlySummary)
        .where(MonthlySummary.month_start >= month_cutoff)
        .order_by(MonthlySummary.month_start.desc())
        .limit(1)
    )
    monthly = ms_result.scalar_one_or_none()

    # Today's morning check-in (subjective log)
    subj_result = await db.execute(
        select(SubjectiveLog).where(SubjectiveLog.date == today)
    )
    subj = subj_result.scalar_one_or_none()

    # Today's sleep session — for deep+REM % signal in training rec chips
    sleep_today = (await db.execute(
        select(SleepSession).where(SleepSession.date == today)
    )).scalar_one_or_none()
    sleep_quality_pct = None
    if sleep_today and sleep_today.total_min:
        deep_rem = (sleep_today.deep_min or 0) + (sleep_today.rem_min or 0)
        sleep_quality_pct = deep_rem / sleep_today.total_min * 100

    # Composite illness / overtraining early-warning
    from app.services.recovery_alarm import assess_recovery_alarm
    recovery_alarm = await assess_recovery_alarm(db, today)

    # ── data_health: readiness input completeness for today ─────────────────────
    # Check today's DM specifically — dm may be yesterday's if 8am recompute
    # hasn't run yet.  Checks hrv_value/rhr_value (raw readings, not z-scores)
    # because a null raw value means no Apple Watch reading arrived overnight;
    # sleep_today = None means no sleep session for last night; acwr = None means
    # not enough load history yet (first ~14 days) or load data never arrived.
    today_dm = dm if (dm is not None and dm.date == today) else None
    if today_dm is None:
        data_health = {
            "readiness_complete": False,
            "missing_inputs": ["Today's metrics not yet computed (daily refresh runs at 8am)"],
        }
    else:
        READINESS_CHECKS = [
            (today_dm.hrv_value is not None, "HRV (no overnight reading)"),
            (today_dm.rhr_value is not None, "RHR (no overnight reading)"),
            (sleep_today is not None,         "Sleep (no sleep session)"),
            (today_dm.acwr is not None,       "Training load / ACWR"),
        ]
        missing_inputs = [label for present, label in READINESS_CHECKS if not present]
        data_health = {
            "readiness_complete": len(missing_inputs) == 0,
            "missing_inputs": missing_inputs,
        }

    # Resolved energy for the displayed day. Computed server-side so there is exactly
    # one goal derivation: the client used to roll its own (Mifflin for today, Apple's
    # resting for past dates), which put a ~380 kcal step at the one-day boundary and
    # disagreed with the nudge card.
    from app.services.energy import effective_calorie_goal
    from app.timeutils import local_today as _local_today

    # This endpoint always reports the current day, so the projection is always partial.
    _hour = local_now().hour
    try:
        _energy = await effective_calorie_goal(db, user_settings_row, _local_today(), _hour)
    except Exception as exc:
        logger.error("today: calorie goal failed (Today shows no goal): %s", exc)
        _energy = None

    _energy_block = None
    if _energy:
        _rmr = _energy.get("rmr")
        _k = _energy.get("k") or 1.0
        _act = _energy.get("active") or {}
        _out_so_far = _out_projected = None
        if _rmr is not None:
            # RMR accrues with the clock; active accrues as it is measured. For a past
            # day the full RMR has elapsed, so the fraction is 1.
            _elapsed = (_hour / 24.0) if _hour is not None else 1.0
            _out_so_far = round(_rmr * _elapsed + _k * (_act.get("so_far") or 0))
            _out_projected = round(_rmr + _k * (_act.get("projected") or 0))
        _energy_block = {
            "goal": _energy.get("goal"),
            "mode": _energy.get("mode"),
            "rmr": _rmr,
            "rmr_basis": _energy.get("rmr_basis"),
            "k": _energy.get("k"),
            "daily_offset": _energy.get("daily_offset"),
            "active_so_far": _act.get("so_far"),
            "active_projected": _act.get("projected"),
            "out_so_far": _out_so_far,
            "out_projected": _out_projected,
        }

    # Away today (a travel flag): the frontend stands down its check-in takeovers and
    # meal reminders. Recovery days are NOT away — logging runs as normal (2026-10-19).
    from app.services.day_flags import is_away
    _away_today = await is_away(db, _local_today())

    return {
        "away": _away_today,
        "metrics": _serialize_daily(dm) if dm else None,
        "activity": {
            "steps": round(steps_today) if steps_today else None,
            "active_calories": round(cals_today) if cals_today else None,
            "resting_calories": round(resting_cals_today) if resting_cals_today else None,
            "exercise_minutes": round(exercise_today) if exercise_today else None,
            "stand_hours": round(stand_today) if stand_today else None,
        },
        "energy": _energy_block,
        "recovery_signals": {
            "walking_heart_rate_avg": {
                "value": round(walking_hr_row.value, 1),
                "timestamp": walking_hr_row.timestamp.isoformat(),
            } if walking_hr_row and walking_hr_row.value is not None else None,
            "blood_oxygen": round(blood_oxygen_today, 3) if blood_oxygen_today is not None else None,
            "breathing_disturbances": round(breathing_disturbances_today, 2) if breathing_disturbances_today is not None else None,
        },
        "latest_weight": {
            "value": latest_weight["value"],
            "unit": latest_weight["unit"],
            "timestamp": latest_weight["timestamp"].isoformat(),
        } if latest_weight else None,
        "yesterday_workouts": [
            {
                "id": w.id,
                "activity_type": w.activity_type,
                "duration_sec": w.duration_sec,
                "active_calories": w.active_calories,
                "avg_hr": w.avg_hr,
                "start_time": w.start_time.isoformat(),
                "rpe": w.rpe,
            }
            for w in yesterday_workouts
        ],
        "weekly_summary": {
            "week_start": weekly.week_start.isoformat(),
            "week_end": weekly.week_end.isoformat(),
            "summary_text": weekly.summary_text,
        } if weekly else None,
        "monthly_summary": {
            "year_month": monthly.year_month,
            "month_start": monthly.month_start.isoformat(),
            "month_end": monthly.month_end.isoformat(),
            "summary_text": monthly.summary_text,
        } if monthly else None,
        "last_sync_at": last_sync_at.isoformat() if last_sync_at else None,
        "training_rec": _training_rec(dm, sleep_quality_pct=sleep_quality_pct),
        # Strength hint only on days the coach's call admits lifting: on REST/EASY
        # days there is no lift to progress, and a green "PROGRESS" under a
        # randomized REST would partially unblind the experiment. Gating on the
        # coach LABEL (not the mechanism) keeps the presence pattern uniform.
        "strength_hint": (strength_hint
                          if coach and coach["label"] in ("MODERATE", "HARD", "CLASS", "+ 4TH")
                          else None),
        "coach": coach,
        "morning_checkin": {
            "wellbeing_score": subj.wellbeing_score,
            "sleep_quality": subj.sleep_quality,
            "energy": subj.energy,
            "mood": subj.mood,
            "soreness": subj.soreness,
            "stress": subj.stress,
            "sick": subj.sick,
            "day_score": subj.day_score,
            "notes": subj.notes,
        } if subj else None,
        "recovery_alarm": recovery_alarm,
        "data_health": data_health,
        "screentime": await _screentime_today(db, today),
    }
