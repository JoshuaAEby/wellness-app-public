"""Daily habit tracking — five binary habits, one row per calendar day.

The derived-series computation (bed/wake consistency, morning-daylight credit, the
per-day habit calendar) was extracted to `services/habit_metrics.py` on 2026-08-03 so
non-route callers can reuse it; adherence % moved to `services/adherence.py`. This
router is now a thin caller. The names below are re-exported for import compatibility.
"""
from datetime import date as Date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, decode_token, secret_equals
from app.config import settings
from app.database import get_db
from app.models import HabitGoal, HabitLog, HealthMetric, SleepSession, UserSettings, Workout
from app.services.adherence import DEFAULT_DAYS as ADHERENCE_DEFAULT_DAYS, compute_adherence
from app.services.habit_metrics import (
    BEDTIME_MIN_NIGHTS,
    BEDTIME_THRESHOLD_MIN,
    MORNING_LIGHT_THRESHOLD_MIN,
    MORNING_LIGHT_WINDOW_H,
    SLEPT_WELL_MIN,
    build_calendar,
    morning_light_days as _morning_light_days,
)
from app.timeutils import local_date_sql, local_today, local_now, local_food_date_sql
from app.workout_categories import LOW_INTENSITY_TYPES

router = APIRouter(prefix="/api/habits", tags=["habits"])

# Activity habits eligible for frequency goals (Phase 2). Routine habits are the
# daily check-off list and aren't goal-tracked here.
ACTIVITY_KEYS = ("worked_out", "went_for_walk", "slept_well", "no_alcohol",
                 "consistent_bedtime", "consistent_waketime")

# ── Static habit definitions ──────────────────────────────────────────────────
# Maps stable integer IDs to HabitLog boolean columns.
# To add a habit: add a column migration + one entry here.
_HABITS = [
    {"id": 1, "name": "No Late Caffeine", "icon": "☕", "color": "#34d399", "column": "no_late_caffeine"},
    {"id": 2, "name": "Outdoor Light AM",  "icon": "🌤️", "color": "#60a5fa", "column": "outdoor_light_am"},
    {"id": 3, "name": "Supplements",       "icon": "💊", "color": "#a78bfa", "column": "supplements"},
    {"id": 4, "name": "Creatine",          "icon": "🧪", "color": "#fb923c", "column": "creatine"},
    {"id": 5, "name": "Meditation",        "icon": "🧘", "color": "#f59e0b", "column": "meditation"},
]
_HABIT_BY_ID = {h["id"]: h for h in _HABITS}


# ── Widget token auth (Bearer header or ?token= query param) ──────────────────

def _get_widget_token_or_raise() -> str:
    if not settings.widget_token or settings.widget_token == "changeme":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WIDGET_TOKEN not configured on server",
        )
    return settings.widget_token


async def _widget_or_jwt(
    token: Optional[str] = Query(default=None),
    authorization: str = Header(default=""),
) -> str:
    """Accept WIDGET_TOKEN (Bearer or ?token=) or a valid JWT Bearer token."""
    widget_token = _get_widget_token_or_raise()

    # Query-param path — only widget token accepted
    if token is not None:
        if secret_equals(token, widget_token):
            return "widget"
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    # Authorization header path — try widget token first, then JWT
    if authorization.startswith("Bearer "):
        raw = authorization[len("Bearer "):]
        if secret_equals(raw, widget_token):
            return "widget"
        return decode_token(raw)  # raises 401 if invalid JWT

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

HABIT_KEYS = ("no_late_caffeine", "outdoor_light_am", "supplements", "creatine", "meditation")

# SLEPT_WELL_MIN (450 = 7.5h) now lives in services/habit_metrics.py and is imported
# above — it is used by both the calendar builder and the goals endpoint.


def _serialize(row: HabitLog) -> dict:
    return {
        "date":             row.date.isoformat(),
        "no_late_caffeine": row.no_late_caffeine,
        "outdoor_light_am": row.outdoor_light_am,
        "supplements":      row.supplements,
        "creatine":         row.creatine,
        "meditation":       row.meditation,
        "caffeine_last_min": row.caffeine_last_min,
        "meditation_min":   row.meditation_min,
        "notes":            row.notes,
    }


