"""Alcohol and water instance logging.

Both write into `health_metrics` rather than into dedicated tables (see Alembic 0050
for why: four years of alcohol history already lives there and every consumer reads it
from there). Caffeine keeps its own table because its pharmacokinetic model needs
per-instance decay; alcohol and water need only a real timestamp.

TIMEZONE: health_metrics timestamps are naive **UTC** — the opposite convention to
food_log, which is naive local. The user supplies a local wall-clock time; it is
converted here. Getting this backwards would shift every evening drink into the next
day for anyone west of UTC.
"""
from __future__ import annotations

from datetime import date as Date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings as app_settings
from app.foodday import food_day_bounds_utc, food_timestamp, food_today
from app.models import HealthMetric, UserSettings
from app.timeutils import local_date_sql, local_now, current_tz, to_local, to_local_date, zone_for_date

SOURCE = "wellness_app"          # distinguishes hand-logged rows from apple_health

# A US standard drink is 0.6 fl oz of pure ethanol (14 g). Keeping the constant
# explicit means a preset's SDU is always re-derivable from its oz/abv rather than
# being a number someone typed once.
ETHANOL_OZ_PER_SDU = 0.6

# `kcal` seeds the linked Nutrition entry. It is NOT derivable from the dose: ethanol
# is 7 kcal/g, so 1 SDU is ~98 kcal of alcohol alone, but beer carries carbohydrate on
# top (the existing hand-logged history says IPA 200, wine 120). Edit these once and
# both the SDU and the calories land from one tap.
ALCOHOL_PRESETS = [
    # kcal and macros are INTERNALLY CONSISTENT: kcal ~= 7*ethanol_g + 4*carbs + 4*protein
    # + 9*fat, where ethanol_g = oz * abv * 0.2333 (density 0.789 g/mL). Ethanol alone
    # sets a floor no macro edit can go below — a 16 oz 7% IPA is already 183 kcal before
    # a single gram of carbohydrate, which is why the first draft's 200 kcal was wrong
    # (it left room for 4 g of carbs in a drink that has ~15).
    {"id": "beer",     "label": "Beer",         "emoji": "🍺", "oz": 12,  "abv": 5.0,  "kcal": 155,
     "macros": {"carbs_g": 13.0, "protein_g": 1.6}},
    {"id": "ipa",      "label": "IPA / strong", "emoji": "🍺", "oz": 16,  "abv": 7.0,  "kcal": 255,
     "macros": {"carbs_g": 15.0, "protein_g": 2.5}},
    {"id": "wine",     "label": "Wine",         "emoji": "🍷", "oz": 5,   "abv": 12.0, "kcal": 115,
     "macros": {"carbs_g": 4.0, "protein_g": 0.1}},
    {"id": "shot",     "label": "Shot",         "emoji": "🥃", "oz": 1.5, "abv": 40.0, "kcal": 100,
     "macros": {"carbs_g": 0.0, "protein_g": 0.0}},
    {"id": "cocktail", "label": "Cocktail",     "emoji": "🍸", "oz": 3,   "abv": 25.0, "kcal": 165,
     "macros": {"carbs_g": 10.0, "protein_g": 0.0}},
    {"id": "seltzer",  "label": "Hard seltzer", "emoji": "🥫", "oz": 12,  "abv": 5.0,  "kcal": 105,
     "macros": {"carbs_g": 2.0, "protein_g": 0.0}},
]


# ── Estimating calories from size + strength + category (notes 26/27) ────────
#
# The user: *"by default I should have the option to add a size in ounces and an ABV
# percentage with the STU amount computed from those, and calorie and other macro data
# estimated from size"* and *"maybe it can have options for beer, wine, and liquor?
# I'm not sure how much things vary for a given category at a given size"*.
#
# TWO NUMBERS WITH VERY DIFFERENT ERROR BARS, and it matters which is which:
#
#   SDU is NOT an estimate. It is arithmetic on volume x strength, exact to the extent
#   the pour and the ABV on the label are right. This is the number the PK model
#   (services/alcohol) and the correlation engine both consume, so the important output
#   carries no modelling error at all.
#
#   CALORIES are part-exact, part-estimate. Ethanol contributes 7 kcal/g and is fixed by
#   the same oz x abv, so it is exact. Only the RESIDUAL CARBOHYDRATE depends on
#   category, and that is the only thing the category picker is guessing. For spirits it
#   is zero, so the estimate is effectively exact; for beer it is the widest (a light
#   lager and a stout at the same size and ABV differ by ~15 g carbs, i.e. ~60 kcal).
#
# THE DENSITIES ARE NOT INVENTED — they are extracted from the six hand-tuned presets
# above, which turn out to encode exactly this model. Reproduced against them:
#   beer 155 vs 156.4 · ipa 255 vs 252.9 · wine 115 vs 114.4
#   shot 100 vs 98.0 · cocktail 165 vs 162.5 · seltzer 105 vs 106.0
# All within 2.5 kcal, so a custom entry and a preset of the same drink agree.

