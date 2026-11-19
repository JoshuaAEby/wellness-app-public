"""Caffeine instance logging → dose + pharmacokinetic 'onboard at bedtime'.

The physiologically meaningful exposure isn't total daily mg or the last dose's
time — it's how much caffeine is still in the bloodstream at bedtime, a
decay-weighted sum over every dose:

    onboard_at_bed = Σ ∫ (mg_i/D_i) · 0.5 ^ ((bedtime − s) / HALF_LIFE_H) ds

integrated over each dose's drinking window D_i (see `onboard_at_bed`; it was a
plain bolus sum until 2026-08-30).

A 200 mg coffee at 3pm leaves ~2.6× more at an 11pm bed than the same coffee at
8am. HALF_LIFE_H is a population average (~5 h); it varies by person (CYP1A2),
but for WITHIN-person day-to-day comparison it's a monotonic transform, so the
ranking of days by residual — which is all the correlation engine uses — is
robust to the exact value.

Completeness: a PAST day's caffeine is complete if it has ≥1 instance OR a
CaffeineDay ack ('none'/'done'). Incomplete days exclude ONLY the caffeine
correlation inputs (a partial log understates the dose), not the whole day.

Instance logging also keeps HabitLog.caffeine_last_min in sync (= latest dose,
local minutes) so the existing no_late_caffeine / caffeine_last_hour chain
derives automatically without a second code path.
"""
import logging
import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import CaffeineDay, CaffeineLog, HabitLog
from app.timeutils import local_day_bounds_utc, local_today, to_local, to_local_date, zone_for_date

logger = logging.getLogger(__name__)

HALF_LIFE_H = 5.0

# Starter presets (mg is the standard single-serving dose; quantity multiplies).
# Approximate — meant to be recalibrated. id is stable; label/emoji are display.
# `kcal` seeds a linked Nutrition "drinks" entry (services/intake.sync_nutrition_entry).
# 0 means no nutrition row is created at all — black coffee is not worth a food-log
# line. Milk drinks are where this matters, so those carry real numbers.
# Any preset with kcal > 0 needs a `macros` dict even when the answer is ~nothing:
# without one its linked food_log row is written with every macro NULL, which reads
# as unmeasured rather than as the trace it is. sync_nutrition_entry treats a macros
# dict as complete, so listing only what is present is enough.
#
# `oz` is the drink's VOLUME, for total fluids (services/fluids, note 78). It lives on
# the preset, not the entry, so editing it re-values that preset's whole history.
PRESETS = [
    {"id": "coffee_mug",     "label": "Coffee (mug)",       "emoji": "☕", "mg": 140, "kcal": 0, "oz": 12},
    {"id": "coffee_small",   "label": "Coffee (small)",     "emoji": "☕", "mg": 95,  "kcal": 0, "oz": 8},
    {"id": "espresso",       "label": "Espresso",           "emoji": "⚡", "mg": 65,  "kcal": 0, "oz": 1},
    {"id": "espresso_double", "label": "Double espresso",   "emoji": "⚡", "mg": 130, "kcal": 0, "oz": 2},
    {"id": "sbux_brewed",    "label": "SBUX grande brewed", "emoji": "🟢", "mg": 310, "kcal": 5, "oz": 16,
     "macros": {"carbs_g": 1.0}},
    {"id": "sbux_latte",     "label": "SBUX grande latte",  "emoji": "🥛", "mg": 150, "kcal": 190, "oz": 16,
     "macros": {"carbs_g": 18.0, "protein_g": 13.0, "fat_g": 7.0}},
    {"id": "sbux_coldbrew",  "label": "SBUX cold brew",     "emoji": "🧊", "mg": 205, "kcal": 5, "oz": 16,
     "macros": {"carbs_g": 1.0}},
    {"id": "tea",            "label": "Tea",                "emoji": "🍵", "mg": 45,  "kcal": 0, "oz": 8},
    {"id": "soda",           "label": "Soda",               "emoji": "🥤", "mg": 35, "oz": 12},
]
PRESET_BY_ID = {p["id"]: p for p in PRESETS}


