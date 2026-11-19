"""Which bedtime a day's intake should be measured against.

The bedtime-residual metrics — caffeine and alcohol still on board at sleep onset
— need the bedtime that FOLLOWS a day. `sleep_sessions.date` is the WAKE date, so
the night after day D is the session dated **D+1**. Getting this backwards is not
hypothetical: it is exactly what left `caffeine_onboard_bed` reading 0.0 on every
day it ever ran (found 2026-08-30). Anything that pairs intake with a
bedtime goes through this module.

For TODAY no such session exists yet, so the residual is a PROJECTION against the
recent median bedtime. Callers must pass the `projected` flag through to the UI —
a projected number keeps moving as the evening goes on and as more is logged,
while a settled one never changes. Presenting the two identically would invite
reading tonight's guess as a measurement.
"""
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SleepSession
from app.timeutils import local_day_bounds_utc

logger = logging.getLogger(__name__)

# Nights of history behind the projected bedtime.
MEDIAN_WINDOW_NIGHTS = 14

# Used only when there is no bedtime history at all.
FALLBACK_OFFSET_MIN = 22 * 60 + 30      # 22:30 local


async def _median_offset_min(db: AsyncSession, before: date) -> float:
    """Median bedtime as MINUTES AFTER the local midnight that starts the evening.

    Measured from the evening's own midnight rather than as a clock time so a
    post-midnight bedtime stays comparable: 22:40 is 1360, 00:30 is 1470. Taking a
    median of raw clock hours would put those on opposite ends and land the
    "average" in the afternoon.
    """
    rows = (await db.execute(
        select(SleepSession.date, SleepSession.bedtime)
        .where(SleepSession.bedtime.isnot(None))
        .where(SleepSession.date <= before)
        .order_by(SleepSession.date.desc())
        .limit(MEDIAN_WINDOW_NIGHTS)
    )).all()

    offsets = []
    for r in rows:
        evening = r.date - timedelta(days=1)          # session date is the WAKE date
        midnight_utc, _ = local_day_bounds_utc(evening)
        offsets.append((r.bedtime - midnight_utc).total_seconds() / 60.0)

    if not offsets:
        return float(FALLBACK_OFFSET_MIN)
    offsets.sort()
    mid = len(offsets) // 2
    if len(offsets) % 2:
        return offsets[mid]
    return (offsets[mid - 1] + offsets[mid]) / 2.0


async def bedtime_for_day(
    db: AsyncSession, day: date
) -> tuple[datetime | None, bool]:
    """The bedtime that ends day `day`, as (naive-UTC datetime, is_actual).

    is_actual=False means the night has not been recorded yet and the value is a
    projection from the recent median. Never returns None in practice — the
    fallback covers a database with no sleep history at all.
    """
    session = (await db.execute(
        select(SleepSession)
        .where(SleepSession.date == day + timedelta(days=1))
        .where(SleepSession.bedtime.isnot(None))
    )).scalar_one_or_none()
    if session is not None:
        return session.bedtime, True

    midnight_utc, _ = local_day_bounds_utc(day)
    offset = await _median_offset_min(db, day)
    return midnight_utc + timedelta(minutes=offset), False