# oz x abv% -> grams of ethanol. 29.5735 mL/fl oz x 0.789 g/mL / 100.
ETHANOL_G_PER_OZ_PCT = 0.23334
KCAL_PER_G_ETHANOL = 7.0

ALCOHOL_CATEGORIES = [
    # carbs/protein per FLUID OUNCE of the drink as served.
    {"id": "beer",     "label": "Beer",     "emoji": "🍺", "carbs_g_per_oz": 1.05,
     "protein_g_per_oz": 0.14,
     "note": "regular beer; pick Light beer for a low-carb lager"},
    # SPLIT OUT after measuring the error (2026-09-11). Light lagers are formulated for
    # low residual carbohydrate — ~5 g per 12 oz against ~13 g for regular — so folding
    # them into "beer" over-attributed by 36 kcal on a 12 oz 4.2% pour, the single worst
    # case found. Every other category came within 7 kcal of published values, so this is
    # the one place the one-density-per-category assumption actually breaks.
    {"id": "beer_light", "label": "Light beer", "emoji": "🍺", "carbs_g_per_oz": 0.42,
     "protein_g_per_oz": 0.07,
     "note": "low-carb lager — the lower ABV is handled separately, so enter it too"},
    {"id": "wine",     "label": "Wine",     "emoji": "🍷", "carbs_g_per_oz": 0.80,
     "protein_g_per_oz": 0.02,
     "note": "dry wine; a sweet or dessert wine runs well above this"},
    {"id": "liquor",   "label": "Liquor",   "emoji": "🥃", "carbs_g_per_oz": 0.0,
     "protein_g_per_oz": 0.0,
     "note": "neat spirits are essentially pure ethanol — this estimate is exact"},
    {"id": "cocktail", "label": "Cocktail", "emoji": "🍸", "carbs_g_per_oz": 3.33,
     "protein_g_per_oz": 0.0,
     "note": "mixer-dominated; enter the TOTAL served volume, not just the spirit"},
    {"id": "seltzer",  "label": "Seltzer",  "emoji": "🥫", "carbs_g_per_oz": 0.17,
     "protein_g_per_oz": 0.0,
     "note": "hard seltzer / low-carb canned"},
]
CATEGORY_BY_ID = {c["id"]: c for c in ALCOHOL_CATEGORIES}
DEFAULT_CATEGORY = "beer"


def estimate_alcohol_nutrition(oz: float, abv: float,
                               category: Optional[str] = None) -> dict:
    """kcal + macros for a drink of this size, strength and kind.

    Unknown categories fall back to LIQUOR (zero residual carbs) rather than to beer:
    an unknown drink should not silently acquire 13 g of carbohydrate it may not have.
    Under-attributing is recoverable; inventing carbohydrate is not.
    """
    oz, abv = float(oz), float(abv)
    cat = CATEGORY_BY_ID.get(category or "", CATEGORY_BY_ID["liquor"])
    ethanol_g = oz * abv * ETHANOL_G_PER_OZ_PCT
    carbs = oz * cat["carbs_g_per_oz"]
    protein = oz * cat["protein_g_per_oz"]
    return {
        "kcal": round(KCAL_PER_G_ETHANOL * ethanol_g + 4.0 * carbs + 4.0 * protein),
        "macros": {"carbs_g": round(carbs, 1), "protein_g": round(protein, 1)},
        "ethanol_g": round(ethanol_g, 1),
        "category": cat["id"],
    }