async def effective_presets(db: AsyncSession) -> list:
    """The user's custom preset list if set, else the hardcoded defaults.

    `kcal` is backfilled from the default of the same id when a saved preset predates
    that field — customised lists were stored before calories existed, so without this
    an edited preset would silently log zero calories forever.
    """
    from app.models import UserSettings
    custom = (await db.execute(
        select(UserSettings.caffeine_presets).limit(1))).scalar_one_or_none()
    if not custom:
        return PRESETS
    by_id = {p["id"]: p for p in PRESETS}
    # `oz` the same way (note 78): lists saved before volume existed get the default's,
    # and a custom preset with no default stays without one (counted as unknown volume).
    return [{**p,
             "kcal": p.get("kcal", by_id.get(p["id"], {}).get("kcal", 0)),
             **({"oz": by_id[p["id"]]["oz"]}
                if not p.get("oz") and by_id.get(p["id"], {}).get("oz") else {}),
             **({"macros": by_id[p["id"]]["macros"]}
                if p.get("macros") is None and by_id.get(p["id"], {}).get("macros") else {})}
            for p in custom]


async def save_presets(db: AsyncSession, presets: list | None) -> list:
    """Persist a custom preset list (None resets to defaults). Returns effective."""
    from app.models import UserSettings
    row = (await db.execute(select(UserSettings).limit(1))).scalar_one_or_none()
    if row is None:
        row = UserSettings()
        db.add(row)
    row.caffeine_presets = presets
    await db.commit()
    return presets if presets else PRESETS


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.tz)


def _local_minutes(ts: datetime) -> int:
    """Naive-UTC timestamp → minutes since local midnight."""
    local = to_local(ts)
    return local.hour * 60 + local.minute


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    """Half-open [start, end) bounds of a LOCAL day, as naive UTC.

    FIXED 2026-08-06. This previously returned `datetime.combine(day, time.min)` to
    `time.max` — naive UTC midnight-to-midnight — which is the exact mistake
    the "naive-UTC storage vs local today" rule warns about. Timestamps are
    stored naive UTC, so a UTC-day window bucketed everything from 20:00 local
    onwards (EDT = UTC-4) onto the FOLLOWING day.

    That misfiled precisely the doses that matter most: `onboard_at_bed` is a decay
    from dose time to bedtime, so an evening coffee dominates it — and it was being
    excluded from that night and added to the next. It also drove
    caffeine_last_min → no_late_caffeine for the wrong day, leaving the real late
    day looking clean and charging a late dose to the next morning.
    """
    return local_day_bounds_utc(day)


async def day_instances(db: AsyncSession, day: date) -> list[CaffeineLog]:
    start, end = _day_bounds(day)
    return list((await db.execute(
        select(CaffeineLog).where(CaffeineLog.timestamp >= start)
        .where(CaffeineLog.timestamp < end)      # half-open: end is next local midnight
        .order_by(CaffeineLog.timestamp)
    )).scalars().all())


async def _sync_caffeine_last_min(db: AsyncSession, day: date) -> None:
    """Bridge: set HabitLog.caffeine_last_min = latest instance time (local min)
    for `day`, so no_late_caffeine / caffeine_last_hour keep deriving. Cleared to
    None when the day has no instances (the manual toggle path takes over)."""
    rows = await day_instances(db, day)
    last_min = max((_local_minutes(r.timestamp) for r in rows), default=None)

    existing = (await db.execute(
        select(HabitLog).where(HabitLog.date == day))).scalar_one_or_none()
    if existing is None and last_min is None:
        return
    from app.models import UserSettings
    cutoff = (await db.execute(
        select(UserSettings.caffeine_cutoff_hour).limit(1))).scalar() or 14
    no_late = None if last_min is None else last_min <= cutoff * 60

    values = {"date": day, "caffeine_last_min": last_min}
    if last_min is not None:
        values["no_late_caffeine"] = no_late
    stmt = sqlite_insert(HabitLog).values(**values)
    stmt = stmt.on_conflict_do_update(index_elements=["date"],
                                      set_={k: v for k, v in values.items() if k != "date"})
    await db.execute(stmt)
    await db.commit()


