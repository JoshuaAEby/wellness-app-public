"""
Total fluid from drinks, and a daily target for it (note 78, 2026-10-25).

WHY TOTAL FLUID AND NOT WATER
-----------------------------
Water alone is the wrong quantity: at habitual doses caffeine's diuretic effect is
small, and coffee hydrates about as well as water (Killer 2014; Maughan 2016's
beverage hydration index). So a day of three cold brews and one bottle is not a
low-fluid day. Counted here, per FOOD day (04:00 → 04:00, app/foodday.py — the LOG
date bar's day, so the card and the monthly summary agree):

  water     health_metrics 'water' (oz)
  caffeine  caffeine_log × the preset's `oz` (volume lives on the PRESET, not the
            entry, so editing a preset's oz re-values its whole history — the same
            "apply by preset" rule as the cold-brew mg backfill). Entries whose kind
            has no oz ("custom", deleted presets) are counted as unknown, not zero.
  other     food_log rows with no source_kind (the caffeine/alcohol buttons write
            linked drinks rows — counting those too would double-count) and a LIQUID
            volume: meal 'drinks' with oz/fl oz/ml/l, or any meal with fl oz/ml/l.
            Bare "oz" outside Drinks is skipped: there it is usually a WEIGHT (6 oz
            steak). A smoothie logged at lunch in "oz" is missed — known, accepted.

ALCOHOL IS EXCLUDED. Beer is roughly fluid-neutral, wine and spirits net-negative;
counting them would make a heavy-drinking day read as a well-hydrated one.

THE TARGET
----------
There is no measured requirement — EFSA (2.5 L) and NASEM (3.7 L) adequate intakes
for men are typical intakes of hydrated people, ~80% of which comes from drinks.
The base is the weight rule in between: 35 mL/kg total × 0.8 from drinks
= 28 mL/kg ≈ 0.43 oz per lb (180 lb → 77 oz). Training adds 16 oz (0.5 L) per hour
of non-walk training — the low end of 0.5-1.5 L/h sweat losses. EFSA's drinks share
(~68 oz) is the "clearly low" floor. Thirst and urine colour beat any formula; this
is a sanity check, and the copy says so.

FROM 2026-11-01 (note 95): a flat 100 oz, plus 4 oz per SDU of wine or spirits and
nothing for beer. Close to the ~10 mL of extra urine per g of ethanol usually quoted
(≈ 4.7 oz per SDU); beer and seltzer are watery enough to roughly offset themselves, and
as alcohol they count for nothing in the total either. 100 is about the old base plus one
class, so training adds nothing. Earlier days keep the rule above, so history is not
rewritten as misses. A drink of unknown type counts as wine or spirits: a target a few oz
high costs nothing.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.foodday import (food_day, food_day_bounds_utc, food_day_end, food_day_of_utc, food_today,
                         food_day_start, on_food_days_utc)
from app.models import CaffeineLog, FoodLog, HealthMetric, Workout
from app.workout_categories import LOW_INTENSITY_TYPES

OZ_PER_LB = 0.43            # 28 mL/kg of drinks, in oz per lb
FALLBACK_BASE_OZ = 84       # used when no weight has ever been logged
TRAINING_OZ_PER_H = 16
FLOOR_OZ = 68               # EFSA 2.5 L × 0.8 from drinks
ML_PER_OZ = 29.5735
FLAT_TARGET_FROM = date(2026, 11, 1)
FLAT_TARGET_OZ = 100
ALCOHOL_OZ_PER_SDU = 4
_WATERY_KINDS = {"beer", "beer_light", "ipa", "seltzer"}
# For drinks with no stored kind: free-text labels and My Food names ("Lime lager").
_WATERY_WORDS = ("beer", "lager", "ipa", " ale", "pilsner", "stout", "porter", "seltzer",
                 "high noon", "white claw", "truly")

_LIQUID_ANY_MEAL = {"fl oz", "fl. oz", "floz", "ml", "l", "liter", "litre"}
_LIQUID_DRINKS = _LIQUID_ANY_MEAL | {"oz", "ounce", "ounces"}


def _liquid_oz(qty, unit, meal_type) -> float | None:
    u = (unit or "").strip().lower()
    if qty is None or qty <= 0:
        return None
    allowed = _LIQUID_DRINKS if meal_type == "drinks" else _LIQUID_ANY_MEAL
    if u not in allowed:
        return None
    if u == "ml":
        return qty / ML_PER_OZ
    if u in ("l", "liter", "litre"):
        return qty * 1000 / ML_PER_OZ
    return float(qty)


async def _preset_oz(db: AsyncSession) -> dict[str, float]:
    from app.services.caffeine import effective_presets
    return {p["id"]: float(p["oz"]) for p in await effective_presets(db) if p.get("oz")}


async def fluid_days(db: AsyncSession, start: date, end: date) -> dict[date, dict]:
    """{food_day: {water, caffeine, other, total, unknown_n}} for days with any drink."""
    lo, hi = food_day_bounds_utc(start)[0], food_day_bounds_utc(end)[1]
    out: dict[date, dict] = defaultdict(lambda: {"water": 0.0, "caffeine": 0.0,
                                                 "other": 0.0, "unknown_n": 0})

    for ts, oz in (await db.execute(
        select(HealthMetric.timestamp, HealthMetric.value)
        .where(HealthMetric.metric_type == "water")
        .where(HealthMetric.timestamp >= lo).where(HealthMetric.timestamp < hi)
    )).all():
        out[food_day_of_utc(ts)]["water"] += oz or 0.0

    oz_by_kind = await _preset_oz(db)
    no_volume: dict[int, date] = {}      # caffeine entries whose kind carries no oz
    for cid, ts, kind, qty in (await db.execute(
        select(CaffeineLog.id, CaffeineLog.timestamp, CaffeineLog.kind, CaffeineLog.quantity)
        .where(CaffeineLog.timestamp >= lo).where(CaffeineLog.timestamp < hi)
    )).all():
        d = out[food_day_of_utc(ts)]
        if kind in oz_by_kind:
            d["caffeine"] += oz_by_kind[kind] * (qty or 1.0)
        else:
            d["unknown_n"] += 1
            no_volume[cid] = food_day_of_utc(ts)

    for ts, qty, unit, meal, src_kind, src_id in (await db.execute(
        select(FoodLog.timestamp, FoodLog.portion_quantity, FoodLog.portion_unit, FoodLog.meal_type,
               FoodLog.source_kind, FoodLog.source_id)
        .where(FoodLog.timestamp >= food_day_start(start))
        .where(FoodLog.timestamp < food_day_end(end))
        .where(FoodLog.source_kind.is_(None) | (FoodLog.source_kind == "caffeine"))
        .where(FoodLog.skipped.isnot(True))
    )).all():
        oz = _liquid_oz(qty, unit, meal)
        if src_kind == "caffeine":
            # A parsed coffee (note 100) is a caffeine entry with no preset volume, linked to
            # this row; its volume is the row's portion. A preset's row is counted above.
            if src_id in no_volume and oz:
                d = out[no_volume.pop(src_id)]
                d["caffeine"] += oz
                d["unknown_n"] -= 1
            continue
        if oz:
            out[food_day(ts)]["other"] += oz

    for v in out.values():
        v["total"] = v["water"] + v["caffeine"] + v["other"]
        for k in ("water", "caffeine", "other", "total"):
            v[k] = round(v[k], 1)
    return {d: v for d, v in out.items() if start <= d <= end}


async def _weight_lb(db: AsyncSession, day: date) -> float | None:
    return (await db.execute(
        select(HealthMetric.value).where(HealthMetric.metric_type == "weight")
        .where(HealthMetric.timestamp < food_day_bounds_utc(day)[1])
        .order_by(HealthMetric.timestamp.desc()).limit(1)
    )).scalar_one_or_none()


async def training_minutes(db: AsyncSession, start: date, end: date) -> dict[date, float]:
    """Non-walk, non-suspect workout minutes per food day."""
    lo, hi = food_day_bounds_utc(start)[0], food_day_bounds_utc(end)[1]
    out: dict[date, float] = defaultdict(float)
    for w in (await db.execute(
        select(Workout).where(Workout.start_time >= lo).where(Workout.start_time < hi)
    )).scalars().all():
        if w.suspect or (w.activity_type or "") in LOW_INTENSITY_TYPES:
            continue
        out[food_day_of_utc(w.start_time)] += (w.duration_sec or 0) / 60
    return out


def is_watery(kind: str | None, label: str | None) -> bool:
    """Beer or seltzer, which add nothing to the target. Everything else, unknown included,
    counts as wine or spirits."""
    if kind:
        return kind in _WATERY_KINDS
    name = f" {label or ''}".lower()
    return any(w in name for w in _WATERY_WORDS)


async def alcohol_add_sdu(db: AsyncSession, start: date, end: date) -> dict[date, float]:
    """SDU per food day that raise the target (wine, spirits, unknown)."""
    rows = (await db.execute(
        select(HealthMetric.timestamp, HealthMetric.value, FoodLog.drink_kind, FoodLog.name)
        .outerjoin(FoodLog, (FoodLog.source_kind == "alcohol") & (FoodLog.source_id == HealthMetric.id))
        .where(HealthMetric.metric_type == "alcohol")
        .where(on_food_days_utc(HealthMetric.timestamp, start, end))
    )).all()
    out: dict[date, float] = defaultdict(float)
    for ts, sdu, kind, name in rows:
        if not is_watery(kind, name):
            out[food_day_of_utc(ts)] += sdu or 0.0
    return out


async def targets(db: AsyncSession, start: date, end: date) -> dict[date, dict]:
    """{day: {target, base, training_min, alcohol_sdu}} — from FLAT_TARGET_FROM, the flat
    target plus wine/spirits; before it, base from the latest weight on/before the day."""
    tmin = await training_minutes(db, start, end)
    add_sdu = await alcohol_add_sdu(db, max(start, FLAT_TARGET_FROM), end) \
        if end >= FLAT_TARGET_FROM else {}
    out, d = {}, start
    while d <= end:
        if d >= FLAT_TARGET_FROM:
            sdu = round(add_sdu.get(d, 0.0), 1)
            out[d] = {"target": round(FLAT_TARGET_OZ + ALCOHOL_OZ_PER_SDU * sdu),
                      "base": FLAT_TARGET_OZ, "training_min": 0, "alcohol_sdu": sdu, "flat": True}
            d += timedelta(days=1)
            continue
        w = await _weight_lb(db, d)
        base = round(w * OZ_PER_LB) if w else FALLBACK_BASE_OZ
        m = round(tmin.get(d, 0.0))
        out[d] = {"target": round(base + TRAINING_OZ_PER_H * m / 60), "base": base,
                  "training_min": m, "alcohol_sdu": 0.0}
        d += timedelta(days=1)
    return out


async def goal_met_days(db: AsyncSession, start: date, end: date) -> dict[date, bool | None]:
    """The Record's FLUIDS row: did the day reach its target? Only water-logged days are
    judged (without water the total is not a day's fluids, the fluids_range rule). Today
    can turn True but is never False: the day is not over."""
    first = (await db.execute(
        select(func.min(HealthMetric.timestamp)).where(HealthMetric.metric_type == "water")
    )).scalar()
    today = food_today()
    start, end = max(start, food_day_of_utc(first)) if first else start, min(end, today)
    if first is None or end < start:
        return {}
    days = {d: v for d, v in (await fluid_days(db, start, end)).items() if v["water"] > 0}
    if not days:
        return {}
    tg = await targets(db, min(days), max(days))
    return {d: True if v["total"] >= tg[d]["target"] else (None if d == today else False)
            for d, v in days.items()}


async def day_status(db: AsyncSession, day: date) -> dict:
    """What the water card shows: today's fluids by source against today's target."""
    fl = (await fluid_days(db, day, day)).get(day) or {
        "water": 0.0, "caffeine": 0.0, "other": 0.0, "total": 0.0, "unknown_n": 0}
    t = (await targets(db, day, day))[day]
    return {**fl, **t, "floor": FLOOR_OZ, "training_oz_per_h": TRAINING_OZ_PER_H}


async def fluids_range(db: AsyncSession, start: date, end: date) -> dict:
    """Behaviors → Exposures FLUIDS chart (note 80, 2026-10-28).

    WATER-LOGGED days only. The user logs fully on any day he logs; fluid logging began
    2026-08-17, and a caffeine-only day before that (or any day without water) would read
    as low fluids rather than unlogged — the rule services/typical_values already uses.
    `first` carries the first-ever water and caffeine dates so the chart can say "not
    logged" for earlier periods instead of drawing zeros.
    """
    # Ends YESTERDAY's food day: today's half-logged fluids are not a finished day (same
    # rule as goal_trajectory_block — "protein goal incomplete" at 05:55, note 55).
    end = min(end, food_today() - timedelta(days=1))
    if end < start:
        return {"days": [], "goal": FLAT_TARGET_OZ, "first": {"water": None, "caffeine": None}}
    days = {d: v for d, v in (await fluid_days(db, start, end)).items() if v["water"] > 0}
    rows = []
    for d in sorted(days):
        t = (await targets(db, d, d))[d]
        rows.append({"date": d.isoformat(), **days[d], "target": t["target"]})
    first_water = (await db.execute(
        select(func.min(HealthMetric.timestamp)).where(HealthMetric.metric_type == "water")
    )).scalar()
    first_caff = (await db.execute(select(func.min(CaffeineLog.timestamp)))).scalar()
    return {
        "days": rows,
        "goal": FLAT_TARGET_OZ,
        "first": {"water": food_day_of_utc(first_water).isoformat() if first_water else None,
                  "caffeine": food_day_of_utc(first_caff).isoformat() if first_caff else None},
    }
