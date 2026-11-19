"""Nutrition-completeness → auto `incomplete_data` day-flag.

A PAST day with partially-logged nutrition muddies the analysis: it looks like a
low-calorie day when really some meals just weren't recorded. So a past day is
excluded (auto `incomplete_data` DayFlag) until every main meal is RESOLVED —
either a food entry or an explicit per-meal skip. Two ways out of the excluded
state:
  (a) log/skip the missing meals → the day becomes complete → auto flag clears,
      day re-enters the analysis;
  (b) hit "Skip day" → a MANUAL incomplete_data flag (acknowledged): still
      excluded, but the reminder stops nagging.

Only PAST days are judged — today is legitimately still in progress. The auto
flag is only ever added/removed by this module; a manual flag (skip-day) is
never overwritten, so the acknowledgement sticks.
"""
import logging
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DayFlag, FoodLog
from app.foodday import food_day_end, food_day_start, food_today

logger = logging.getLogger(__name__)

MAIN_MEALS = ("breakfast", "lunch", "dinner")
_REASON = "incomplete_data"


async def resolved_meals(db: AsyncSession, day: date) -> set:
    """Main meals with at least one entry (food OR per-meal skip) on `day`."""
    rows = (await db.execute(
        select(FoodLog.meal_type).distinct()
        .where(FoodLog.timestamp >= food_day_start(day)).where(FoodLog.timestamp < food_day_end(day))
    )).all()
    present = {r[0] for r in rows}
    return {m for m in MAIN_MEALS if m in present}


async def is_complete(db: AsyncSession, day: date) -> bool:
    return len(await resolved_meals(db, day)) == len(MAIN_MEALS)


async def _auto_flag(db: AsyncSession, day: date) -> DayFlag | None:
    return (await db.execute(
        select(DayFlag).where(DayFlag.date == day).where(DayFlag.reason == _REASON)
    )).scalar_one_or_none()


async def sync_day(db: AsyncSession, day: date) -> str:
    """Reconcile the incomplete_data flag for one PAST day. Returns the action.

    - today/future: never flag (still in progress) → 'skipped_today'
    - a MANUAL flag present (skip-day acknowledgement): leave it → 'acknowledged'
    - incomplete + no flag: add auto flag → 'flagged'
    - complete + auto flag present: remove it → 'cleared'
    """
    if day >= food_today():            # the food day in progress (yesterday until 04:00)
        return "skipped_today"

    existing = await _auto_flag(db, day)
    if existing is not None and (existing.source or "manual") == "manual":
        return "acknowledged"

    complete = await is_complete(db, day)

    # AWAY days (travel flag): an incomplete log is expected, not something to nag
    # about. Flag it with source='away' — still nutrition-excluded (reason is
    # incomplete_data), but `incomplete_days` only lists source='auto', so no
    # "Finish logging" reminder. If the trip is later removed, the row falls back to
    # 'auto' below and is re-judged like any other day (2026-10-19).
    from app.services.day_flags import is_away
    if not complete and await is_away(db, day):
        if existing is None:
            db.add(DayFlag(date=day, reason=_REASON, source="away",
                           note="away: not logged while traveling"))
        elif existing.source != "away":
            existing.source, existing.note = "away", "away: not logged while traveling"
        else:
            return "away"
        await db.commit()
        return "away"
    if existing is not None and existing.source == "away":
        existing.source, existing.note = "auto", "auto: incomplete meal log"
        await db.commit()

    if not complete and existing is None:
        db.add(DayFlag(date=day, reason=_REASON, source="auto",
                       note="auto: incomplete meal log"))
        await db.commit()
        return "flagged"
    if complete and existing is not None:
        await db.delete(existing)
        await db.commit()
        return "cleared"
    return "noop"


async def sync_recent(db: AsyncSession, days_back: int = 14) -> dict:
    """Reconcile the last `days_back` past days. Returns action counts."""
    today = food_today()
    counts: dict = {}
    for i in range(1, days_back + 1):
        action = await sync_day(db, today - timedelta(days=i))
        counts[action] = counts.get(action, 0) + 1
    return counts


async def incomplete_days(db: AsyncSession, days_back: int = 7) -> list:
    """Past days (most recent first) that are AUTO-flagged incomplete — i.e. still
    nagging (a skip-day acknowledgement is source=manual and excluded here)."""
    today = food_today()
    start = today - timedelta(days=days_back)
    rows = (await db.execute(
        select(DayFlag.date)
        .where(DayFlag.reason == _REASON).where(DayFlag.source == "auto")
        .where(DayFlag.date >= start).where(DayFlag.date < today)
        .order_by(DayFlag.date.desc())
    )).all()
    return [r[0] for r in rows]


async def skip_day(db: AsyncSession, day: date) -> None:
    """Whole-day skip: acknowledge a day as intentionally un-logged.

    Converts any auto flag to a MANUAL incomplete_data flag (still excluded, no
    longer nagging). Distinct from per-meal skip, which is a KNOWN zero that keeps
    the day IN the analysis.
    """
    existing = await _auto_flag(db, day)
    if existing is not None:
        existing.source = "manual"
        existing.note = "skipped: day intentionally not logged"
        await db.commit()
        return
    db.add(DayFlag(date=day, reason=_REASON, source="manual",
                   note="skipped: day intentionally not logged"))
    await db.commit()