async def add_instance(db: AsyncSession, day: date, kind: str, mg: float,
                       quantity: float, at: datetime | None) -> CaffeineLog:
    """Log a dose. `at` (naive UTC) defaults to now; for backfill it's noon-on-day
    unless a specific time is given."""
    if at is None:
        # Today OR the food day in progress: until 04:00 the LOG date bar shows
        # yesterday (app/foodday.py), and a dose logged then happened now, not at noon.
        from app.foodday import food_today
        if day == local_today() or day == food_today():
            at = datetime.now(timezone.utc).replace(tzinfo=None)
        else:
            # Past day with no explicit time → local noon (a neutral default).
            local_noon = datetime.combine(day, time(12, 0)).replace(tzinfo=zone_for_date(day))
            at = local_noon.astimezone(timezone.utc).replace(tzinfo=None)
    row = CaffeineLog(timestamp=at, kind=kind, mg=mg, quantity=quantity)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    # Logging an instance implicitly un-marks a 'none' ack for that day.
    await _clear_none_ack(db, day)
    await _sync_caffeine_last_min(db, day)
    return row


async def delete_instance(db: AsyncSession, instance_id: int) -> date | None:
    row = (await db.execute(
        select(CaffeineLog).where(CaffeineLog.id == instance_id))).scalar_one_or_none()
    if row is None:
        return None
    day = to_local_date(row.timestamp)
    await db.delete(row)
    await db.commit()
    await _sync_caffeine_last_min(db, day)
    return day


async def update_instance(db: AsyncSession, instance_id: int, at: datetime | None = None,
                          mg: float | None = None,
                          quantity: float | None = None) -> CaffeineLog | None:
    """Edit a logged dose — principally its TIME.

    Timing is the whole point of instance logging: onboard_at_bed decays as
    0.5^(hours/HALF_LIFE), so a dose recorded 3 h from its true time misstates the
    bedtime residual by ~1.5x. Until this existed the only recordable times were
    "the moment I tapped" and the noon backfill default, both uncorrectable.

    Re-syncs caffeine_last_min for BOTH the old and new local day, because moving a
    timestamp across midnight changes which day's no_late_caffeine derives from it.
    """
    row = (await db.execute(
        select(CaffeineLog).where(CaffeineLog.id == instance_id))).scalar_one_or_none()
    if row is None:
        return None

    old_day = to_local_date(row.timestamp)
    if at is not None:
        row.timestamp = at
    if mg is not None:
        row.mg = mg
    if quantity is not None:
        row.quantity = quantity
    await db.commit()
    await db.refresh(row)

    new_day = to_local_date(row.timestamp)
    for d in {old_day, new_day}:
        await _sync_caffeine_last_min(db, d)
    return row


async def _clear_none_ack(db: AsyncSession, day: date) -> None:
    await db.execute(delete(CaffeineDay).where(CaffeineDay.date == day)
                     .where(CaffeineDay.ack == "none"))
    await db.commit()


async def set_ack(db: AsyncSession, day: date, ack: str) -> None:
    """Mark a day complete: 'none' (no caffeine) or 'done' (finished logging)."""
    stmt = sqlite_insert(CaffeineDay).values(date=day, ack=ack)
    stmt = stmt.on_conflict_do_update(index_elements=["date"], set_={"ack": ack})
    await db.execute(stmt)
    await db.commit()


async def clear_ack(db: AsyncSession, day: date) -> None:
    await db.execute(delete(CaffeineDay).where(CaffeineDay.date == day))
    await db.commit()


async def _ack_dates(db: AsyncSession, start: date, end: date) -> set:
    rows = (await db.execute(
        select(CaffeineDay.date).where(CaffeineDay.date >= start).where(CaffeineDay.date <= end)
    )).all()
    return {r[0] for r in rows}


async def _instance_dates(db: AsyncSession, start: date, end: date) -> set:
    """Local dates in range that have ≥1 instance."""
    s, _ = _day_bounds(start)
    _, e = _day_bounds(end)
    rows = (await db.execute(
        select(CaffeineLog.timestamp).where(CaffeineLog.timestamp >= s).where(CaffeineLog.timestamp <= e)
    )).all()
    return {to_local_date(r[0]) for r in rows}


async def incomplete_dates(db: AsyncSession, start: date, end: date) -> set:
    """PAST days in range with neither an instance nor an ack → caffeine inputs
    are unreliable (unknown whether zero or unlogged)."""
    today = local_today()
    hi = min(end, today - timedelta(days=1))
    if hi < start:
        return set()
    have = await _instance_dates(db, start, hi)
    have |= await _ack_dates(db, start, hi)
    return {start + timedelta(days=i)
            for i in range((hi - start).days + 1)
            if (start + timedelta(days=i)) not in have}


