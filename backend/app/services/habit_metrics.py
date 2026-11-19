"""Habit-derived daily series — the single source of truth for habit computation.

Extracted from `routers/habits.py` 2026-08-03 so that non-route callers (the
gamification layer, and anything else that needs the derived habit series) can
import it without going through HTTP. The router now delegates here; behaviour is
unchanged.

Contents:
  * bed/wake-time consistency (the Sleep Regularity Index idea)
  * morning-daylight auto-credit for `outdoor_light_am`
  * `build_calendar()` — the per-day habit matrix (manual + derived binaries)
"""
import statistics as st
from datetime import date as Date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    HabitLog,
    HealthMetric,
    SleepSession,
    UserSettings,
    WeatherDaily,
    Workout,
)
from app.timeutils import local_date_sql, local_hour_sql, local_now, to_local, in_away_segment, zone_for_date, local_food_date_sql
from app.workout_categories import LOW_INTENSITY_TYPES

# ── Bed/wake-time consistency (sleep regularity) ──────────────────────────────
# A night is "consistent" if its bed/wake time is within ±THRESHOLD of the rolling
# MEDIAN over the prior WINDOW nights. Measures *regularity* vs your own pattern (the
# Sleep Regularity Index idea), not earliness. Wake-time regularity anchors
# circadian phase and is arguably the higher-leverage of the two.
BEDTIME_WINDOW = 14
BEDTIME_THRESHOLD_MIN = 60
BEDTIME_MIN_NIGHTS = 4
# Fallback when the 14-night window holds fewer than BEDTIME_MIN_NIGHTS usable nights —
# after a trip, whose nights are kept out of the baseline, that is the whole first week
# home (2026-10-22: every bed/wake cell blank 10-20 → 10-23). Then the reference is the
# most recent BEDTIME_WINDOW usable nights within BEDTIME_FALLBACK_DAYS. Only nights that
# were unjudgeable change; a night with ≥4 nights in its window keeps its exact baseline.
BEDTIME_FALLBACK_DAYS = 45

# The reference point is the MEDIAN of the prior nights, not the mean (changed
# 2026-08-06). `wake_time` is the end of a sleep SESSION, which is not "when you woke
# up" whenever the watch fragments a night or stitches a nap onto one — real cases:
# a night ending 01:16, and a night+nap spanning 16h whose session ended 15:38. A
# single such record shifts a 14-night mean by ~40 min and can therefore flip the
# consistency verdict for the following two weeks, contaminating the habit calendar,
# streaks, the Discipline stat and the consistent_* correlation inputs. The median is
# unmoved by one bad night.

# ── Morning daylight → auto-credit outdoor_light_am ───────────────────────────
# Apple "time_in_daylight" arrives as timestamped per-minute samples (units=min), so we
# can sum daylight *in the morning window* and auto-credit the manual habit when it
# clears the threshold. Manual logging still works (effective = manual True OR auto).
#
# The window is SUNRISE-RELATIVE when we know that day's sunrise (weather_daily,
# collected from 2026-07-18 on): [sunrise, sunrise + MORNING_LIGHT_WINDOW_H]. That's
# the circadian-meaningful anchor — a fixed clock hour is seasonally inconsistent
# (sunrise swings ~1h45m here between summer and winter, so a 10am cutoff grants a
# much wider effective window in December than in June).
# Days with no sunrise on record (i.e. all history before weather collection began)
# fall back to the original fixed `light_cutoff_hour`, so historical habit values and
# streaks are unchanged.
MORNING_LIGHT_THRESHOLD_MIN = 10.0
MORNING_LIGHT_WINDOW_H = 3.0

SLEPT_WELL_MIN = 450  # 7.5 hours

# ANY recorded mindful session counts. The first pass set this at 5 min to exclude
# Apple Watch "Breathe" prompts, but the sub-3-minute entries turned out to be Headspace
# "take a breath" sessions — deliberate practice the user considers real, just short.
# Excluding them would have thrown away most of the history to protect against a problem
# that was not there. Strictly greater than zero, so a 0-minute artefact still cannot
# create a meditation day.
MEDITATION_MIN_MIN = 0.0