class HabitUpdate(BaseModel):
    date:             Optional[Date] = None
    no_late_caffeine: Optional[bool] = None
    outdoor_light_am: Optional[bool] = None
    supplements:      Optional[bool] = None
    creatine:         Optional[bool] = None
    meditation:       Optional[bool] = None
    caffeine_last_min: Optional[int] = None
    meditation_min:   Optional[int]  = None
    notes:            Optional[str]  = None


@router.get("/summary")
async def get_habits_summary(
    _: str = Depends(_widget_or_jwt),
    db: AsyncSession = Depends(get_db),
):
    """
    Aggregated habit snapshot for the iOS widget.
    Accepts WIDGET_TOKEN (Bearer or ?token=) or a valid JWT.
    """
    _tz = ZoneInfo(settings.tz)
    today: Date = local_today()
    yesterday = today - timedelta(days=1)

    # ── synced_minutes_ago (same logic as widgets/summary) ────────────────────
    now_utc = datetime.now(ZoneInfo("UTC")).replace(tzinfo=None)
    last_ts_result = await db.execute(select(func.max(HealthMetric.timestamp)))
    last_ts = last_ts_result.scalar()
    synced_minutes_ago = (
        int((now_utc - last_ts).total_seconds() / 60) if last_ts else None
    )

    # ── today's HabitLog row ──────────────────────────────────────────────────
    result = await db.execute(select(HabitLog).where(HabitLog.date == today))
    today_row = result.scalar_one_or_none()

    # ── build per-habit output ────────────────────────────────────────────────
    habits_out = []
    for habit in _HABITS:
        col = habit["column"]
        completed = bool(getattr(today_row, col)) if today_row else False

        # Streak: consecutive True days ending on yesterday
        streak = 0
        check_date = yesterday
        while True:
            r = await db.execute(select(HabitLog).where(HabitLog.date == check_date))
            row = r.scalar_one_or_none()
            if row and getattr(row, col):
                streak += 1
                check_date -= timedelta(days=1)
            else:
                break

        habits_out.append({
            "id":        habit["id"],
            "name":      habit["name"],
            "icon":      habit["icon"],
            "color":     habit["color"],
            "completed": completed,
            "streak":    streak,
        })

    return {"habits": habits_out, "synced_minutes_ago": synced_minutes_ago}


@router.post("/{habit_id}/toggle")
async def toggle_habit(
    habit_id: int,
    _: str = Depends(_widget_or_jwt),
    db: AsyncSession = Depends(get_db),
):
    """
    Toggle a habit for today (America/New_York).
    If currently True/unset → sets True (complete).
    If currently True → sets False (incomplete).
    Accepts WIDGET_TOKEN (Bearer or ?token=) or a valid JWT.
    """
    habit = _HABIT_BY_ID.get(habit_id)
    if habit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Habit not found")

    _tz = ZoneInfo(settings.tz)
    today: Date = local_today()
    col = habit["column"]

    result = await db.execute(select(HabitLog).where(HabitLog.date == today))
    existing = result.scalar_one_or_none()

    current = bool(getattr(existing, col)) if existing else False
    new_value = not current

    values = {"date": today, col: new_value}
    # Preserve all other columns from existing row
    if existing:
        for other in _HABITS:
            if other["column"] != col:
                values.setdefault(other["column"], getattr(existing, other["column"]))

    stmt = sqlite_insert(HabitLog).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["date"],
        set_={col: new_value},
    )
    await db.execute(stmt)
    await db.commit()

    return {
        "id":        habit["id"],
        "name":      habit["name"],
        "completed": new_value,
        "date":      today.isoformat(),
    }