def sdu_from(oz: float, abv: float) -> float:
    """Standard drink units from volume + strength."""
    return round((float(oz) * (float(abv) / 100.0)) / ETHANOL_OZ_PER_SDU, 3)


def preset_sdu(p: dict) -> float:
    return p.get("sdu") if p.get("sdu") is not None else sdu_from(p["oz"], p["abv"])


async def effective_alcohol_presets(db: AsyncSession) -> list:
    custom = (await db.execute(select(UserSettings.alcohol_presets).limit(1))).scalar_one_or_none()
    presets = custom if custom else ALCOHOL_PRESETS
    # Same kcal backfill as caffeine: a list saved before calories existed must not
    # silently start logging zero-calorie beers.
    by_id = {p["id"]: p for p in ALCOHOL_PRESETS}
    return [{**p, "sdu": preset_sdu(p),
             "kcal": p.get("kcal", by_id.get(p["id"], {}).get("kcal")),
             **({"macros": by_id[p["id"]]["macros"]}
                if p.get("macros") is None and by_id.get(p["id"], {}).get("macros") else {})}
            for p in presets]


async def save_alcohol_presets(db: AsyncSession, presets: list | None) -> list:
    row = (await db.execute(select(UserSettings).limit(1))).scalar_one_or_none()
    if row is None:
        row = UserSettings()
        db.add(row)
    row.alcohol_presets = presets
    await db.commit()
    return await effective_alcohol_presets(db)


WATER_PRESETS = [
    {"id": "bottle", "label": "Refill", "emoji": "💧", "oz": 30},
    {"id": "glass",  "label": "Glass",  "emoji": "🥛", "oz": 8},
    {"id": "pint",   "label": "Pint",   "emoji": "🥤", "oz": 16},
]


async def effective_water_presets(db: AsyncSession) -> list:
    """Custom list if set, else defaults with the bottle sized from settings.

    `water_bottle_oz` predates presets, so the default "Refill" inherits it rather
    than silently reverting a bottle size the user already configured.
    """
    row = (await db.execute(select(UserSettings).limit(1))).scalar_one_or_none()
    if row is not None and row.water_presets:
        return row.water_presets
    size = int(row.water_bottle_oz) if row is not None and row.water_bottle_oz else 30
    return [{**p, **({"oz": size} if p["id"] == "bottle" else {})} for p in WATER_PRESETS]


async def save_water_presets(db: AsyncSession, presets: list | None) -> list:
    row = (await db.execute(select(UserSettings).limit(1))).scalar_one_or_none()
    if row is None:
        row = UserSettings()
        db.add(row)
    row.water_presets = presets
    await db.commit()
    return await effective_water_presets(db)


async def bottle_oz(db: AsyncSession) -> int:
    v = (await db.execute(select(UserSettings.water_bottle_oz).limit(1))).scalar_one_or_none()
    return int(v) if v else 30


def _to_utc_naive(day: Date, at_time: str | None) -> datetime:
    """Local wall-clock -> naive UTC, the health_metrics convention.

    No time given: use the current clock for today, else noon on that day. Same
    day-dependent rule as food_log — "now" is a good guess for something you just
    consumed and a fabrication for a backfill.
    """
    now_local = local_now()
    # `day` is the LOG date bar's day, a FOOD day (app/foodday.py, 04:00 boundary): a
    # clock time before 04:00 is the following calendar date, and "today" until 04:00 is
    # yesterday. A clock time typed for today means local time where the user is; for a
    # backfilled day, the zone that day was spent in (tz phase 3).
    is_today = day == food_today()
    if at_time:
        try:
            hh, mm = (int(x) for x in str(at_time).split(":")[:2])
            naive = food_timestamp(day, time(hh, mm))
        except (ValueError, TypeError):
            naive = datetime.combine(day, time(12, 0))
        local = naive.replace(tzinfo=current_tz() if is_today else zone_for_date(naive.date()))
    elif is_today:
        local = now_local.replace(second=0, microsecond=0)
    else:
        local = datetime.combine(day, time(12, 0), tzinfo=zone_for_date(day))
    return local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