def bedtime_minutes_local(bedtime: datetime, tz: Optional[ZoneInfo] = None) -> int:
    """Bedtime (naive UTC) → minutes since 18:00 local (keeps evening→early-AM contiguous).

    tz=None (the default since tz phase 4) = the zone the user was in that night, so a
    22:30 bedtime abroad reads as 22:30, not as 15:30 Eastern."""
    local = to_local(bedtime) if tz is None else bedtime.replace(tzinfo=timezone.utc).astimezone(tz)
    return ((local.hour - 18) % 24) * 60 + local.minute


def waketime_minutes_local(wake_time: datetime, tz: Optional[ZoneInfo] = None) -> int:
    """Wake time (naive UTC) → minutes since local midnight. Morning wake times
    (~05:00–11:00) don't cross midnight, so a midnight anchor is contiguous with no
    wrap distortion (unlike bedtime, which needs the 18:00 anchor)."""
    local = to_local(wake_time) if tz is None else wake_time.replace(tzinfo=timezone.utc).astimezone(tz)
    return local.hour * 60 + local.minute


def _prior_nights(by_date: dict, target_date: Date) -> list:
    """Reference nights for target_date: the previous BEDTIME_WINDOW calendar nights, or —
    when those hold fewer than BEDTIME_MIN_NIGHTS — the most recent BEDTIME_WINDOW nights
    within BEDTIME_FALLBACK_DAYS (see the constant). `by_date` already excludes away nights."""
    return [by_date[d] for d in _prior_dates_for(by_date, target_date)]


def consistent_bedtime(bedtimes_by_date: dict, target_date: Date, tz: Optional[ZoneInfo] = None) -> Optional[bool]:
    """True/False if judgeable, else None (no sleep that night or too little history)."""
    tonight = bedtimes_by_date.get(target_date)
    if tonight is None:
        return None
    prior = [bedtime_minutes_local(bt, tz) for bt in _prior_nights(bedtimes_by_date, target_date)]
    if len(prior) < BEDTIME_MIN_NIGHTS:
        return None
    ref = st.median(prior)
    return abs(bedtime_minutes_local(tonight, tz) - ref) <= BEDTIME_THRESHOLD_MIN


def consistent_waketime(waketimes_by_date: dict, target_date: Date, tz: Optional[ZoneInfo] = None) -> Optional[bool]:
    """Wake-time analogue of consistent_bedtime. None when unjudgeable."""
    this = waketimes_by_date.get(target_date)
    if this is None:
        return None
    prior = [waketime_minutes_local(wt, tz) for wt in _prior_nights(waketimes_by_date, target_date)]
    if len(prior) < BEDTIME_MIN_NIGHTS:
        return None
    ref = st.median(prior)
    return abs(waketime_minutes_local(this, tz) - ref) <= BEDTIME_THRESHOLD_MIN


