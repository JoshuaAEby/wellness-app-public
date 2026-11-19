"""Day-exclusion logic — the single source of truth for 'is this day analysable'.

A day is excluded from the correlation series and from experiment eligibility if
it carries ANY DayFlag, or if the legacy subjective_log.sick flag is set (kept
working so old check-ins still exclude without a data migration).

Both consumers (services/coach.py eligibility, services/correlations.py series)
call in here so the rule can never drift between them.
"""
from datetime import date
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DayFlag, SubjectiveLog

# Canonical reasons. `incomplete_data` is auto-only (missing nutrition etc.);
# the rest are user-selectable. `travel` is also auto-materialised from
# slept_away. Keep in sync with the frontend reason list.
DAY_FLAG_REASONS = (
    "travel", "travel_recovery", "sick", "poor_sleep_env", "high_stress",
    "incomplete_data", "other",
)

REASON_LABELS = {
    "travel":          "Traveling",
    "travel_recovery": "Back from a trip (jet lag)",
    "sick":            "Sick",
    "poor_sleep_env":  "Poor sleep environment",
    "high_stress":     "High stress",
    "incomplete_data": "Incomplete data",
    "other":           "Other",
}


# incomplete_data is NOT a whole-day exclusion: a day with a partial meal log
# still has valid HRV/sleep/steps and a valid experiment outcome. It only makes
# the NUTRITION inputs unreliable, so it's handled separately
# (nutrition_excluded_dates → null the macros) rather than dropping the day.
_WHOLE_DAY_REASONS_SQL = DayFlag.reason != "incomplete_data"


async def excluded_dates(db: AsyncSession, start: date, end: date) -> set:
    """Local dates in [start, end] excluded from the WHOLE analysis (all
    correlations + experiments): travel, sick, poor env, high stress, other —
    plus the legacy subjective_log.sick. Excludes `incomplete_data` (nutrition-only).
    """
    out: set = set()
    rows = (await db.execute(
        select(DayFlag.date).where(DayFlag.date >= start).where(DayFlag.date <= end)
        .where(_WHOLE_DAY_REASONS_SQL)
    )).all()
    out.update(r[0] for r in rows)

    sick = (await db.execute(
        select(SubjectiveLog.date)
        .where(SubjectiveLog.date >= start).where(SubjectiveLog.date <= end)
        .where(SubjectiveLog.sick.is_(True))
    )).all()
    out.update(r[0] for r in sick)
    return out


async def nutrition_excluded_dates(db: AsyncSession, start: date, end: date) -> set:
    """Dates whose NUTRITION inputs are unreliable (incomplete_data flag) — a
    partial meal log understates intake, so the macros are nulled rather than the
    whole day dropped. Covers both auto (still nagging) and manual (skip-day)."""
    rows = (await db.execute(
        select(DayFlag.date)
        .where(DayFlag.date >= start).where(DayFlag.date <= end)
        .where(DayFlag.reason == "incomplete_data")
    )).all()
    return {r[0] for r in rows}


async def day_exclusion_reason(db: AsyncSession, day: date) -> Optional[str]:
    """Whole-day exclusion reason for a single day, or None if analysable.

    Used by experiment eligibility. Ignores `incomplete_data` (nutrition-only —
    doesn't affect the experiment outcome). Prefers an explicit DayFlag reason;
    falls back to the legacy sick flag.
    """
    flag = (await db.execute(
        select(DayFlag.reason).where(DayFlag.date == day)
        .where(_WHOLE_DAY_REASONS_SQL).order_by(DayFlag.id).limit(1)
    )).scalar_one_or_none()
    if flag is not None:
        return flag

    sick = (await db.execute(
        select(SubjectiveLog.sick).where(SubjectiveLog.date == day)
    )).scalar_one_or_none()
    return "sick" if sick else None


async def flags_in_range(db: AsyncSession, start: date, end: date) -> dict:
    """{date: [reason, ...]} for every flagged day in [start, end], sick included.

    Unlike `excluded_dates` this keeps the REASONS and keeps `incomplete_data`, because
    the consumer is narrative rather than statistical: a summary that knows the athlete
    was sick reads differently from one that only knows two days are missing. Callers
    that are deciding analysability still want `excluded_dates`.
    """
    out: dict = {}
    rows = (await db.execute(
        select(DayFlag.date, DayFlag.reason)
        .where(DayFlag.date >= start).where(DayFlag.date <= end)
        .order_by(DayFlag.date, DayFlag.id)
    )).all()
    for d, reason in rows:
        out.setdefault(d, [])
        if reason not in out[d]:
            out[d].append(reason)

    sick = (await db.execute(
        select(SubjectiveLog.date)
        .where(SubjectiveLog.date >= start).where(SubjectiveLog.date <= end)
        .where(SubjectiveLog.sick.is_(True))
    )).all()
    for (d,) in sick:
        out.setdefault(d, [])
        if "sick" not in out[d]:
            out[d].append("sick")
    return dict(sorted(out.items()))


