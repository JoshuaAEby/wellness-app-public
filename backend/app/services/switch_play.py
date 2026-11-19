"""Switch 2 play time from Home Assistant's Nintendo parental controls integration.

The sensor counts minutes played today and resets at local midnight, so a day's total is its
highest reading that day. Saved as metric `switch_min`, one row per local day, from HISTORY_DAYS
of HA history (all it keeps), so a missed run heals itself. A day HA has no reading for gets no
row: unknown, not zero. It is the CONSOLE total: no per-player sensors exist yet, so it cannot
separate players. Late syncs lag: a session reported after midnight lands on the new day.
Display only, not a correlation input. Read only: this never touches the integration's limits.
"""
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app.config import settings
from app.models import SwitchStep
from app.timeutils import local_day_bounds_utc, to_local_date

logger = logging.getLogger(__name__)

HISTORY_DAYS = 10
FIRST_DAY = date(2026, 11, 1)       # connected this morning


def daily_max(points: list[dict]) -> dict[date, float]:
    out: dict[date, float] = defaultdict(float)
    seen = set()
    for p in points:
        try:
            v = float(p["state"])
            d = to_local_date(datetime.fromisoformat(p["last_changed"])
                              .astimezone(timezone.utc).replace(tzinfo=None))
        except (KeyError, TypeError, ValueError):
            continue            # "unavailable" / "unknown"
        seen.add(d)
        out[d] = max(out[d], v)
    return {d: out[d] for d in seen if d >= FIRST_DAY}


def steps(points: list[dict]) -> list[tuple[datetime, float]]:
    """Readings where the counter changed; an unavailable blip between equal values is not one."""
    out, last = [], None
    for p in points:
        try:
            v = float(p["state"])
            t = datetime.fromisoformat(p["last_changed"]).astimezone(timezone.utc).replace(tzinfo=None)
        except (KeyError, TypeError, ValueError):
            continue
        if v != last:
            if last is not None:
                out.append((t, v))
            last = v
    return out


async def collect_switch(db: AsyncSession) -> int:
    from app.services.env_sensors import _upsert_night
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=HISTORY_DAYS)
    async with httpx.AsyncClient(base_url=settings.ha_url.rstrip("/"), timeout=30,
                                 headers={"Authorization": f"Bearer {settings.ha_token}"}) as c:
        r = await c.get(f"/api/history/period/{start.isoformat()}",
                        params={"end_time": now.isoformat(), "minimal_response": "true",
                                "no_attributes": "true",
                                "filter_entity_id": settings.ha_switch_entity})
        r.raise_for_status()
        data = r.json()
    points = data[0] if data else []
    days = daily_max(points)
    found = steps(points)
    have = set((await db.execute(select(SwitchStep.time).where(
        SwitchStep.time >= start.replace(tzinfo=None)))).scalars())
    db.add_all(SwitchStep(time=t, minutes=v) for t, v in found if t not in have)
    for d, v in days.items():
        await _upsert_night(db, "switch_min", d, local_day_bounds_utc(d)[0] + timedelta(hours=12), v)
    await db.commit()
    return len(days)