async def morning_light_days(db: AsyncSession, start: Date, end: Date) -> set:
    """Local dates in [start, end] that earned the morning-daylight credit.

    Sunrise-relative window where sunrise is known; fixed-hour cutoff otherwise.

    The windowing runs in SQL (2026-10-21, second pass). Each local day's morning window
    is built here in naive UTC — [sunrise, sunrise + MORNING_LIGHT_WINDOW_H] clipped to
    the day, or [local midnight, local midnight + cutoff) — and SQLite sums the daylight
    samples inside each window off the (metric_type, timestamp) index. The previous
    version pulled every per-minute sample of the range into Python (~35k rows for the
    365-day streaks window, ~210 ms per /streaks request). Results verified identical to
    it over the full history, a long trip abroad included.

    Bounds are literal 'YYYY-MM-DD HH:MM:SS' strings, never bound datetimes: a bound
    datetime renders with microseconds and silently drops on-the-boundary samples
    against the 19-char stored text. An inclusive upper edge becomes an exclusive one
    second later (samples are stored at second resolution).
    """
    from sqlalchemy import text

    from app.timeutils import local_day_bounds_utc

    cutoff = (await db.execute(select(UserSettings.light_cutoff_hour).limit(1))).scalar() or 10

    # Sunrise per local day, kept in naive UTC like the samples.
    sun_rows = (await db.execute(
        select(WeatherDaily.date, WeatherDaily.sunrise)
        .where(WeatherDaily.date >= start - timedelta(days=1))
        .where(WeatherDaily.date <= end + timedelta(days=1))
    )).all()
    sunrise_by_day = {d: sr for d, sr in sun_rows if sr is not None}

    # Same sample range the per-row version read: [start-1 00:00, end+1 23:59:59] UTC.
    rng_lo = datetime.combine(start - timedelta(days=1), time.min)
    rng_hi = datetime.combine(end + timedelta(days=2), time.min)          # exclusive
    window = timedelta(hours=MORNING_LIGHT_WINDOW_H)
    second = timedelta(seconds=1)
    fmt = lambda dt: dt.strftime("%Y-%m-%d %H:%M:%S")

    windows = []
    for i in range((end - start).days + 6):
        day = start - timedelta(days=2) + timedelta(days=i)
        d0, d1 = local_day_bounds_utc(day)
        # weather_daily's sunrise is for the HOME coordinates, so abroad it means
        # nothing — fall back to the fixed-hour rule there (tz phase 4). Away segments
        # start and end on local midnights, so a whole local day is in or out.
        sunrise = None if in_away_segment(d0) else sunrise_by_day.get(day)
        if sunrise is not None:
            lo, hi = max(sunrise, d0), min(sunrise + window + second, d1)
        else:
            # Local midnight + cutoff, in UTC. Off by an hour only on a DST-change
            # morning (as before).
            lo, hi = d0, min(d0 + timedelta(hours=cutoff), d1)
        lo, hi = max(lo, rng_lo), min(hi, rng_hi)
        if lo < hi:
            windows.append(f"('{day.isoformat()}', '{fmt(lo)}', '{fmt(hi)}')")
    if not windows:
        return set()

    rows = (await db.execute(text(
        "WITH w(day, lo, hi) AS (VALUES " + ", ".join(windows) + ") "
        "SELECT w.day, SUM(h.value) FROM w JOIN health_metrics h "
        "ON h.metric_type = 'time_in_daylight' AND h.timestamp >= w.lo AND h.timestamp < w.hi "
        "GROUP BY w.day"
    ))).all()
    return {Date.fromisoformat(d) for d, total in rows if (total or 0.0) >= MORNING_LIGHT_THRESHOLD_MIN}


async def consistency_targets(db: AsyncSession) -> dict:
    """What tonight has to hit to count as a consistent bedtime / wake time (2026-10-22,
    The user: "show me the current central value and error bar").

    Tonight's sleep is keyed by its WAKE date (tomorrow), so the reference is the same
    median build_calendar would use for tomorrow — same baseline (travel nights left out,
    14-night window or the recent-home-nights fallback), same ±BEDTIME_THRESHOLD_MIN.
    Clock times are local where you were each night."""
    from app.services.day_flags import away_dates
    from app.timeutils import local_today
    today = local_today()
    target = today + timedelta(days=1)
    start = target - timedelta(days=BEDTIME_FALLBACK_DAYS)
    rows = (await db.execute(
        select(SleepSession.date, SleepSession.bedtime, SleepSession.wake_time)
        .where(SleepSession.date >= start).where(SleepSession.date <= today)
    )).all()
    away = await away_dates(db, start, today)
    out = {"for_wake_date": target.isoformat(), "threshold_min": BEDTIME_THRESHOLD_MIN,
           "window_nights": BEDTIME_WINDOW}
    for kind, idx, to_min, judge in (("bedtime", 1, bedtime_minutes_local, consistent_bedtime),
                                     ("waketime", 2, waketime_minutes_local, consistent_waketime)):
        by_date = {r[0]: r[idx] for r in rows if r[idx] is not None and r[0] not in away}
        in_window = sum(1 for i in range(1, BEDTIME_WINDOW + 1) if (target - timedelta(days=i)) in by_date)
        prior_dates = _prior_dates_for(by_date, target)
        mins = [to_min(by_date[d]) for d in prior_dates]
        if len(mins) < BEDTIME_MIN_NIGHTS:
            out[kind] = None
            continue
        ref = st.median(mins)
        # bedtime minutes are since 18:00 (evening→early-AM contiguous); wake since 00:00
        clock = (lambda m: (18 * 60 + m) % 1440) if kind == "bedtime" else (lambda m: m % 1440)
        last = {r[0]: r for r in rows}.get(today)
        last_val = last[idx] if last and last[idx] is not None else None
        out[kind] = {
            "ref_clock_min": round(clock(ref)),
            "lo_clock_min": round(clock(ref - BEDTIME_THRESHOLD_MIN)),
            "hi_clock_min": round(clock(ref + BEDTIME_THRESHOLD_MIN)),
            "n": len(mins),
            "basis": "last_14_nights" if in_window >= BEDTIME_MIN_NIGHTS else "recent_home_nights",
            # Offsets from the reference, minutes (+ = later) — for the dot plot.
            "nights": [{"date": d.isoformat(), "offset_min": round(to_min(by_date[d]) - ref)}
                       for d in sorted(prior_dates)],
            "last_night": None if last_val is None else {
                "date": today.isoformat(),
                "clock_min": round(clock(to_min(last_val))),
                "offset_min": round(to_min(last_val) - st.median(
                    [to_min(by_date[d]) for d in _prior_dates_for(by_date, today)] or [to_min(last_val)])),
                "consistent": judge({**by_date, today: last_val}, today),
            },
        }
    return out