# ── Away periods + the post-trip recovery buffer (2026-10-19, note 72) ────────────────
#
# A two-week trip abroad showed what the travel flag did NOT cover:
# ~55 pushes (Eastern-time steps nudges at 22:00 local, "coach tomorrow" at 04:00,
# daily freshness/screen-time alarms for streams that only exist at home), check-in
# takeovers on every app open, "finish logging" reminders, and streaks/habits counting
# every un-logged day as a miss. The flag itself already excluded the days from the
# correlations and experiments; everything below makes the rest of the app honour it.
#
# An "away period" is nothing more than consecutive `travel` flags — set ahead of time
# from the trip planner, or materialised nightly from slept_away. No separate table:
# one fact, one place, and every existing consumer of `travel` keeps working.
#
# `travel_recovery` is the buffer after a trip of at least TRAVEL_RECOVERY_MIN_TRIP
# days: jet lag distorts HRV/RHR/sleep for several days after return, and those days
# looked like ordinary home days. It is a whole-day exclusion (correlations,
# experiments, baselines) but NOT an away day — logging and check-ins run as normal.
# The user chose N = 4 on 2026-10-19 (a 7 h westward shift). It is an auto flag, visible
# and deletable like any other.

AWAY_REASON = "travel"
RECOVERY_REASON = "travel_recovery"
TRAVEL_RECOVERY_DAYS = 4
TRAVEL_RECOVERY_MIN_TRIP = 3     # a one- or two-night trip doesn't earn a buffer
# The buffer is for JET LAG (2026-11-11, after a short road trip was given one): only a trip that spent a day at least this far from home's UTC offset earns one.
# A one-hour shift does not.
TRAVEL_RECOVERY_MIN_SHIFT_H = 2


async def trip_shifted_zones(db: AsyncSession, days: list) -> bool:
    """Did any day of the trip sit TRAVEL_RECOVERY_MIN_SHIFT_H+ hours from home?"""
    from datetime import datetime as _dt, time as _t
    from app.services.tz import home_zone, tz_for_date
    for d in days:
        noon = _dt.combine(d, _t(12))
        zone = await tz_for_date(db, d)
        shift = abs((zone.utcoffset(noon) - home_zone().utcoffset(noon)).total_seconds()) / 3600
        if shift >= TRAVEL_RECOVERY_MIN_SHIFT_H:
            return True
    return False

# Derived habits that read the CLOCK or need active logging. Since tz phase 4
# (2026-10-19) they are blanked only on away days whose ZONE is unknown (no tz period,
# no away segment); with a known zone they are judged in local time, except
# no_late_caffeine on a day with no logged dose (see habit_metrics.build_calendar).
# Original reasoning, from before the app was time-zone aware: morning light is judged in Eastern
# hours, bed/wake consistency against Eastern-time nights, and late-caffeine falls
# back to a default-True when nothing was logged. Manual habits are deliberately NOT
# here — the user (2026-10-19): an unlogged supplement/creatine day while away is a real
# miss, and alcohol records are ones that were logged for completeness.
AWAY_UNKNOWN_HABITS = (
    "no_late_caffeine", "outdoor_light_am", "consistent_bedtime", "consistent_waketime",
)


async def away_dates(db: AsyncSession, start: date, end: date) -> set:
    """Local dates in [start, end] that are part of an away period (any `travel` flag)."""
    rows = (await db.execute(
        select(DayFlag.date).where(DayFlag.date >= start).where(DayFlag.date <= end)
        .where(DayFlag.reason == AWAY_REASON)
    )).all()
    return {r[0] for r in rows}


async def is_away(db: AsyncSession, day: date) -> bool:
    return bool(await away_dates(db, day, day))


async def push_mute_reason(db: AsyncSession, day: date, kind: str) -> Optional[str]:
    """Why a push of `kind` should NOT go out on `day`, or None to send it.

    kind = "nag"      — steps nudge, coach calls, briefing, input/screen-time
                        freshness: pointless while away (and mistimed abroad).
    kind = "recovery" — illness + HRV-suppression alerts: also muted during the
                        post-trip buffer, because jet lag produces exactly the RHR-up /
                        HRV-down pattern those alarms look for.
    Anything else (an AI outage, for example) is never muted here.
    """
    reasons = {r[0] for r in (await db.execute(
        select(DayFlag.reason).where(DayFlag.date == day)
        .where(DayFlag.reason.in_((AWAY_REASON, RECOVERY_REASON)))
    )).all()}
    if AWAY_REASON in reasons and kind in ("nag", "recovery"):
        return "away"
    if RECOVERY_REASON in reasons and kind == "recovery":
        return "post-trip recovery"
    return None