@router.get("")
async def get_habits(
    date: Date = Query(default_factory=Date.today),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Return the habit log for a given date, or all-null if no entry exists yet."""
    result = await db.execute(select(HabitLog).where(HabitLog.date == date))
    row = result.scalar_one_or_none()
    out = _serialize(row) if row else {
        "date":             date.isoformat(),
        "no_late_caffeine": None,
        "outdoor_light_am": None,
        "supplements":      None,
        "creatine":         None,
        "meditation":       None,
        "notes":            None,
    }
    # Auto-credit outdoor light from morning daylight (manual still wins)
    if out.get("outdoor_light_am") is not True and date in await _morning_light_days(db, date, date):
        out["outdoor_light_am"] = True
    # If the credit window has closed (past days, or today past cutoff) and outdoor light is still
    # not credited → mark as failed. The opportunity is gone; no credit = didn't achieve it.
    if out.get("outdoor_light_am") is not True:
        _tz = ZoneInfo(settings.tz)
        _now = local_now()
        _cutoff = (await db.execute(select(UserSettings.light_cutoff_hour).limit(1))).scalar() or 10
        if date < _now.date() or (_now.date() == date and _now.hour >= _cutoff):
            out["outdoor_light_am"] = False
    # no_late_caffeine defaults to True (assume no late caffeine unless explicitly failed).
    # Stored False means "explicitly failed". Null/True both display as done.
    if out.get("no_late_caffeine") is not False:
        out["no_late_caffeine"] = True
    return out


@router.post("")
async def upsert_habits(
    body: HabitUpdate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """
    Upsert habit log for a date. Only supplied fields are written;
    existing values for omitted fields are preserved.
    """
    entry_date = body.date or Date.today()

    result = await db.execute(select(HabitLog).where(HabitLog.date == entry_date))
    existing = result.scalar_one_or_none()

    # Use model_fields_set so an explicit null ("field": null) clears the value,
    # while an absent field preserves the existing value.
    def _pick(attr):
        if attr in body.model_fields_set:
            return getattr(body, attr)          # explicit value, even if None
        return getattr(existing, attr) if existing else None  # preserve / default

    values = {
        "date":             entry_date,
        "no_late_caffeine": _pick("no_late_caffeine"),
        "outdoor_light_am": _pick("outdoor_light_am"),
        "supplements":      _pick("supplements"),
        "creatine":         _pick("creatine"),
        "meditation":       _pick("meditation"),
        "caffeine_last_min": _pick("caffeine_last_min"),
        "meditation_min":   _pick("meditation_min"),
        "notes":            _pick("notes"),
    }

    # A logged time IMPLIES the occurrence: setting a caffeine time derives
    # no_late_caffeine from the cutoff; setting a meditation time marks meditated.
    if "caffeine_last_min" in body.model_fields_set and body.caffeine_last_min is not None:
        cutoff = (await db.execute(
            select(UserSettings.caffeine_cutoff_hour).limit(1))).scalar() or 14
        values["no_late_caffeine"] = body.caffeine_last_min <= cutoff * 60
    if "meditation_min" in body.model_fields_set and body.meditation_min is not None:
        values["meditation"] = True

    stmt = sqlite_insert(HabitLog).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["date"],
        set_={k: v for k, v in values.items() if k != "date"},
    )
    await db.execute(stmt)
    await db.commit()

    result = await db.execute(select(HabitLog).where(HabitLog.date == entry_date))
    return _serialize(result.scalar_one())


@router.get("/consistency-targets")
async def get_consistency_targets(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)):
    """Tonight's bed / wake window: reference median ± threshold, the nights behind it,
    and last night's result (Behaviors → tap the 🛏️ / 🌅 row)."""
    from app.services.habit_metrics import consistency_targets
    return await consistency_targets(db)


@router.get("/calendar")
async def get_habits_calendar(
    start: Date = Query(...),
    end: Date = Query(...),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """
    Return per-day habit data for a date range including derived binaries:
    worked_out, slept_well, drank_alcohol.
    Manual habits are null when not logged; derived are always bool.

    Computation lives in services/habit_metrics.build_calendar so non-route callers
    (gamification, adherence) share one implementation.
    """
    return await build_calendar(db, start, end)


@router.get("/adherence")
async def get_habits_adherence(
    days: int = Query(default=ADHERENCE_DEFAULT_DAYS, ge=7, le=365),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Rolling adherence % per habit + the variance monitor flags.

    Split denominator: manual habits over CALENDAR days (unlogged = miss), auto-derived
    habits over JUDGEABLE days (null = excluded). See services/adherence.py.
    """
    today = local_today()
    return await compute_adherence(db, end=today, days=days)


@router.get("/history")
async def get_habit_history(
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Return habit logs for the last N days, oldest first."""
    since = Date.today() - timedelta(days=days)
    result = await db.execute(
        select(HabitLog)
        .where(HabitLog.date >= since)
        .order_by(HabitLog.date)
    )
    return [_serialize(r) for r in result.scalars().all()]


# ── Activity frequency goals (Phase 2) ────────────────────────────────────────

async def _activity_count(db: AsyncSession, key: str, start: Date, end: Date, slept_well_min: int) -> int:
    """Count days in [start, end] (inclusive) on which the activity occurred."""
    s, e = str(start), str(end)
    wo_day = local_date_sql(Workout.start_time)
    hm_day = local_date_sql(HealthMetric.timestamp)
    if key in ("worked_out", "went_for_walk"):
        cond = (Workout.activity_type.in_(LOW_INTENSITY_TYPES) if key == "went_for_walk"
                else Workout.activity_type.notin_(LOW_INTENSITY_TYPES))
        n = (await db.execute(
            select(func.count(func.distinct(wo_day)))
            .where(cond)
            .where(wo_day >= s).where(wo_day <= e)
        )).scalar()
        return n or 0
    if key == "no_alcohol":
        alc_day = local_food_date_sql(HealthMetric.timestamp)     # 04:00 food day
        drink_days = (await db.execute(
            select(func.count(func.distinct(alc_day)))
            .where(HealthMetric.metric_type == "alcohol").where(HealthMetric.value > 0)
            .where(alc_day >= s).where(alc_day <= e)
        )).scalar() or 0
        elapsed = (end - start).days + 1
        return max(0, elapsed - drink_days)
    if key == "slept_well":
        n = (await db.execute(
            select(func.count()).select_from(SleepSession)
            .where(SleepSession.date >= start).where(SleepSession.date <= end)
            .where(SleepSession.total_min >= slept_well_min)
        )).scalar()
        return n or 0
    if key in ("consistent_bedtime", "consistent_waketime"):
        # Counted from the calendar's own verdicts (2026-10-22) so goal progress can
        # never disagree with the Behaviors grid. The copy that lived here kept travel
        # nights IN the baseline and had no post-trip fallback, unlike the calendar.
        return sum(1 for r in await build_calendar(db, start, end) if r.get(key) is True)
    return 0


def _period_start(period: str, today: Date) -> Date:
    if period == "week":
        return today - timedelta(days=today.weekday())  # Monday of this week
    return today.replace(day=1)                          # first of this month


@router.get("/goals")
async def get_habit_goals(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Return each configured Activity goal with live current-period progress."""
    s = (await db.execute(select(UserSettings).limit(1))).scalar_one_or_none()
    slept_well_min = int(s.target_sleep_hours * 60) if s and s.target_sleep_hours else SLEPT_WELL_MIN
    today = local_today()

    goals = (await db.execute(select(HabitGoal))).scalars().all()
    out: dict[str, dict] = {}
    for g in goals:
        start = _period_start(g.period, today)
        current = await _activity_count(db, g.habit_key, start, today, slept_well_min)
        out[g.habit_key] = {
            "habit_key":    g.habit_key,
            "target":       g.target,
            "period":       g.period,
            "current":      current,
            "met":          current >= g.target,
            "period_label": "this week" if g.period == "week" else "this month",
        }
    return out


class GoalIn(BaseModel):
    target: int = Field(ge=1, le=31)
    period: str  # 'week' | 'month'


@router.put("/goals/{habit_key}")
async def set_habit_goal(
    habit_key: str,
    body: GoalIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Create or update the frequency goal for an Activity habit."""
    if habit_key not in ACTIVITY_KEYS:
        raise HTTPException(status_code=404, detail="Unknown activity habit")
    if body.period not in ("week", "month"):
        raise HTTPException(status_code=422, detail="period must be 'week' or 'month'")

    stmt = sqlite_insert(HabitGoal).values(habit_key=habit_key, target=body.target, period=body.period)
    stmt = stmt.on_conflict_do_update(
        index_elements=["habit_key"],
        set_={"target": body.target, "period": body.period},
    )
    await db.execute(stmt)
    await db.commit()
    return {"habit_key": habit_key, "target": body.target, "period": body.period}


@router.delete("/goals/{habit_key}")
async def delete_habit_goal(
    habit_key: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Remove the frequency goal for an Activity habit."""
    await db.execute(delete(HabitGoal).where(HabitGoal.habit_key == habit_key))
    await db.commit()
    return {"ok": True}
