"""What wasn't logged on a trip (note 102, 2026-11-10).

The user: after a trip, say which manually logged things he kept up and which have a known gap,
so the gap isn't read as a value. Without this, an unlogged trip day reads as ZERO drinks
(alcohol is logged completely, so absence = dry) and as MISSED supplements (his 2026-10-19
rule). Caffeine and water already read an empty day as unknown; ticking them also drops
partial entries from the charts for those days. Food needs nothing here: away days are
flagged incomplete automatically.

Consumers: Exposures (those days drawn as not logged), the habit calendar and streaks
(no_alcohol unknown and held; supplements and creatine held), and the alcohol lever's "now".
Correlations already leave travel days out of their frame.
"""
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TripGap

ITEMS = ("alcohol", "caffeine", "water", "supplements")


async def all_gaps(db: AsyncSession) -> list[TripGap]:
    return list((await db.execute(select(TripGap).order_by(TripGap.start_date))).scalars().all())


async def gap_dates(db: AsyncSession, item: str, start: date, end: date) -> set[date]:
    """Dates in [start, end] that fall in a trip where `item` was not logged."""
    out: set[date] = set()
    for g in await all_gaps(db):
        if item not in (g.items or []) or g.end_date < start or g.start_date > end:
            continue
        d = max(g.start_date, start)
        while d <= min(g.end_date, end):
            out.add(d)
            d += timedelta(days=1)
    return out


async def save_gaps(db: AsyncSession, start: date, end: date, items: list) -> TripGap:
    from datetime import datetime
    clean = [i for i in ITEMS if i in set(items or [])]
    row = (await db.execute(select(TripGap).where(TripGap.start_date == start))).scalar_one_or_none()
    if row is None:
        row = TripGap(start_date=start)
        db.add(row)
    row.end_date, row.items, row.updated_at = end, clean, datetime.utcnow()
    await db.commit()
    return row