async def _insert_metric(db: AsyncSession, metric_type: str, ts: datetime,
                         value: float, unit: str) -> HealthMetric:
    """Insert, stepping the timestamp forward on collision.

    health_metrics has UNIQUE(metric_type, timestamp), and two drinks logged in the
    same minute is entirely normal. Rather than fail or overwrite, nudge by a second —
    the second-level offset is meaningless physiologically and keeps both instances.
    """
    for _ in range(120):
        exists = (await db.execute(
            select(HealthMetric.id)
            .where(HealthMetric.metric_type == metric_type)
            .where(HealthMetric.timestamp == ts)
        )).scalar_one_or_none()
        if exists is None:
            break
        ts = ts + timedelta(seconds=1)
    row = HealthMetric(metric_type=metric_type, timestamp=ts, value=value,
                       unit=unit, source=SOURCE)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def log_alcohol(db: AsyncSession, day: Date, sdu: float,
                      at_time: str | None = None) -> HealthMetric:
    return await _insert_metric(db, "alcohol", _to_utc_naive(day, at_time), float(sdu), "sdu")


async def log_water(db: AsyncSession, day: Date, oz: float,
                    at_time: str | None = None) -> HealthMetric:
    return await _insert_metric(db, "water", _to_utc_naive(day, at_time), float(oz), "oz")


# Naps cannot come from Apple: sleep_sessions holds exactly ONE row per date (783 rows
# over 783 dates), so a daytime sleep is merged into that night's session rather than
# stored separately — which is also why a handful of nights show impossible 16 h spans.
# Duration is therefore user-supplied, and the timestamp is the nap's START.
NAP_PRESETS = [
    {"id": "power",  "label": "Power",     "emoji": "⚡", "minutes": 20},
    {"id": "short",  "label": "Short",     "emoji": "😴", "minutes": 45},
    {"id": "cycle",  "label": "Full cycle", "emoji": "🌙", "minutes": 90},
]


async def log_nap(db: AsyncSession, day: Date, minutes: float,
                  at_time: str | None = None) -> HealthMetric:
    return await _insert_metric(db, "nap", _to_utc_naive(day, at_time), float(minutes), "min")


def scale_macros(macros: dict | None, qty: float) -> dict | None:
    """Macros are per SERVING, so they scale with quantity exactly as calories do.

    Missing this is a silent under-count rather than an error: two IPAs logged as one
    entry would report 400 kcal but a single serving's carbohydrate, quietly biasing
    the very macro goals this feature exists to keep honest.
    """
    if not macros:
        return None
    q = float(qty or 1.0)
    return {k: round(float(v) * q, 2) for k, v in macros.items() if v is not None}


async def sync_nutrition_entry(db: AsyncSession, *, source_kind: str, source_id: int,
                               label: str, kcal: float | None, ts_utc: datetime,
                               macros: dict | None = None,
                               my_food_id: int | None = None,
                               servings: float | None = None,
                               drink_kind: str | None = None,
                               brand: str | None = None) -> None:
    """Create/refresh the linked 'drinks' entry in the food log for one drink.

    A preset with no calories creates nothing — a 5 kcal black coffee is clutter, not
    data — and if a preset's kcal is later cleared, any existing linked row is removed.

    TIMESTAMP: food_log is naive LOCAL while health_metrics is naive UTC, so the drink's
    UTC instant is converted back to local here. Skipping this would file evening drinks
    against the following day in the nutrition totals.
    """
    from app.models import FoodLog

    existing = (await db.execute(
        select(FoodLog).where(FoodLog.source_kind == source_kind)
                       .where(FoodLog.source_id == source_id)
    )).scalar_one_or_none()

    if not kcal or float(kcal) <= 0:
        if existing is not None:
            await db.delete(existing)
            await db.commit()
        return

    local_naive = to_local(ts_utc).replace(tzinfo=None)
    row = existing or FoodLog(source_kind=source_kind, source_id=source_id)
    row.timestamp = local_naive
    row.name = label
    row.description = label
    row.meal_type = "drinks"
    row.calories = float(kcal)
    # A preset's macros dict is AUTHORITATIVE, not partial: whoever wrote it listed
    # what the drink contains, so anything unlisted is 0, not unknown. Writing NULL
    # for those was a quiet lie — beer rows carried carbs and protein but a NULL
    # fat_g, which reads as "not measured" for a drink that definitionally has none.
    # SQL SUM ignores NULL so the daily totals were never wrong, but any consumer
    # that branches on None sees a hole that isn't there.
    # No macros dict at all is different and stays NULL: that is a preset which never
    # said anything, and defaulting it to all-zero would invent measurements for a
    # 190 kcal latte the moment someone adds one without filling the field in.
    if macros:
        for k in ("protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g", "sodium_mg"):
            setattr(row, k, float(macros.get(k) or 0.0))
    if drink_kind is not None:      # an edit passes none and keeps what was logged
        row.drink_kind = drink_kind
    if brand is not None:           # a preset's or a typed drink's brand (note 98); edits keep it
        row.brand = brand
    # A drink from the library (0067, note 34) links its row like any My Food entry.
    if my_food_id is not None:
        row.my_food_id = my_food_id
        row.servings = servings
        # …and carries its brand, like a My Food logged through the food sheet (0070).
        from app.models import MyFood
        row.brand = (await db.execute(
            select(MyFood.brand).where(MyFood.id == my_food_id))).scalar_one_or_none()
    if existing is None:
        db.add(row)
    await db.commit()


