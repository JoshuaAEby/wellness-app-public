"""Did you eat after dinner? One definition, shared by the nutrition scorecard and the
after-dinner experiment's automatic compliance (2026-10-04) — two copies of a rule like
this drift apart, and then the scorecard and the trial disagree about the same evening.

A day counts as "ate after dinner" when a dessert or snack with calories is timed after
that day's last dinner entry. Rules:
  * only days with a dinner at a REAL time (time_estimated rows are placeholders);
  * only from MEAL_TIMES_RELIABLE_FROM — food_log times were a noon placeholder before it;
  * logger rows (caffeine, alcohol, water) are drinks, not food, and never count;
  * skipped rows never count.
Returns None for a day it cannot judge (no timed dinner), never a guess.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FoodLog
from app.foodday import food_day, food_day_end, food_day_start

MEAL_TIMES_RELIABLE_FROM = date(2026, 8, 17)


async def ate_after_dinner(db: AsyncSession, start: date, end: date) -> dict[date, Optional[bool]]:
    rows = (await db.execute(
        select(FoodLog.timestamp, FoodLog.meal_type, FoodLog.calories, FoodLog.skipped,
               FoodLog.time_estimated, FoodLog.source_kind)
        .where(FoodLog.timestamp >= food_day_start(max(start, MEAL_TIMES_RELIABLE_FROM)))
        .where(FoodLog.timestamp < food_day_end(end))
    )).all()
    by_day: dict[date, list] = defaultdict(list)
    for r in rows:
        by_day[food_day(r.timestamp)].append(r)
    out: dict[date, Optional[bool]] = {}
    for d, rs in by_day.items():
        dinners = [r.timestamp for r in rs
                   if r.meal_type == "dinner" and not r.skipped and not r.time_estimated]
        if not dinners:
            out[d] = None
            continue
        cutoff = max(dinners)
        out[d] = any(r.meal_type in ("dessert", "snack") and r.timestamp > cutoff
                     and not r.skipped and not r.time_estimated and r.source_kind is None
                     and (r.calories or 0) > 0 for r in rs)
    return out
