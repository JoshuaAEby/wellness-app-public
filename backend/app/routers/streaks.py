"""Streak computation for all tracked daily goals."""
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import BodyWeightLog, FoodLog, HabitLog, HealthMetric, SleepSession, UserSettings, Workout
from app.timeutils import local_date_sql, local_day_bounds_utc, local_month_sql, local_today, local_now, local_food_date_sql
from app.workout_categories import LOW_INTENSITY_TYPES

router = APIRouter(prefix="/api/streaks", tags=["streaks"])

STAND_GOAL = 12  # Apple's standard stand goal (hours)
# Re-exported, not redefined: `services/nutrition_goals` is the canonical home (a
# router is the wrong place for a rule that four other modules need). Importers that
# do `from app.routers.streaks import PROTEIN_MULT` keep working unchanged.
from app.services.nutrition_goals import PROTEIN_MULT  # noqa: E402,F401
from app.foodday import food_date_sql
# Per-process cache of the yearly activity aggregate; see get_streaks.
_ACTIVITY_CACHE: dict = {}
MAX_CONSECUTIVE_GAPS = 3   # consecutive no-data days before streak is considered broken
LOOKBACK_DAYS = 365        # max streak length tracked


def _gap_days(prev_d: date, d: date, held: frozenset) -> int:
    """Calendar days strictly between prev_d and d that are NOT held."""
    return sum(1 for i in range(1, (d - prev_d).days)
               if prev_d + timedelta(days=i) not in held)


def _compute_streak(
    daily: dict,
    goal: Optional[float],
    today: date,
    gap_breaks: bool = False,
    goal_by_day: Optional[dict] = None,
    held: frozenset = frozenset(),
) -> Optional[dict]:
    """
    Compute streak going backwards from yesterday.

    Rules:
      - data >= effective_goal → counts toward streak
      - data < effective_goal  → streak broken immediately
      - no data (None):
          gap_breaks=False → gap day; MAX_CONSECUTIVE_GAPS consecutive gaps → broken
          gap_breaks=True  → treated as 0 → streak broken immediately

    goal_by_day: optional {date: float} map of per-day goals. Falls back to scalar
    `goal` for days not in the map. When provided, scalar `goal` may be None (disabled
    check is skipped).

    held: days that neither extend nor break the streak — away days for streaks that
    depend on logging (protein, calories), see get_streaks (2026-10-19).

    Returns None if goal is None and goal_by_day is not provided.
    """
    if goal is None and not goal_by_day:
        return None

    def _eff_goal(d: date) -> Optional[float]:
        if goal_by_day is not None:
            return goal_by_day.get(d, goal)
        return goal

    streak = 0
    total_gaps = 0
    consecutive_gaps = 0

    d = today - timedelta(days=1)
    for _ in range(LOOKBACK_DAYS):
        if d in held:
            d -= timedelta(days=1)
            continue
        val = daily.get(d)
        g = _eff_goal(d)
        if val is None:
            if gap_breaks:
                break
            consecutive_gaps += 1
            total_gaps += 1
            if consecutive_gaps >= MAX_CONSECUTIVE_GAPS:
                break
        elif g is None:
            # No goal defined for this day — treat as gap
            if gap_breaks:
                break
            consecutive_gaps += 1
            total_gaps += 1
            if consecutive_gaps >= MAX_CONSECUTIVE_GAPS:
                break
        elif val >= g:
            streak += 1
            consecutive_gaps = 0
        else:
            break
        d -= timedelta(days=1)

    today_val = daily.get(today)
    today_goal = _eff_goal(today)
    today_on_track = today_val is not None and today_goal is not None and today_val >= today_goal

    best = 0
    run = 0
    c_gaps = 0
    prev_di = None
    for di in sorted(k for k in daily.keys() if k not in held):
        v = daily[di]
        g = _eff_goal(di)
        skipped = _gap_days(prev_di, di, held) if prev_di is not None else 0
        if skipped > 0:
            if gap_breaks:
                run = 0
                c_gaps = 0
            else:
                c_gaps += skipped
                if c_gaps >= MAX_CONSECUTIVE_GAPS:
                    run = 0
                    c_gaps = 0
        if v is None or g is None:
            if gap_breaks:
                run = 0
                c_gaps = 0
            else:
                c_gaps += 1
                if c_gaps >= MAX_CONSECUTIVE_GAPS:
                    run = 0
                    c_gaps = 0
        elif v >= g:
            run += 1
            c_gaps = 0
            best = max(best, run)
        else:
            run = 0
            c_gaps = 0
        prev_di = di

    display_goal = today_goal if today_goal is not None else goal
    return {
        "streak": streak,
        "best":   best,
        "goal": round(display_goal, 1) if display_goal is not None and display_goal != int(display_goal) else (int(display_goal) if display_goal is not None else None),
        "today_value": round(today_val, 1) if today_val is not None else None,
        "today_on_track": today_on_track,
        "gap_days": total_gaps,
    }