async def recent_incomplete(db: AsyncSession, days_back: int = 7) -> list:
    today = local_today()
    inc = await incomplete_dates(db, today - timedelta(days=days_back), today - timedelta(days=1))
    return sorted(inc, reverse=True)


DEFAULT_INTAKE_MIN = 60.0

# How long a serving actually takes to drink. A dose is modelled as a uniform
# infusion over this window rather than an instantaneous bolus (user note,
# 2026-08-24: "there should be an assumption that I drink it in about an hour").
# A shot gets thrown back; a mug gets nursed. These are editable ASSUMPTIONS, not
# measurements — only the espressos are confidently shorter than the default.
INTAKE_MIN_BY_KIND = {
    "espresso":        10.0,
    "espresso_double": 10.0,
}


def _intake_min(kind: str | None) -> float:
    return INTAKE_MIN_BY_KIND.get(kind or "", DEFAULT_INTAKE_MIN)


def onboard_at_bed(instances: list, bedtime: datetime) -> float | None:
    """Caffeine (mg) still on board at `bedtime` (naive UTC), decay-weighted.

    Each dose is a UNIFORM INFUSION over its drinking window, not a bolus. For a
    dose that starts u_hi hours before bed and finishes u_lo hours before it:

        onboard = (mg/D) · (τ/ln2) · (2^(−u_lo/τ) − 2^(−u_hi/τ))

    the exact integral of bolus decay over the window, which collapses to the old
    mg·2^(−u/τ) as D→0. Clamping u_lo at 0 handles a drink still in hand at
    bedtime: only the part actually drunk by then counts. Doses starting at or
    after bedtime are ignored.

    WHAT THIS DOES AND DOESN'T CHANGE. For a single fixed window the correction is
    a CONSTANT factor — at D=1h, τ=5h it is ×1.0726, exactly equivalent to logging
    every dose 30 min later. The correlation engine ranks days (Spearman), and a
    constant factor preserves ranks, so on its own this moves no correlation. It
    only bites where windows DIFFER between drinks, which is the whole reason
    espresso carries a shorter one: a thrown-back double and a nursed venti really
    do leave different residuals.
    """
    if bedtime is None:
        return None
    total = 0.0
    for r in instances:
        u_hi = (bedtime - r.timestamp).total_seconds() / 3600.0
        if u_hi <= 0:
            continue
        d_h = _intake_min(getattr(r, "kind", None)) / 60.0
        if d_h <= 0:
            total += r.mg * (0.5 ** (u_hi / HALF_LIFE_H))
            continue
        # Clamping at 0 handles a drink still in hand at bedtime: the integral then
        # spans only the part actually drunk, so no extra partial-dose term is needed.
        u_lo = max(u_hi - d_h, 0.0)
        total += (r.mg / d_h) * (HALF_LIFE_H / math.log(2)) * (
            0.5 ** (u_lo / HALF_LIFE_H) - 0.5 ** (u_hi / HALF_LIFE_H)
        )
    return round(total, 1)


async def day_summary(db: AsyncSession, day: date) -> dict:
    rows = await day_instances(db, day)
    ack = (await db.execute(
        select(CaffeineDay.ack).where(CaffeineDay.date == day))).scalar_one_or_none()
    total_mg = round(sum(r.mg for r in rows), 1)
    complete = bool(rows) or ack is not None or day >= local_today()

    # Bedtime residual — the quantity the correlation engine actually uses, and
    # until now visible nowhere in the app. Against the night that FOLLOWS `day`
    # (services/bedtime), projected for today since that night hasn't happened.
    from app.services.bedtime import bedtime_for_day
    bed, bed_actual = await bedtime_for_day(db, day)
    onboard = onboard_at_bed(rows, bed) if bed else None

    return {
        "date": day.isoformat(),
        "instances": [_serialize(r) for r in rows],
        "total_mg": total_mg,
        "ack": ack,
        "complete": complete,
        "onboard_at_bed": onboard,
        "bedtime_projected": (not bed_actual) if bed else None,
    }


def _serialize(r: CaffeineLog) -> dict:
    tz = ZoneInfo(settings.tz)
    local = to_local(r.timestamp)
    return {
        "id": r.id,
        "kind": r.kind,
        "mg": r.mg,
        "quantity": r.quantity,
        "time": local.strftime("%H:%M"),
        "timestamp": r.timestamp.isoformat(),
    }