async def unsync_nutrition_entry(db: AsyncSession, source_kind: str, source_id: int) -> None:
    """Remove the linked nutrition row when its drink is deleted, so the two cannot drift."""
    from app.models import FoodLog
    row = (await db.execute(
        select(FoodLog).where(FoodLog.source_kind == source_kind)
                       .where(FoodLog.source_id == source_id)
    )).scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.commit()


async def update_entry(db: AsyncSession, metric_type: str, row_id: int, *,
                       value: float | None = None, day: Date | None = None,
                       at_time: str | None = None) -> HealthMetric | None:
    """Edit a logged instance after the fact — its amount, its time, or both.

    The timestamp is rebuilt from the row's own LOCAL day unless a new day is given,
    so correcting a time cannot silently move an entry to another date. Collisions are
    stepped past exactly as on insert.
    """
    row = (await db.execute(
        select(HealthMetric).where(HealthMetric.id == row_id)
        .where(HealthMetric.metric_type == metric_type)
    )).scalar_one_or_none()
    if row is None:
        return None
    if value is not None:
        row.value = float(value)
    if at_time or day:
        local_day = day or to_local_date(row.timestamp)
        ts = _to_utc_naive(local_day, at_time) if at_time else _to_utc_naive(local_day, None)
        for _ in range(120):
            clash = (await db.execute(
                select(HealthMetric.id).where(HealthMetric.metric_type == metric_type)
                .where(HealthMetric.timestamp == ts).where(HealthMetric.id != row_id)
            )).scalar_one_or_none()
            if clash is None:
                break
            ts = ts + timedelta(seconds=1)
        row.timestamp = ts
    await db.commit()
    await db.refresh(row)
    return row


def serialize(r: HealthMetric) -> dict:
    return {"id": r.id, "timestamp": r.timestamp.isoformat(),
            "value": r.value, "unit": r.unit, "source": r.source}


async def day_entries(db: AsyncSession, metric_type: str, day: Date) -> dict:
    """All instances on the LOG date bar's day — a food day, 04:00 → 04:00 local
    (app/foodday.py) — plus the total. Analytics still bucket by calendar day."""
    start, end = food_day_bounds_utc(day)
    rows = (await db.execute(
        select(HealthMetric)
        .where(HealthMetric.metric_type == metric_type)
        .where(HealthMetric.timestamp >= start).where(HealthMetric.timestamp < end)
        .order_by(HealthMetric.timestamp)
    )).scalars().all()
    return {"date": day.isoformat(),
            "total": round(sum(r.value or 0 for r in rows), 2),
            "entries": [serialize(r) for r in rows]}


async def delete_entry(db: AsyncSession, metric_type: str, row_id: int) -> bool:
    row = (await db.execute(
        select(HealthMetric).where(HealthMetric.id == row_id)
        .where(HealthMetric.metric_type == metric_type)
    )).scalar_one_or_none()
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    # Drop the paired nutrition row too — a linked pair that only half-deletes is
    # exactly the drift this design exists to prevent.
    await unsync_nutrition_entry(db, metric_type, row_id)
    return True