def _prior_dates_for(by_date: dict, target_date: Date) -> list:
    """Dates of the reference nights _prior_nights would use for target_date."""
    win = [target_date - timedelta(days=i) for i in range(1, BEDTIME_WINDOW + 1)]
    have = [d for d in win if d in by_date and by_date[d] is not None]
    if len(have) >= BEDTIME_MIN_NIGHTS:
        return have
    return sorted((d for d in by_date if target_date - timedelta(days=BEDTIME_FALLBACK_DAYS) <= d < target_date
                   and by_date[d] is not None), reverse=True)[:BEDTIME_WINDOW]


async def build_calendar(db: AsyncSession, start: Date, end: Date) -> list[dict]:
    """Per-day habit data for a date range including derived binaries:
    worked_out, slept_well, drank_alcohol.
    Manual habits are null when not logged; derived are always bool.
    """
    # "Slept well" uses the user's sleep target so it matches the Sleep streak counter
    # (rather than a hardcoded 7.5h). Falls back to SLEPT_WELL_MIN if no target set.
    _settings = (await db.execute(select(UserSettings).limit(1))).scalar_one_or_none()
    slept_well_min = (
        int(_settings.target_sleep_hours * 60)
        if _settings and _settings.target_sleep_hours else SLEPT_WELL_MIN
    )
    _light_cutoff = (_settings.light_cutoff_hour or 10) if _settings else 10
    _tz = ZoneInfo(settings.tz)
    _now_local = local_now()

    # ── Manual habits ─────────────────────────────────────────────────────────
    result = await db.execute(
        select(HabitLog)
        .where(HabitLog.date >= start)
        .where(HabitLog.date <= end)
    )
    habit_rows: dict[Date, HabitLog] = {r.date: r for r in result.scalars().all()}

    # ── Workouts ──────────────────────────────────────────────────────────────
    wo_day = local_date_sql(Workout.start_time)
    result = await db.execute(
        select(wo_day.label("day"), Workout.activity_type)
        .where(wo_day >= str(start))
        .where(wo_day <= str(end))
    )
    all_workout_rows = result.all()
    # Real training sessions vs walks/active recovery tracked separately
    workout_days: set[str] = {
        r.day for r in all_workout_rows
        if (r.activity_type or "") not in LOW_INTENSITY_TYPES
    }
    walk_days: set[str] = {
        r.day for r in all_workout_rows
        if (r.activity_type or "") in LOW_INTENSITY_TYPES
    }

    # ── Sleep (fetch extra prior nights so bedtime consistency has a baseline) ──
    result = await db.execute(
        select(SleepSession)
        .where(SleepSession.date >= start - timedelta(days=BEDTIME_FALLBACK_DAYS))
        .where(SleepSession.date <= end)
    )
    sleep_rows: dict[Date, SleepSession] = {r.date: r for r in result.scalars().all()}
    # Away nights are left out of the bed/wake consistency baseline as well as being
    # unjudged themselves: abroad they read as 15:00 "bedtimes" in Eastern time, and
    # would wreck the median the first week home is compared against (2026-10-19).
    from app.models import TzPeriod
    from app.services.day_flags import AWAY_UNKNOWN_HABITS, away_dates
    away = await away_dates(db, start - timedelta(days=BEDTIME_FALLBACK_DAYS), end)
    # Away days whose ZONE is known (tz phase 4): a manual tz period covers them, or an
    # away segment covers their midday. Those are judged in local time; away days with
    # an unknown zone keep the clock-dependent habits blank.
    zone_known: set = set()
    for p in (await db.execute(
        select(TzPeriod).where(TzPeriod.end_date >= start).where(TzPeriod.start_date <= end)
    )).scalars().all():
        d = max(p.start_date, start)
        while d <= min(p.end_date, end):
            zone_known.add(d); d += timedelta(days=1)
    zone_known |= {d for d in away if getattr(zone_for_date(d), "key", None) != settings.tz}
    bedtimes_by_date = {d: s.bedtime for d, s in sleep_rows.items()
                        if s.bedtime is not None and d not in away}
    waketimes_by_date = {d: s.wake_time for d, s in sleep_rows.items()
                         if s.wake_time is not None and d not in away}

    # Auto-credit outdoor light from morning daylight (manual logging still wins)
    morning_light = await morning_light_days(db, start, end)

    # ── Alcohol — by the 04:00 FOOD day (a 00:30 drink is the night before's) ──
    alc_day = local_food_date_sql(HealthMetric.timestamp)
    result = await db.execute(
        select(
            alc_day.label("day"),
            func.sum(HealthMetric.value).label("total"),
        )
        .where(HealthMetric.metric_type == "alcohol")
        .where(alc_day >= str(start))
        .where(alc_day <= str(end))
        .group_by(alc_day)
    )
    alcohol_days: set[str] = {r.day for r in result.all() if (r.total or 0) > 0}
    # Trip days he said alcohol wasn't logged (note 102): unknown, not dry.
    from app.services.trip_gaps import gap_dates
    alcohol_gaps = await gap_dates(db, "alcohol", start, end)
    hm_day = local_date_sql(HealthMetric.timestamp)

    # ── Mindful minutes (Apple; HKMindfulSession) ─────────────────────────────
    #
    # Verified 2026-08-19 against a known session: two back-to-back 5-minute sits
    # arrived as TWO 5.0-minute rows five minutes apart. So ONE ROW = ONE SESSION —
    # value is that session's duration and the timestamp is its start. Extraction is
    # correct; the "may need a custom path" warning in ingestion is stale.
    #
    # (First reading of this data assumed HAE was chunking a single 10-minute sit into
    # 5-minute segments. It was not — the user had simply done two sessions. Recorded
    # because the two explanations look identical in the data and imply different things
    # about whether row counts and per-row durations mean anything. They do.)
    #
    # Any recorded session counts — the short entries are Headspace "take a breath"
    # sits, not stray Watch prompts, so gating them out would discard real practice.
    result = await db.execute(
        select(
            hm_day.label("day"),
            func.sum(HealthMetric.value).label("total"),
        )
        .where(HealthMetric.metric_type == "mindful_minutes")
        .where(hm_day >= str(start))
        .where(hm_day <= str(end))
        .group_by(hm_day)
    )
    mindful_min_by_day: dict[str, float] = {r.day: (r.total or 0.0) for r in result.all()}

    # ── Caffeine timing (from the instance log) ───────────────────────────────
    #
    # `no_late_caffeine` used to be a manual toggle defaulting to True. Per-instance
    # caffeine logging now records the actual time of every dose, so the answer is
    # derivable: the latest dose of the day against the cutoff.
    #
    # Only days that HAVE caffeine entries are derived. Absence is ambiguous — it could
    # mean no caffeine or no logging — and everything before 2026-08-03 predates the
    # instance log entirely, so those days keep the old default-True behaviour rather
    # than being blanked. Coverage since logging began is 16/16 days, so in practice the
    # derived branch is the one that runs.
    from app.models import CaffeineLog
    caf_cutoff = (await db.execute(
        select(UserSettings.caffeine_cutoff_hour).limit(1))).scalar() or 14
    caf_day = local_date_sql(CaffeineLog.timestamp)
    result = await db.execute(
        select(caf_day.label("day"), func.max(local_hour_sql(CaffeineLog.timestamp)).label("latest"))
        .where(caf_day >= str(start)).where(caf_day <= str(end))
        .group_by(caf_day)
    )
    caffeine_latest_hour: dict[str, int] = {
        str(r.day): int(r.latest) for r in result.all() if r.latest is not None
    }

    from app.services.fluids import goal_met_days
    fluids_goal = await goal_met_days(db, start, end)

    # ── Build response ────────────────────────────────────────────────────────
    output = []
    cur = start
    while cur <= end:
        date_str = cur.isoformat()
        habit = habit_rows.get(cur)
        sleep = sleep_rows.get(cur)

        output.append({
            "date":             date_str,
            # Manual (null = not logged that day)
            # Manual value wins (Behaviors calendar can still correct a day); else
            # derive from the last dose logged; else the historical default.
            "no_late_caffeine": (
                habit.no_late_caffeine if (habit and habit.no_late_caffeine is not None)
                else (caffeine_latest_hour[date_str] < caf_cutoff)
                if date_str in caffeine_latest_hour else True
            ),
            "caffeine_last_hour_actual": caffeine_latest_hour.get(date_str),
            "outdoor_light_am": (
                True if ((habit and habit.outdoor_light_am is True) or cur in morning_light)
                # Past days or today past cutoff: window closed → not credited = failed
                else False if (cur < _now_local.date() or (cur == _now_local.date() and _now_local.hour >= _light_cutoff))
                else (habit.outdoor_light_am if habit else None)
            ),
            "supplements":      habit.supplements      if habit else None,
            "creatine":         habit.creatine         if habit else None,
            # Auto-derived from Apple, with the manual checkbox kept as an OVERRIDE:
            # an explicit tap still wins, so an unrecorded off-phone sit can be logged.
            # Ordering matters — deriving first would silently discard the manual entry.
            "meditation": (
                habit.meditation if (habit and habit.meditation is not None)
                else (mindful_min_by_day.get(date_str, 0.0) > MEDITATION_MIN_MIN
                      if date_str in mindful_min_by_day else None)
            ),
            "mindful_min": round(mindful_min_by_day.get(date_str, 0.0), 1) or None,
            # Derived. worked_out/walk/alcohol are "absence of record = didn't
            # happen" — you know you didn't work out if there's no workout. Sleep
            # is different: no sleep record means the WATCH DIDN'T CAPTURE it (dead
            # battery, not worn), which is unknown, not "slept poorly". Return None
            # so it renders as untracked rather than a false-negative red ✗.
            "worked_out":    date_str in workout_days,
            "went_for_walk": date_str in walk_days,
            "slept_well":    (sleep.total_min >= slept_well_min
                              if (sleep and sleep.total_min) else None),
            "no_alcohol":    (None if cur in alcohol_gaps and date_str not in alcohol_days
                              else date_str not in alcohol_days),
            # Food day, like the card. None = water not logged, not a miss.
            "fluids_goal":   fluids_goal.get(cur),
            # Zone-aware (tz phase 4). Tonight is judged even when away — in local time
            # it is comparable — but away nights never enter the baseline (see above).
            "consistent_bedtime": consistent_bedtime(
                {**bedtimes_by_date, cur: sleep.bedtime} if (sleep and sleep.bedtime) else bedtimes_by_date, cur),
            "consistent_waketime": consistent_waketime(
                {**waketimes_by_date, cur: sleep.wake_time} if (sleep and sleep.wake_time) else waketimes_by_date, cur),
            "away": cur in away,
        })
        if cur in away:
            # An explicit manual value always wins — a tap on the Behaviors grid is a
            # statement about the day.
            row = output[-1]
            if cur in zone_known:
                # Zone known → clock habits are judged in local time. Only late caffeine
                # stays unknown when nothing was logged: its fallback is a default-True.
                blank = () if date_str in caffeine_latest_hour else ("no_late_caffeine",)
            else:
                blank = AWAY_UNKNOWN_HABITS     # zone unknown → can't read the clock
            for key in blank:
                manual = getattr(habit, key, None) if habit else None
                row[key] = manual if manual is not None else None
        cur += timedelta(days=1)

    return output