@router.get("")
async def get_streaks(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    today = local_today()
    lookback_dt = local_day_bounds_utc(today - timedelta(days=LOOKBACK_DAYS))[0]

    # ── User settings ─────────────────────────────────────────────────────────
    s = (await db.execute(select(UserSettings).limit(1))).scalar_one_or_none()
    if not s:
        return {}

    steps_goal    = s.steps_goal    or 10_000
    move_goal     = s.move_goal     or 500
    exercise_goal = s.exercise_goal or 30
    sleep_goal    = s.target_sleep_hours or 8.0
    from app.services.energy import effective_calorie_goal, resolve_calorie_goal
    cal_resolved  = await resolve_calorie_goal(db, s)
    calorie_goal  = cal_resolved["value"]  # non-None only for fixed mode

    # Protein goal: ONE derivation, shared — see services/nutrition_goals.
    from app.services.nutrition_goals import resolve_protein_goal
    protein_goal = await resolve_protein_goal(db, s)

    # ── Activity: aggregate by date × metric_type ─────────────────────────────
    # A year of samples (~1M rows) costs ~0.6 s to scan on this box, and this endpoint
    # runs on every Today open. Activity rows only ever arrive via the HAE webhook, which
    # stamps user_settings.last_webhook_at — so the aggregate is cached per
    # (day, last webhook, away segments) and recomputed only after new data (2026-10-21).
    from app.timeutils import away_segments
    cache_key = (today, s.last_webhook_at, tuple(away_segments()))
    if _ACTIVITY_CACHE.get("key") == cache_key:
        activity = _ACTIVITY_CACHE["value"]
    else:
        act_rows = (await db.execute(
            select(
                local_date_sql(HealthMetric.timestamp).label("d"),
                HealthMetric.metric_type,
                func.sum(HealthMetric.value).label("total"),
            )
            .where(HealthMetric.metric_type.in_(
                ["steps", "active_calories", "resting_calories", "exercise_minutes", "stand_hours"]
            ))
            .where(HealthMetric.timestamp >= lookback_dt)
            .group_by(local_date_sql(HealthMetric.timestamp), HealthMetric.metric_type)
        )).all()

        activity = defaultdict(dict)
        for row in act_rows:
            d = row.d if isinstance(row.d, date) else date.fromisoformat(str(row.d))
            activity[row.metric_type][d] = row.total
        _ACTIVITY_CACHE.update(key=cache_key, value=activity)

    # ── Nutrition: aggregate by date ──────────────────────────────────────────
    food_rows = (await db.execute(
        select(
            food_date_sql(FoodLog.timestamp).label("d"),
            func.sum(FoodLog.calories).label("calories"),
            func.sum(FoodLog.protein_g).label("protein"),
        )
        .where(FoodLog.timestamp >= lookback_dt)
        .group_by(food_date_sql(FoodLog.timestamp))
    )).all()

    nut_protein  = {}
    nut_calories = {}
    for row in food_rows:
        d = row.d if isinstance(row.d, date) else date.fromisoformat(str(row.d))
        nut_protein[d]  = row.protein  or 0
        nut_calories[d] = row.calories or 0

    # ── Sleep: total minutes → hours by date ─────────────────────────────────
    sleep_rows = (await db.execute(
        select(SleepSession.date, SleepSession.total_min)
        .where(SleepSession.date >= today - timedelta(days=LOOKBACK_DAYS))
    )).all()
    sleep_by_date = {
        r.date: (r.total_min or 0) / 60.0
        for r in sleep_rows
    }

    # ── Walks (from workout records) ──────────────────────────────────────────
    walk_workout_rows = (await db.execute(
        select(local_date_sql(Workout.start_time).label("d"))
        .where(Workout.activity_type.in_(LOW_INTENSITY_TYPES))
        .where(Workout.start_time >= lookback_dt)
    )).all()
    # Dates absent from the map are treated as None by _habit_streak_stats → streak broken.
    # That's the correct behaviour: no walk record = didn't walk that day.
    walk_by_date: dict[date, bool] = {
        date.fromisoformat(str(r.d)): True for r in walk_workout_rows
    }

    # ── Workouts (non-walk training sessions) for the worked_out streak ──────────
    workout_rows = (await db.execute(
        select(local_date_sql(Workout.start_time).label("d"))
        .where(Workout.activity_type.notin_(LOW_INTENSITY_TYPES))
        .where(Workout.start_time >= lookback_dt)
    )).all()
    workout_by_date: dict[date, bool] = {
        date.fromisoformat(str(r.d)): True for r in workout_rows
    }

    # ── Alcohol (absence of a record = dry day). Derived habit, mirrors the calendar. ──
    # FOOD day (04:00 boundary): a 00:30 drink belongs to the night it was drunk.
    alcohol_rows = (await db.execute(
        select(local_food_date_sql(HealthMetric.timestamp).label("d"))
        .where(HealthMetric.metric_type == "alcohol")
        .where(HealthMetric.value > 0)
        .where(HealthMetric.timestamp >= lookback_dt)
    )).all()
    alcohol_days: set[date] = {
        r.d if isinstance(r.d, date) else date.fromisoformat(str(r.d)) for r in alcohol_rows
    }
    # Count from the first tracked drink so "best" isn't inflated by pre-tracking history.
    first_alc = (await db.execute(
        select(func.min(local_food_date_sql(HealthMetric.timestamp)))
        .where(HealthMetric.metric_type == "alcohol").where(HealthMetric.value > 0)
    )).scalar()
    first_alcohol_date = (
        first_alc if isinstance(first_alc, date)
        else date.fromisoformat(str(first_alc)) if first_alc else None
    )

    # ── Habits ────────────────────────────────────────────────────────────────
    habit_rows = (await db.execute(
        select(HabitLog)
        .where(HabitLog.date >= today - timedelta(days=LOOKBACK_DAYS))
        .order_by(HabitLog.date)
    )).scalars().all()

    habit_maps = {
        # no_late_caffeine defaults True (assumed unless explicitly failed = False)
        "caffeine":    {r.date: (False if r.no_late_caffeine is False else True) for r in habit_rows},
        "light":       {r.date: r.outdoor_light_am for r in habit_rows},
        "supplements": {r.date: r.supplements      for r in habit_rows},
        "creatine":    {r.date: r.creatine         for r in habit_rows},
        "meditation":  {r.date: r.meditation        for r in habit_rows},
    }

    # Auto-credit outdoor light from morning daylight (manual still wins)
    from app.routers.habits import _morning_light_days
    from app.config import settings as app_settings
    from zoneinfo import ZoneInfo
    ml = await _morning_light_days(db, today - timedelta(days=LOOKBACK_DAYS), today)
    for d in ml:
        if habit_maps["light"].get(d) is not True:
            habit_maps["light"][d] = True

    # For today: if the cutoff hour has passed and light is still not credited,
    # mark today as failed so the streak tile shows ✕ rather than ○.
    # (Past days without credit already break the streak the same way as False.)
    _tz = ZoneInfo(app_settings.tz)
    _now_local = local_now()
    _cutoff = (await db.execute(select(UserSettings.light_cutoff_hour).limit(1))).scalar() or 10
    if _now_local.hour >= _cutoff and habit_maps["light"].get(today) is not True:
        habit_maps["light"][today] = False

    # ── Per-day calorie goal for balance/pace modes ───────────────────────────
    #
    # Goes through effective_calorie_goal(), the single source of truth (see the
    # note above it in services/energy). This used to roll its own model and got
    # three things wrong, which together left the streak returning None on a live
    # `pace`-mode install:
    #
    #   1. It read the latest BodyWeightLog for the BMR weight. That table is
    #      EMPTY here — weight arrives from Apple into health_metrics — so
    #      weight_kg was None, the whole block was skipped, and cal_goal_by_day
    #      stayed None. With `pace` mode the scalar goal is None too, so
    #      _compute_streak returned None and the card silently showed nothing.
    #   2. It used Apple's `resting_calories` as the resting rate. Apple models
    #      basal rate rather than measuring it, and reads ~380 kcal/day high here
    #      — the exact bug resolve_rmr() exists to avoid.
    #   3. It applied no activity factor (k = 1.0), while the rest of the app
    #      scales Apple's active calories by the fitted k (~0.84).
    #
    # Only days that can affect a verdict are resolved: _compute_streak reads a
    # goal for the days present in `daily` (the `best` scan) plus today. Days
    # outside the map fall back to the scalar goal, which is None in dynamic
    # modes and is correctly treated as a gap.
    cal_goal_by_day: Optional[dict] = None
    if cal_resolved["dynamic"]:
        goal_days = set(nut_calories.keys()) | {today}
        cal_goal_by_day = {}
        _shared: dict = {}          # mode / RMR / k are day-independent: resolve once
        past = [d for d in goal_days if d < today]
        if past:                    # one grouped query instead of one per past day
            from app.services.energy import prefetch_active_totals
            await prefetch_active_totals(db, _shared, min(past), today - timedelta(days=1))
        for d in goal_days:
            # `hour` projects the rest of the day's active calories so a midday
            # goal isn't understated; a past day is complete, so it takes None.
            hour = local_now().hour if d == today else None
            resolved = await effective_calorie_goal(db, s, d, hour, shared=_shared)
            if resolved.get("goal") is not None:
                cal_goal_by_day[d] = resolved["goal"]
        cal_goal_by_day = cal_goal_by_day or None

    # ── Compute and return ────────────────────────────────────────────────────
    # gap_breaks=True for manually-logged metrics: not logging = didn't hit goal.
    # gap_breaks=False (default) for Apple Health metrics: missing data may just
    # mean the watch wasn't synced that day, not that the goal was missed.
    # AWAY days (travel flags) are HELD for streaks that need logging or read the clock:
    # food (protein, calories), caffeine timing, morning light (Eastern hours), and
    # walks/workouts. Records that were genuinely kept while away still count, so
    # supplements/creatine/meditation (unlogged = missed), alcohol
    # and and the Apple activity rings are judged as usual.
    from app.services.day_flags import away_dates
    held = frozenset(await away_dates(db, today - timedelta(days=LOOKBACK_DAYS), today))
    # Trip days he said weren't logged (note 102) are held too: unknown, not missed or dry.
    from app.services.trip_gaps import gap_dates
    lo = today - timedelta(days=LOOKBACK_DAYS)
    supp_held = frozenset(await gap_dates(db, "supplements", lo, today))
    alc_held = frozenset(await gap_dates(db, "alcohol", lo, today))

    return {
        "steps":       _compute_streak(activity["steps"],            steps_goal,    today),
        "move":        _compute_streak(activity["active_calories"],  move_goal,     today),
        "exercise":    _compute_streak(activity["exercise_minutes"], exercise_goal, today),
        "stand":       _compute_streak(activity["stand_hours"],      STAND_GOAL,    today),
        "sleep":       _compute_streak(sleep_by_date,                sleep_goal,    today),
        "protein":     _compute_streak(nut_protein,                  protein_goal,  today, gap_breaks=True, held=held),
        "calories":    _compute_streak(nut_calories, calorie_goal, today, gap_breaks=True, goal_by_day=cal_goal_by_day, held=held),
        "caffeine":    _habit_streak_stats(habit_maps["caffeine"],    today, held),
        "light":       _habit_streak_stats(habit_maps["light"],       today, held),
        "supplements": _habit_streak_stats(habit_maps["supplements"], today, supp_held),
        "creatine":    _habit_streak_stats(habit_maps["creatine"],    today, supp_held),
        "meditation":  _habit_streak_stats(habit_maps["meditation"],  today),
        "walk":        _habit_streak_stats(walk_by_date,              today, held),
        "no_alcohol":  _no_alcohol_streak(alcohol_days, first_alcohol_date, today, alc_held),
        "worked_out":  _habit_streak_stats(workout_by_date,           today, held),
    }


# ── Habit streak helpers ──────────────────────────────────────────────────────

def _no_alcohol_streak(alcohol_days: set, first_track: Optional[date], today: date,
                       held: frozenset = frozenset()) -> dict:
    """
    Consecutive alcohol-free days. Absence of an alcohol record = dry day (mirrors the
    calendar's `no_alcohol`). Counting starts at the first tracked drink so `best` isn't
    inflated by pre-tracking history. `held` = trip days with alcohol not logged (note 102):
    skipped, neither dry nor a drink, unless a drink was logged on one anyway.
    """
    held = frozenset(d for d in held if d not in alcohol_days)
    window_start = today - timedelta(days=LOOKBACK_DAYS)
    start = max(first_track, window_start) if first_track else window_start
    yesterday = today - timedelta(days=1)

    # Current streak: yesterday backwards while dry
    streak = 0
    d = yesterday
    while d >= start:
        if d in alcohol_days:
            break
        if d not in held:
            streak += 1
        d -= timedelta(days=1)

    # Personal best: longest dry run in [start, yesterday]
    best = run = 0
    d = start
    while d <= yesterday:
        if d not in held:
            run = 0 if d in alcohol_days else run + 1
            best = max(best, run)
        d += timedelta(days=1)

    today_dry = today not in alcohol_days
    return {
        "streak": streak, "best": best,
        "today_value": today_dry, "today_on_track": today_dry,
        "gap_days": 0, "goal": None,
    }


def _habit_streak_stats(by_date: dict, today: date, held: frozenset = frozenset()) -> dict:
    """
    Compute current streak (through yesterday) and personal best for a binary habit.
    True = done (counts), False = explicitly failed (breaks streak),
    None = not logged → treated as failure (breaks streak).

    Habits are manually logged, so "didn't log" = "didn't do it".
    There is no gap tolerance here: any unlogged day resets the streak — except
    `held` days (away, for habits that can't be judged abroad), which are skipped.
    """
    yesterday = today - timedelta(days=1)

    # Current streak: walk backwards from yesterday
    streak = 0
    d = yesterday
    for _ in range(LOOKBACK_DAYS):
        if d in held:
            d -= timedelta(days=1)
            continue
        val = by_date.get(d)
        if val is True:
            streak += 1
        else:
            break  # False (explicit fail) or None (unlogged) both break
        d -= timedelta(days=1)

    # Personal best: scan full history for longest run.
    # Date gaps (days absent from the dict) break the streak, same as False/None.
    best = 0
    run = 0
    prev_d = None
    for d in sorted(k for k in by_date.keys() if k not in held):
        val = by_date[d]
        if prev_d is not None and _gap_days(prev_d, d, held) > 0:
            run = 0  # gap in date sequence → streak broken
        if val is True:
            run += 1
            best = max(best, run)
        else:
            run = 0  # False or None both reset
        prev_d = d

    today_val = by_date.get(today)
    return {
        "streak": streak,
        "best":   best,
        "today_value":    today_val,          # True / False / None
        "today_on_track": today_val is True,
        "gap_days": 0,
        "goal": None,
    }


@router.get("/records")
async def get_streak_records(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """
    Returns proactive alerts for habits and metrics where completing
    the action today would match or beat a personal best.
    """
    today = local_today()
    alerts = []

    # ── Habit streak alerts ───────────────────────────────────────────────────

    result = await db.execute(select(HabitLog).order_by(HabitLog.date))
    all_habits = result.scalars().all()

    HABIT_META = [
        ("no_late_caffeine", "No late caffeine", "#f59e0b"),
        ("outdoor_light_am", "Outdoor light",    "#38bdf8"),
        ("supplements",      "Supplements",      "#a78bfa"),
        ("creatine",         "Creatine",         "#fb923c"),
        ("meditation",       "Meditation",       "#10b981"),
    ]

    for habit_key, label, color in HABIT_META:
        by_date = {r.date: getattr(r, habit_key) for r in all_habits}
        today_val = by_date.get(today)

        # Don't alert if already logged today (done or failed)
        if today_val is not None:
            continue

        stats = _habit_streak_stats(by_date, today)
        streak = stats["streak"]
        best   = stats["best"]

        # Minimum personal best of 3 to avoid trivial alerts
        if best < 3:
            continue

        would_be = streak + 1
        if would_be > best:
            message = f"Sets your longest {label.lower()} streak ever ({would_be} days 🔥)"
        elif would_be == best:
            message = f"Ties your {label.lower()} record ({best} days)"
        else:
            continue  # not within 1

        alerts.append({
            "key":     habit_key,
            "type":    "streak",
            "label":   label,
            "color":   color,
            "message": message,
            "streak":  streak,
            "best":    best,
        })

    # ── Monthly workout count alert ───────────────────────────────────────────

    month_start = today.replace(day=1)

    # Count training sessions only — walks tracked separately
    wo_day = local_date_sql(Workout.start_time)
    wo_this_month = (await db.execute(
        select(func.count(Workout.id))
        .where(wo_day >= str(month_start))
        .where(wo_day <= str(today))
        .where(Workout.activity_type.notin_(LOW_INTENSITY_TYPES))
    )).scalar() or 0

    # Personal best: max count in any completed prior month (training only)
    best_wo_row = (await db.execute(
        select(func.count(Workout.id).label("cnt"))
        .where(local_month_sql(Workout.start_time) < today.strftime("%Y-%m"))
        .where(Workout.activity_type.notin_(LOW_INTENSITY_TYPES))
        .group_by(local_month_sql(Workout.start_time))
        .order_by(func.count(Workout.id).desc())
        .limit(1)
    )).first()
    best_wo = best_wo_row.cnt if best_wo_row else 0

    days_remaining = (month_start.replace(
        month=month_start.month % 12 + 1,
        year=month_start.year + (1 if month_start.month == 12 else 0),
    ) - timedelta(days=1)).day - today.day

    needed_wo = best_wo - wo_this_month
    if best_wo >= 5 and needed_wo == 1 and days_remaining >= 0:
        alerts.append({
            "key":     "workout_monthly",
            "type":    "monthly",
            "label":   "Workouts",
            "color":   "#f97316",
            "message": f"One more workout ties your monthly record ({best_wo} 🏋️)",
            "current": wo_this_month,
            "best":    best_wo,
        })
    elif best_wo >= 5 and needed_wo == 0 and days_remaining >= 0:
        alerts.append({
            "key":     "workout_monthly",
            "type":    "monthly",
            "label":   "Workouts",
            "color":   "#f97316",
            "message": f"Any workout today beats your monthly record ({best_wo} 🏋️)",
            "current": wo_this_month,
            "best":    best_wo,
        })

    return {"alerts": alerts}