async def sync_travel_recovery(db: AsyncSession, today: date, lookback_days: int = 60) -> list:
    """Create the post-trip `travel_recovery` flags for any trip that just ENDED.

    A trip = a run of >= TRAVEL_RECOVERY_MIN_TRIP consecutive travel days. Its buffer is
    the TRAVEL_RECOVERY_DAYS after the last one. Only trips whose last day is within
    the past 3 days are acted on, so a buffer day the user deletes stays deleted once that
    window has passed (there's no tombstone), and a pre-planned trip gets its buffer
    the morning after it actually ends, not when it is entered. Returns created dates.
    """
    from datetime import timedelta
    travel = sorted(await away_dates(db, today - timedelta(days=lookback_days), today))
    runs, cur = [], []
    for d in travel:
        if cur and (d - cur[-1]).days != 1:
            runs.append(cur); cur = []
        cur.append(d)
    if cur:
        runs.append(cur)

    created = []
    travel_set = set(travel)
    for run in runs:
        end = run[-1]
        if len(run) < TRAVEL_RECOVERY_MIN_TRIP or end >= today or (today - end).days > 3:
            continue
        if not await trip_shifted_zones(db, run):
            continue                        # a road trip: no jet lag, no buffer
        for i in range(1, TRAVEL_RECOVERY_DAYS + 1):
            d = end + timedelta(days=i)
            if d in travel_set:
                break                       # left again — the next trip owns it
            exists = (await db.execute(
                select(DayFlag.id).where(DayFlag.date == d).where(DayFlag.reason == RECOVERY_REASON)
            )).scalar_one_or_none()
            if exists is None:
                db.add(DayFlag(date=d, reason=RECOVERY_REASON, source="auto",
                               note=f"auto: {TRAVEL_RECOVERY_DAYS}-day buffer after a "
                                    f"{len(run)}-day trip ending {end.isoformat()}"))
                created.append(d)
    if created:
        await db.commit()
    return created


# ── Keeping the two sick writers in step ──────────────────────────────────────
#
# NOTE 23 (2026-09-07): "the sick log in the morning check-in doesn't talk to the sick
# log in Today". Two writers, one fact:
#   • the morning check-in sets `subjective_log.sick`
#   • the Today tab's DayFlagControl writes a `DayFlag(reason='sick')`
# Reads had already been patched to union both (`excluded_dates`, `flags_in_range`, and
# the chart bands via `include_legacy_sick`), so analysis was correct — but the two UIs
# showed each other nothing, and marking yourself sick at check-in left Today insisting
# the day was standard.
#
# `DayFlag` is the general mechanism and the source of truth; `subjective_log.sick` is
# the legacy field kept working for old check-ins. So the sync is deliberately ASYMMETRIC:
#
#   forward (check-in -> flag): create and delete freely, but ONLY rows this sync owns
#       (`source='checkin'`). A flag the user raised by hand in Today is never removed by
#       a later check-in that happens not to mention illness.
#   reverse (flag -> check-in): UPDATE an existing subjective row only, never INSERT one.
#       Creating a row just to carry `sick` would manufacture a morning check-in that
#       never happened, and the weekly digest counts those rows.

SICK_FLAG_SOURCE = "checkin"


async def sync_sick_flag(db: AsyncSession, day: date, is_sick: Optional[bool]) -> Optional[str]:
    """Mirror a check-in's `sick` value into `day_flags`. Returns what it did, for logs.

    `None` means "the check-in said nothing about illness" and is a no-op — it must not
    clear a flag, because a partial submission (an iOS Shortcut sending only subscores)
    merges to None for every field it omitted.
    """
    existing = (await db.execute(
        select(DayFlag).where(DayFlag.date == day).where(DayFlag.reason == "sick")
    )).scalars().first()

    if is_sick is True:
        if existing is None:
            db.add(DayFlag(date=day, reason="sick", source=SICK_FLAG_SOURCE,
                           note="marked sick on the morning check-in"))
            return "created"
        return "already-flagged"

    if is_sick is False and existing is not None and existing.source == SICK_FLAG_SOURCE:
        # Only ever retract our own row. A manual flag is the user's statement about the
        # day and outranks a check-in toggle they may simply have left alone.
        await db.delete(existing)
        return "cleared"
    return "noop"


async def sync_sick_checkin(db: AsyncSession, day: date, is_sick: bool) -> Optional[str]:
    """Mirror a DayFlag sick change back onto an EXISTING subjective row.

    Never inserts — see the note above on phantom check-ins.
    """
    row = (await db.execute(
        select(SubjectiveLog).where(SubjectiveLog.date == day)
    )).scalars().first()
    if row is None:
        return "no-checkin-row"
    if bool(row.sick) == bool(is_sick):
        return "already-in-step"
    row.sick = is_sick
    return "updated"
