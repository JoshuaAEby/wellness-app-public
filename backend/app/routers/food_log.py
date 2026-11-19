from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional
import json
import re

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import and_, case, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models import FoodLog, FoodParseCache
from app.services import nutrition_completeness as nc
from app.timeutils import local_today, local_now
from app.foodday import food_date_sql, food_day, food_day_end, food_day_start, food_timestamp, food_today

router = APIRouter(prefix="/api/nutrition", tags=["nutrition"])


def _clean_brand(b: Optional[str]) -> Optional[str]:
    """Trimmed brand, or None. Empty string means "no brand" (how an edit clears it)."""
    b = (b or "").strip()
    return b[:40] or None


def _serialize(r: FoodLog) -> dict:
    return {
        "id": r.id,
        "date": food_day(r.timestamp).isoformat(),     # the FOOD day (04:00 boundary)
        "timestamp": r.timestamp.isoformat(),
        "name": r.name,
        "brand": r.brand,
        "meal_type": r.meal_type or "snack",
        "description": r.description,
        "portion_quantity": r.portion_quantity,
        "portion_unit": r.portion_unit,
        "calories": r.calories,
        "protein_g": r.protein_g,
        "carbs_g": r.carbs_g,
        "fat_g": r.fat_g,
        "fiber_g": r.fiber_g,
        "sugar_g": r.sugar_g,
        "sodium_mg": r.sodium_mg,
        "ai_notes": r.llm_notes,
        "confidence": r.confidence,
        "omitted_kcal": r.omitted_kcal,
        "restaurant": r.restaurant,
        "is_saved": bool(r.is_saved),
        "skipped": bool(r.skipped),
        "time_estimated": bool(r.time_estimated),
        "my_food_id": r.my_food_id,
        "servings": r.servings,
        "recipe_id": r.recipe_id,
        "combo_group": r.combo_group,
        "combo_part": r.combo_part,
        "combo_name": r.combo_name,
        # 'alcohol' / 'caffeine' when a drink logger wrote this row (its dose lives there).
        "source_kind": r.source_kind,
    }


# ── Parse endpoint (Claude Haiku, no DB write) ────────────────────────────────

class ParseRequest(BaseModel):
    description: str
    meal_type: str = "snack"
    # True = skip the repeat lookup and ask the model (the "Re-estimate" button).
    fresh: bool = False


# ── Instant repeats (2026-09-13) ──────────────────────────────────────────────
# 69% of food entries repeat an earlier one. The picker handles most, but about a
# third of repeats were retyped and sent to the model again: a 3–6 s wait, a billed
# call, and numbers that could drift between days for the same food. So an exact
# retype returns what was SAVED for that text — the user's edits included — and only
# new text reaches the model.

_ITEM_FIELDS = ("name", "brand", "description", "portion_quantity", "portion_unit", "calories",
                "protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g", "sodium_mg",
                "my_food_id", "servings", "restaurant")


async def _known_restaurant(db, my_food_id, name, brand) -> Optional[bool]:
    """The last known restaurant answer for the same food (note 84): a re-log of a My Food
    or of a recent carries it without asking again. My Food first, then name + brand."""
    q = select(FoodLog.restaurant).where(FoodLog.restaurant.isnot(None))
    if my_food_id:
        q = q.where(FoodLog.my_food_id == my_food_id)
    elif (name or "").strip():
        q = q.where(func.lower(func.trim(FoodLog.name)) == name.strip().lower())
        q = q.where(func.lower(func.coalesce(FoodLog.brand, "")) == (brand or "").strip().lower())
    else:
        return None
    return (await db.execute(q.order_by(FoodLog.timestamp.desc()).limit(1))).scalar()


def _text_key(text: str) -> str:
    """Case, spacing and trailing punctuation do not make a different food."""
    return re.sub(r"\s+", " ", (text or "").strip().lower()).rstrip(" .,;!")


def _cache_item(i) -> dict:
    """A saved batch item in the parser's own item shape, so the client renders it
    exactly like a fresh parse."""
    d = {f: getattr(i, f) for f in _ITEM_FIELDS}
    d.update(notes=i.ai_notes, confidence=i.confidence, omitted_kcal=i.omitted_kcal,
             portion_options=None, combo_group=i.combo_group, combo_part=i.combo_part,
             combo_name=i.combo_name, caffeine_mg=i.caffeine_mg, alcohol_abv=i.alcohol_abv,
             alcohol_oz=i.alcohol_oz)
    return d


async def _repeat_lookup(db: AsyncSession, text: str) -> Optional[dict]:
    """Saved result for this exact text, else the newest single-food history entry
    whose own text matches. None = genuinely new text; ask the model."""
    key = _text_key(text)
    if not key:
        return None
    hit = (await db.execute(
        select(FoodParseCache).where(FoodParseCache.text_key == key)
    )).scalar_one_or_none()
    if hit is not None:
        hit.hits = (hit.hits or 0) + 1
        await db.commit()
        return {"items": json.loads(hit.items_json),
                "cached": {"source": "saved",
                           "date": hit.updated_at.date().isoformat() if hit.updated_at else None}}
    # History fallback — covers everything logged before the cache existed. Only a
    # SINGLE-food entry qualifies: in a multi-food meal each row carries the parser's
    # per-item text, not what was typed.
    rows = (await db.execute(
        select(FoodLog).where(FoodLog.source_kind.is_(None))
        .where((FoodLog.skipped == False) | (FoodLog.skipped.is_(None)))  # noqa: E712
        .order_by(FoodLog.timestamp.desc()).limit(3000)
    )).scalars().all()
    for r in rows:
        if _text_key(r.description) != key:
            continue
        if sum(1 for s in rows if s.timestamp == r.timestamp and s.meal_type == r.meal_type) != 1:
            continue
        item = {f: getattr(r, f) for f in _ITEM_FIELDS}
        item.update(notes=r.llm_notes, confidence=r.confidence, omitted_kcal=r.omitted_kcal,
                    portion_options=None)
        return {"items": [item],
                "cached": {"source": "history", "date": food_day(r.timestamp).isoformat()}}
    return None


async def _with_my_foods(db: AsyncSession, result: dict) -> dict:
    """My Foods is the source of truth for any item it knows — fresh parse, cached
    repeat or history match alike (docs/MY_FOODS.md)."""
    from app.services.my_foods import apply_my_foods, split_known_brands
    await split_known_brands(db, (result or {}).get("items") or [])
    return await apply_my_foods(db, result)


async def _my_foods_reference(db: AsyncSession) -> str:
    """The user's My Foods, handed to the parser so it names them EXACTLY (Phase 2).

    Replaces the hard-coded Kirkland-bar entry in the prompt: without an anchor the
    model may call the bar "Kirkland Signature Protein Bar", which misses the library's
    key and gets guessed numbers. The numbers here are guidance only — apply_my_foods
    overwrites a matched item with the library's own values regardless.
    """
    from app.models import MyFood
    from app.services.my_foods import food_components
    foods = (await db.execute(
        select(MyFood).where(MyFood.archived.isnot(True)).order_by(MyFood.name)
    )).scalars().all()
    if not foods:
        return ""
    by_id = {f.id: f for f in foods}
    # The parser sees BRAND + NAME ("Kirkland Cream cheese"); split_known_brands moves
    # the brand back out of what it returns, so the item keys to the My Food (0070).
    def full(f):
        return f"{f.brand} {f.name}" if f.brand else f.name
    lines = []
    for f in foods:
        if f.role == "combo":
            parts = " + ".join(full(by_id[c["my_food_id"]]) for c in food_components(f) if c["my_food_id"] in by_id)
            lines.append(f"- {full(f)} (a combination of {parts}) — ONE item with exactly this name")
        else:
            lines.append(f"- {full(f)}: 1 serving = {f.serving_label or f'{f.serving_qty:g} {f.serving_unit}'}; "
                         f"{f.calories:g} kcal, P {f.protein_g or 0:g} g, C {f.carbs_g or 0:g} g, "
                         f"F {f.fat_g or 0:g} g, fiber {f.fiber_g or 0:g} g")
    return ("THE USER'S OWN FOODS (checked numbers). When the text refers to one of these — "
            "including obvious variants such as the store name — use EXACTLY this name as the "
            "item name, give portion_quantity/portion_unit in its servings' units, and use these "
            "numbers:\n" + "\n".join(lines) + "\n\n")


@router.post("/parse")
async def parse_food(
    body: ParseRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Estimate nutrition for a natural-language food description. Does not save to DB.

    An exact retype of earlier text is answered from what was saved (see
    _repeat_lookup) with `cached` set; `fresh` forces the model."""
    if not body.fresh:
        hit = await _repeat_lookup(db, body.description)
        if hit is not None:
            return await _with_my_foods(db, hit)
    from app.services.ai_summaries import parse_food_description
    result = await parse_food_description(body.description, body.meal_type,
                                          await _my_foods_reference(db))
    if result is None:
        # Say what ACTUALLY failed. This message used to read "check that your API
        # key is set and AI is enabled in Settings" — and on 2026-09-04, when the
        # Anthropic credit balance ran out, it sent the user to check two things
        # that were both fine while the real cause was invisible.
        from app.services import ai_health
        snap = ai_health.snapshot()
        detail = snap["label"]
        if snap.get("action"):
            detail += f" — {snap['action']}"
        raise HTTPException(status_code=503, detail=detail)
    return await _with_my_foods(db, result)


# ── Background parsing (2026-10-21) ─────────────────────────────────────────────────
#
# "Parse in background" in the log modal: the job runs the same path as /parse above and
# waits in parse_jobs until reviewed (the modal reopens pre-filled) — nothing reaches
# food_log without the usual review-and-save. A job interrupted by a restart is resumed
# at boot (scheduler: resume_parse_jobs).

class ParseJobIn(BaseModel):
    description: str
    meal_type: str = "snack"
    date: Optional[str] = None        # YYYY-MM-DD; default today
    time: Optional[str] = None        # HH:MM; default = meal slot at save time


# Strong references to running parse tasks: the event loop only keeps weak ones, so a
# fire-and-forget create_task() can be garbage-collected mid-parse.
_PARSE_TASKS: set = set()


def _spawn_parse(job_id: str) -> None:
    import asyncio
    t = asyncio.create_task(run_parse_job(job_id))
    _PARSE_TASKS.add(t)
    t.add_done_callback(_PARSE_TASKS.discard)


def _job_out(j) -> dict:
    return {"id": j.id, "description": j.description, "meal_type": j.meal_type,
            "date": j.log_date.isoformat(), "time": j.log_time, "status": j.status,
            "result": json.loads(j.result) if j.result else None, "error": j.error,
            "created_at": j.created_at.isoformat() if j.created_at else None}


async def run_parse_job(job_id: str) -> None:
    """Run one job to completion in its own session. Never raises."""
    from app.database import AsyncSessionLocal
    from app.models import ParseJob
    async with AsyncSessionLocal() as db:
        job = await db.get(ParseJob, job_id)
        if job is None or job.status != "pending":
            return
        try:
            hit = await _repeat_lookup(db, job.description)
            if hit is not None:
                out = await _with_my_foods(db, hit)
            else:
                from app.services.ai_summaries import parse_food_description
                res = await parse_food_description(job.description, job.meal_type,
                                                   await _my_foods_reference(db))
                if res is None:
                    from app.services import ai_health
                    snap = ai_health.snapshot()
                    raise RuntimeError(snap["label"] + (f" — {snap['action']}" if snap.get("action") else ""))
                out = await _with_my_foods(db, res)
            job.result, job.status = json.dumps(out, default=str), "done"
        except Exception as exc:
            job.status, job.error = "error", str(exc)[:300]
        job.finished_at = datetime.utcnow()
        await db.commit()


@router.post("/parse-jobs", status_code=201)
async def create_parse_job(
    body: ParseJobIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    import uuid
    from app.models import ParseJob
    if not body.description.strip():
        raise HTTPException(status_code=422, detail="nothing to parse")
    job = ParseJob(id=uuid.uuid4().hex, description=body.description.strip(),
                   meal_type=body.meal_type,
                   log_date=date.fromisoformat(body.date) if body.date else food_today(),
                   log_time=body.time or None, status="pending",
                   created_at=datetime.utcnow())
    db.add(job)
    await db.commit()
    _spawn_parse(job.id)
    return _job_out(job)


@router.get("/parse-jobs")
async def list_parse_jobs(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Jobs still waiting for review (pending / done / error), last 48 h, newest first."""
    from app.models import ParseJob
    rows = (await db.execute(
        select(ParseJob).where(ParseJob.status.in_(("pending", "done", "error")))
        .where(ParseJob.created_at >= datetime.utcnow() - timedelta(hours=48))
        .order_by(ParseJob.created_at.desc())
    )).scalars().all()
    return {"jobs": [_job_out(j) for j in rows]}


class ParseJobPatch(BaseModel):
    status: str                       # saved | dismissed | pending (= retry)


@router.patch("/parse-jobs/{job_id}")
async def update_parse_job(
    job_id: str,
    body: ParseJobPatch,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    from app.models import ParseJob
    if body.status not in ("saved", "dismissed", "pending"):
        raise HTTPException(status_code=422, detail="status must be saved, dismissed or pending")
    job = await db.get(ParseJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such job")
    job.status = body.status
    if body.status == "pending":
        job.error, job.result, job.finished_at = None, None, None
    await db.commit()
    if body.status == "pending":
        _spawn_parse(job.id)
    return _job_out(job)


# Default clock times (2026-09-13): breakfast 8, lunch 12, dinner 7 pm, dessert
# 8 pm — on ANY day, not just a backfill; a typed time always wins. Snacks and drinks
# have no usual time ("snack is who knows"): today they take the current clock, a past
# day gets _PAST_FALLBACK. Every filled-in time is flagged `time_estimated`, which does
# the job the old noon placeholder did by being recognisable (45% of entries had none).
_MEAL_SLOT = {"breakfast": time(8, 0), "lunch": time(12, 0), "dinner": time(19, 0),
              "dessert": time(20, 0)}
_PAST_FALLBACK = time(15, 0)


def _parse_hhmm(at_time: Optional[str]) -> Optional[time]:
    if at_time:
        try:
            hh, mm = (int(x) for x in at_time.split(":")[:2])
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                return time(hh, mm)
        except (ValueError, TypeError):
            pass
    return None


def _meal_timestamp(day: date, at_time: Optional[str],
                    meal_type: Optional[str] = None) -> datetime:
    """Naive LOCAL datetime for a meal (see create_log for why local, not UTC).

    Typed time > the meal's default slot (any day) > snacks/drinks: now for today,
    _PAST_FALLBACK for a past day.

    `day` is a FOOD day (app/foodday.py): a clock time before 04:00 is stored on the
    following calendar date, its real moment.
    """
    t = _parse_hhmm(at_time)
    if t is not None:
        return food_timestamp(day, t)
    slot = _MEAL_SLOT.get((meal_type or "").lower())
    if slot is not None:
        return food_timestamp(day, slot)
    if day == food_today():
        now = local_now()
        return food_timestamp(day, time(now.hour, now.minute))
    return food_timestamp(day, _PAST_FALLBACK)


def _time_is_estimate(day: date, at_time: Optional[str],
                      meal_type: Optional[str] = None) -> bool:
    """True whenever _meal_timestamp filled the time in — a meal slot on any day, or a
    past-day snack/drink. A snack logged today with no time takes 'now': not flagged."""
    if _parse_hhmm(at_time) is not None:
        return False
    if (meal_type or "").lower() in _MEAL_SLOT:
        return True
    return day != food_today()


# ── CRUD for confirmed log entries ────────────────────────────────────────────

class LogCreate(BaseModel):
    date: date
    # Local wall-clock time of the meal, "HH:MM". Optional so older clients and
    # backfilled entries keep working.
    at_time: Optional[str] = None
    name: Optional[str] = None
    brand: Optional[str] = None          # 0070, note 60 — optional, user-entered
    meal_type: str = "snack"
    description: str
    portion_quantity: Optional[float] = None
    portion_unit: Optional[str] = None
    calories: Optional[float] = None
    protein_g: Optional[float] = None
    carbs_g: Optional[float] = None
    fat_g: Optional[float] = None
    fiber_g: Optional[float] = None
    sugar_g: Optional[float] = None
    sodium_mg: Optional[float] = None
    ai_notes: Optional[str] = None
    # Parser self-reporting, passed straight through from /parse. Optional so an
    # older client (or a hand-typed entry) simply stores NULL rather than 422-ing.
    confidence: Optional[str] = None
    omitted_kcal: Optional[int] = None
    # My Foods link (0063) — stored as given; the macros above are the snapshot.
    my_food_id: Optional[int] = None
    servings: Optional[float] = None
    recipe_id: Optional[int] = None      # household recipe this came from (0068)
    restaurant: Optional[bool] = None    # note 84: True / False, None = unknown


@router.post("/logs", status_code=201)
async def create_log(
    body: LogCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Save a confirmed food log entry at its real local time.

    STORAGE CONVENTION: food_log.timestamp is naive LOCAL wall-clock, unlike
    health_metrics/workouts/sleep which are naive UTC. That is deliberate and must be
    preserved — every food query buckets days with plain `func.date(timestamp)`, which
    is only correct because the value is already local. Storing UTC here would roll
    every evening meal into the next day.

    Until 2026-08-17 the time was hardcoded to noon, so meal timing (eating window,
    last meal before bed, overnight fast) did not exist and CANNOT be recovered for
    older rows.
    """
    entry = FoodLog(
        timestamp=_meal_timestamp(body.date, body.at_time, body.meal_type),
        time_estimated=_time_is_estimate(body.date, body.at_time, body.meal_type),
        name=body.name,
        brand=_clean_brand(body.brand),
        meal_type=body.meal_type,
        description=body.description,
        portion_quantity=body.portion_quantity,
        portion_unit=body.portion_unit,
        calories=body.calories,
        protein_g=body.protein_g,
        carbs_g=body.carbs_g,
        fat_g=body.fat_g,
        fiber_g=body.fiber_g,
        sugar_g=body.sugar_g,
        sodium_mg=body.sodium_mg,
        llm_notes=body.ai_notes,
        confidence=body.confidence,
        omitted_kcal=body.omitted_kcal,
        my_food_id=body.my_food_id,
        servings=body.servings,
        recipe_id=body.recipe_id,
        restaurant=body.restaurant,
    )
    if entry.restaurant is None:
        entry.restaurant = await _known_restaurant(db, body.my_food_id, body.name, body.brand)
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    await nc.sync_day(db, body.date)     # may complete a past day → clear its flag
    return _serialize(entry)


class LogBatchItem(BaseModel):
    """One food within a batch. Deliberately NOT LogCreate: date, time and meal type
    describe the sitting and live on the envelope, so an item cannot carry its own
    and disagree with its siblings."""
    name: Optional[str] = None
    brand: Optional[str] = None          # 0070, note 60
    description: str
    portion_quantity: Optional[float] = None
    portion_unit: Optional[str] = None
    calories: Optional[float] = None
    protein_g: Optional[float] = None
    carbs_g: Optional[float] = None
    fat_g: Optional[float] = None
    fiber_g: Optional[float] = None
    sugar_g: Optional[float] = None
    sodium_mg: Optional[float] = None
    ai_notes: Optional[str] = None
    # ADDED 2026-09-07. Both shipped on 2026-09-04 and were SILENTLY DROPPED on every
    # batch write: Pydantic ignores unknown keys by default, the frontend was sending
    # them, and this model did not declare them. Because the multi-item parse (0902)
    # made batch the primary path, `omitted_kcal` was NULL on 35 of 35 rows logged since
    # it shipped — the parser computed the number every time and it was thrown away.
    #
    # This is why note 10 looked like it needed a new parser contract. It did not; it
    # needed two field declarations.
    confidence: Optional[str] = None
    omitted_kcal: Optional[int] = None
    # Doses parsed with a drink (note 100): saved as the caffeine or alcohol entry this
    # food row is linked to, so one entry carries both the macros and the dose.
    caffeine_mg: Optional[float] = None
    alcohol_abv: Optional[float] = None
    alcohol_oz: Optional[float] = None
    # Note 84: bought prepared from a restaurant / cafe / bar / food truck. None = unknown,
    # in which case the last known answer for the same My Food or name is used.
    restaurant: Optional[bool] = None
    my_food_id: Optional[int] = None
    servings: Optional[float] = None
    recipe_id: Optional[int] = None      # household recipe this came from (0068)
    # Grouping of a combo's parts. Kept in the repeat cache so a retype still scales
    # toppings with their base, and (0065, note 35) stored on food_log so Today can show
    # the combination as one row. The client's group id is only unique within the
    # modal; the batch replaces it with a fresh one per meal.
    combo_group: Optional[str] = None
    combo_part: Optional[str] = None
    combo_name: Optional[str] = None


# ── Quick-log suggestions (2026-10-21) ──────────────────────────────────────────────
#
# One-tap chips on Today/LOG for what you usually eat around NOW. Usage: ~11 entries/day,
# and the top foods repeat 15–55 times a month (protein bar 55, chicken sausage 34, bagel
# + cream cheese 15) — each a modal, a search/pick and a save. A chip replays the MOST
# RECENT instance's rows through the normal batch write (numbers, servings, My Food link,
# combo parts), so it is exactly "log that again", never a new estimate. Drinks with their
# own one-tap presets (caffeine, alcohol — source_kind set) are left to the intake card.

_QL_SKIP_NAMES = {"scanned nutrition label"}     # generic label scans: contents vary


@router.get("/quick-log")
async def quick_log_suggestions(
    limit: int = Query(default=8, ge=1, le=16),
    # per_meal > 0 (note 81, 2026-10-26): up to that many per meal, each item filed under
    # the meal it is USUALLY logged at (all day, not just ±3 h of now), for the LOG view's
    # meal tabs. `limit` is then ignored. Order within a meal is still time-of-day weighted.
    per_meal: int = Query(default=0, ge=0, le=6),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    import math
    from collections import Counter, defaultdict
    now = local_now()
    hour = now.hour + now.minute / 60
    rows = (await db.execute(
        select(FoodLog)
        .where(FoodLog.timestamp >= datetime.combine(local_today() - timedelta(days=120), time.min))
        .where(FoodLog.source_kind.is_(None))
        .where((FoodLog.skipped == False) | (FoodLog.skipped.is_(None)))  # noqa: E712
        .where(FoodLog.calories.is_not(None))
        .order_by(FoodLog.timestamp)
    )).scalars().all()

    # Group rows into "instances" (one combo sitting = one instance) and instances into
    # suggestion keys. food_log.timestamp is naive LOCAL time, so .hour is the clock hour.
    combos: dict = defaultdict(list)
    singles = []
    for r in rows:
        if (r.name or r.description or "").strip().lower() in _QL_SKIP_NAMES:
            continue
        (combos[r.combo_group].append(r) if r.combo_group else singles.append(r))
    instances = []                                # (key, label, [rows], ts, meal)
    for g, parts in combos.items():
        label = parts[0].combo_name or " + ".join((p.name or p.description) for p in parts)
        key = "combo:" + label.strip().lower()
        instances.append((key, label, parts, parts[0].timestamp, parts[0].meal_type))
    for r in singles:
        label = r.name or r.description
        key = f"mf:{r.my_food_id}" if r.my_food_id else "n:" + label.strip().lower()
        instances.append((key, label, [r], r.timestamp, r.meal_type))

    score: dict = defaultdict(float)
    latest: dict = {}
    meals: dict = defaultdict(Counter)
    meals_all: dict = defaultdict(Counter)
    count: Counter = Counter()
    today = local_today()
    for key, label, parts, ts, meal in instances:
        d = abs((ts.hour + ts.minute / 60) - hour)
        d = min(d, 24 - d)                         # circular clock distance
        w_hour = 1.0 if d <= 1.5 else 0.5 if d <= 3 else 0.08
        w_recent = math.exp(-(today - ts.date()).days / 45)
        score[key] += w_hour * w_recent
        count[key] += 1
        if d <= 3:
            meals[key][meal] += 1
        meals_all[key][meal] += 1
        if key not in latest or ts >= latest[key][2]:
            latest[key] = (label, parts, ts, meal)

    out, seen_labels = [], set()
    per_meal_n: Counter = Counter()
    for key in sorted(score, key=lambda k: -score[k]):
        if count[key] < 2:                          # a one-off isn't a habit
            continue
        label, parts, ts, meal = latest[key]
        # The same food logged two ways (a two-part combo vs an older single row with the
        # combined name) would show twice; keep the higher-scoring one.
        if label.strip().lower() in seen_labels:
            continue
        if per_meal:
            meal_type = (meals_all[key].most_common(1)[0][0] if meals_all[key] else meal) or "snack"
            if per_meal_n[meal_type] >= per_meal:
                continue
            per_meal_n[meal_type] += 1
        else:
            meal_type = (meals[key].most_common(1)[0][0] if meals[key] else meal) or "snack"
        seen_labels.add(label.strip().lower())
        items = [{
            "name": p.name, "brand": p.brand, "description": p.description,
            "portion_quantity": p.portion_quantity, "portion_unit": p.portion_unit,
            "calories": p.calories, "protein_g": p.protein_g, "carbs_g": p.carbs_g,
            "fat_g": p.fat_g, "fiber_g": p.fiber_g, "sugar_g": p.sugar_g,
            "sodium_mg": p.sodium_mg, "my_food_id": p.my_food_id, "servings": p.servings,
            "recipe_id": p.recipe_id,
            "combo_group": "quick" if len(parts) > 1 else None,
            "combo_part": p.combo_part if len(parts) > 1 else None,
            "combo_name": p.combo_name if len(parts) > 1 else None,
            "ai_notes": "Quick-logged: a repeat of your entry from " + ts.date().isoformat(),
        } for p in parts]
        out.append({"key": key, "label": label, "meal_type": meal_type,
                    "kcal": round(sum(p.calories or 0 for p in parts)),
                    "protein_g": round(sum(p.protein_g or 0 for p in parts)),
                    "times": count[key], "last": ts.date().isoformat(), "items": items})
        if not per_meal and len(out) >= limit:
            break
    return {"hour": round(hour, 2), "suggestions": out}


class LogBatchCreate(BaseModel):
    """One meal's worth of separately-identified foods, written in one shot."""
    date: date
    at_time: Optional[str] = None
    meal_type: str = "snack"
    # The text that was TYPED and parsed into these items. When present, what is
    # saved here becomes the instant answer the next time that text is typed. The
    # client sends it only when every item came from parsing that text.
    source_text: Optional[str] = None
    items: list[LogBatchItem]


@router.post("/logs/batch", status_code=201)
async def create_logs_batch(
    body: LogBatchCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Write N food rows from one parse.

    A five-item dinner used to be five modals and five parses, or one summed row
    like "meatball sub with olive oil potato chips" that no future meal can reuse
    and that food_components (0056) then has to guess the split of. One item per
    food fixes both, and the write has to be one transaction so a partial dinner
    can never land.

    Day/time/meal_type are set ONCE here rather than per item: they describe the
    sitting, not the food, and letting items disagree about which day they belong
    to is how a dinner ends up split across two dates. Every row shares one
    timestamp, so the meal reads as a single event in the log.
    """
    if not body.items:
        raise HTTPException(status_code=422, detail="No items to log")

    ts = _meal_timestamp(body.date, body.at_time, body.meal_type)
    est = _time_is_estimate(body.date, body.at_time, body.meal_type)
    # Client group ids ("c5-3", "u7", or the parser's "5-0") repeat across meals, so
    # each distinct one gets a fresh id here. A lone "group" is not a combination.
    from uuid import uuid4
    counts: dict[str, int] = {}
    for item in body.items:
        if item.combo_group:
            counts[item.combo_group] = counts.get(item.combo_group, 0) + 1
    fresh = {g: uuid4().hex[:12] for g, n in counts.items() if n > 1}
    created = []
    for item in body.items:
        grp = fresh.get(item.combo_group or "")
        entry = FoodLog(
            timestamp=ts,
            name=item.name,
            brand=_clean_brand(item.brand),
            meal_type=body.meal_type,
            description=item.description,
            portion_quantity=item.portion_quantity,
            portion_unit=item.portion_unit,
            calories=item.calories,
            protein_g=item.protein_g,
            carbs_g=item.carbs_g,
            fat_g=item.fat_g,
            fiber_g=item.fiber_g,
            sugar_g=item.sugar_g,
            sodium_mg=item.sodium_mg,
            llm_notes=item.ai_notes,
            confidence=item.confidence,
            omitted_kcal=item.omitted_kcal,
            time_estimated=est,
            my_food_id=item.my_food_id,
            servings=item.servings,
            recipe_id=item.recipe_id,
            combo_group=grp,
            combo_part=item.combo_part if grp else None,
            combo_name=((item.combo_name or "").strip() or None) if grp else None,
            restaurant=(item.restaurant if item.restaurant is not None
                        else await _known_restaurant(db, item.my_food_id, item.name, item.brand)),
        )
        db.add(entry)
        created.append(entry)

    await db.commit()
    for entry in created:
        await db.refresh(entry)
    await _log_parsed_doses(db, body, created, ts)
    # Once for the batch, not once per item — sync_day re-reads the whole day.
    await nc.sync_day(db, body.date)
    # Remember what was SAVED for the typed text, so typing it again is instant and
    # returns these numbers — edits included — instead of a fresh model guess.
    key = _text_key(body.source_text or "")
    if key:
        items_json = json.dumps([_cache_item(i) for i in body.items])
        await db.execute(
            sqlite_insert(FoodParseCache)
            .values(text_key=key, items_json=items_json, hits=0, updated_at=datetime.utcnow())
            .on_conflict_do_update(index_elements=["text_key"],
                                   set_={"items_json": items_json, "updated_at": datetime.utcnow()})
        )
        await db.commit()
    return [_serialize(e) for e in created]


async def _log_parsed_doses(db: AsyncSession, body, created: list, ts: datetime) -> None:
    """A parsed drink's caffeine or alcohol becomes a caffeine/alcohol entry at the meal's
    time, and its food row is linked to it (source_kind/source_id) exactly like a row the
    caffeine or alcohol buttons write: one drink, one entry, and deleting it removes both.
    Linked rows are left out of quick-log and "same as last meal", as those are. A drink
    with both (an espresso martini) links the alcohol; the caffeine is logged unlinked."""
    from app.services import caffeine as caf, intake
    from app.timeutils import zone_for_date
    at_utc = ts.replace(tzinfo=zone_for_date(ts.date())).astimezone(timezone.utc).replace(tzinfo=None)
    for item, entry in zip(body.items, created):
        if item.alcohol_abv and item.alcohol_oz:
            sdu = round(item.alcohol_oz * item.alcohol_abv / 100 / intake.ETHANOL_OZ_PER_SDU, 3)
            if sdu > 0:
                m = await intake.log_alcohol(db, body.date, sdu, ts.strftime("%H:%M"))
                entry.source_kind, entry.source_id = "alcohol", m.id
        if item.caffeine_mg:
            inst = await caf.add_instance(db, body.date, "custom", float(item.caffeine_mg), 1.0, at_utc)
            if entry.source_kind is None:
                entry.source_kind, entry.source_id = "caffeine", inst.id
    await db.commit()


class MealSkip(BaseModel):
    date: date
    meal_type: str


@router.post("/skip", status_code=201)
async def skip_meal(
    body: MealSkip,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Mark a meal intentionally skipped — a KNOWN zero, not missing data.

    Idempotent: re-skipping returns the existing skip row. Refuses to skip a meal
    that already has real food logged (that would misrepresent the day); the
    client should delete those entries first if the intent really is 'skipped'.
    Undo is a normal DELETE of the returned row.
    """
    existing = (await db.execute(
        select(FoodLog)
        .where(FoodLog.meal_type == body.meal_type)
        .where(FoodLog.timestamp >= food_day_start(body.date))
        .where(FoodLog.timestamp < food_day_end(body.date))
    )).scalars().all()

    already_skipped = next((e for e in existing if e.skipped), None)
    if already_skipped is not None:
        return _serialize(already_skipped)
    if existing:
        raise HTTPException(
            status_code=409,
            detail="Meal already has food logged; delete those entries before skipping.",
        )

    entry = FoodLog(
        timestamp=datetime.combine(body.date, time(12, 0, 0)),
        time_estimated=True,                 # a skipped meal has no time at all
        name="Skipped",
        meal_type=body.meal_type,
        description="(skipped)",
        skipped=True,
        calories=0, protein_g=0, carbs_g=0, fat_g=0, fiber_g=0, sugar_g=0, sodium_mg=0,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    # Logging a meal may complete a previously-incomplete past day → clear its flag.
    await nc.sync_day(db, body.date)
    return _serialize(entry)


_EDITABLE_MEALS = ("breakfast", "lunch", "dinner", "dessert", "snack", "drinks")


class LogUpdate(BaseModel):
    at_time: Optional[str] = None      # "HH:MM", local — corrects a mis-timed entry
    name: Optional[str] = None
    brand: Optional[str] = None        # "" clears it (0070, note 60)
    description: Optional[str] = None
    portion_quantity: Optional[float] = None
    portion_unit: Optional[str] = None
    calories: Optional[float] = None
    protein_g: Optional[float] = None
    carbs_g: Optional[float] = None
    fat_g: Optional[float] = None
    fiber_g: Optional[float] = None
    sugar_g: Optional[float] = None
    sodium_mg: Optional[float] = None
    ai_notes: Optional[str] = None
    servings: Optional[float] = None   # a My Food entry's servings, edited on its card
    restaurant: Optional[bool] = None  # note 84; None leaves it as it is
    # The edit sheet always SHOWED the meal picker but never sent it, so re-filing
    # a dinner logged as dessert silently did nothing (note 59).
    meal_type: Optional[str] = None


@router.put("/logs/{log_id}")
async def update_log(
    log_id: int,
    body: LogUpdate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    result = await db.execute(select(FoodLog).where(FoodLog.id == log_id))
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Log entry not found")
    for field in ("portion_quantity", "portion_unit", "calories", "protein_g",
                  "carbs_g", "fat_g", "fiber_g", "sugar_g", "sodium_mg"):
        val = getattr(body, field)
        if val is not None:
            setattr(entry, field, val)
    # Hand-editing the calorie total retires the parser's self-assessment of it.
    # confidence becomes 'high' because the number is now asserted rather than
    # estimated, and omitted_kcal goes back to NULL — "not assessed" — because the
    # parser's account of what IT left out no longer describes a number the user
    # typed. Leaving a stale "low" on a figure you entered yourself would be a
    # worse lie than showing nothing.
    if body.calories is not None:
        entry.confidence = "high"
        entry.omitted_kcal = None
    if body.name is not None:
        entry.name = body.name
    if body.brand is not None:
        entry.brand = _clean_brand(body.brand)
    if body.description is not None:
        entry.description = body.description
    if body.ai_notes is not None:
        entry.llm_notes = body.ai_notes
    if body.servings is not None:
        entry.servings = body.servings
    if body.restaurant is not None:
        entry.restaurant = body.restaurant
    if body.meal_type is not None:
        if body.meal_type not in _EDITABLE_MEALS:
            raise HTTPException(status_code=422, detail=f"Unknown meal type: {body.meal_type}")
        entry.meal_type = body.meal_type
    if body.at_time:
        # Keep the entry on its existing DAY — this edits the clock time only, so a
        # mistyped time can never silently move a meal to another date.
        entry.timestamp = _meal_timestamp(food_day(entry.timestamp), body.at_time)
        entry.time_estimated = False
    await db.commit()
    await db.refresh(entry)
    # A meal-type change can complete a past day (dinner re-filed from dessert) or
    # re-open one — the same reconcile the delete path already runs.
    if body.meal_type is not None:
        await nc.sync_day(db, food_day(entry.timestamp))
        await db.refresh(entry)
    return _serialize(entry)


@router.delete("/logs/{log_id}")
async def delete_log(
    log_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    result = await db.execute(select(FoodLog).where(FoodLog.id == log_id))
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Log entry not found")
    day = food_day(entry.timestamp)
    # A drink logged through the alcohol logger is ONE drink stored as a pair: its SDU
    # in health_metrics and this calories row. Deleting only this row (✕ in Today) left
    # the SDU behind — the half-delete the alcohol sheet's own delete already avoids.
    # Found 2026-09-14 (note 34), when My Foods drinks started landing here too.
    if entry.source_kind == "alcohol" and entry.source_id:
        from app.services import intake
        if await intake.delete_entry(db, "alcohol", entry.source_id):   # also drops this row
            await nc.sync_day(db, day)
            return {"ok": True}
    # The same for a caffeine pair (2026-11-10): ✕ on the drink also removes its dose.
    if entry.source_kind == "caffeine" and entry.source_id:
        from app.services import caffeine as caf
        await caf.delete_instance(db, entry.source_id)
    await db.delete(entry)
    await db.commit()
    await nc.sync_day(db, day)           # deleting a meal may re-open a past day
    return {"ok": True}


# ── Whole-day nutrition skip + completeness ───────────────────────────────────

class DaySkip(BaseModel):
    date: date


@router.post("/skip-day", status_code=201)
async def skip_day(
    body: DaySkip,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Acknowledge a whole day as intentionally un-logged → excluded, stops nagging.

    Distinct from per-meal skip (a known zero that keeps the day in the analysis).
    Undo via DELETE /skip-day, which re-runs completeness (auto-flags again if the
    day is genuinely incomplete).
    """
    await nc.skip_day(db, body.date)
    return {"ok": True, "date": body.date.isoformat(), "skipped": True}


@router.delete("/skip-day")
async def unskip_day(
    date: date = Query(...),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    from app.models import DayFlag
    row = (await db.execute(
        select(DayFlag).where(DayFlag.date == date).where(DayFlag.reason == "incomplete_data")
    )).scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.commit()
    await nc.sync_day(db, date)          # re-auto-flag if still incomplete
    return {"ok": True}


@router.get("/nudges")
async def get_nudges(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Actionable suggestions for the top of Today. Fail-safe: an empty list rather
    than an error, since this is decoration on a page that must always render."""
    from app.services.nudges import build_nudges
    try:
        return await build_nudges(db)
    except Exception as exc:                     # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("nudges failed: %s", exc)
        return []


@router.get("/incomplete")
async def list_incomplete(
    days: int = Query(default=7, ge=1, le=60),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Past days still auto-flagged incomplete (drives the 'log yesterday's meals'
    reminder). Excludes days already acknowledged via skip-day."""
    days_list = await nc.incomplete_days(db, days_back=days)
    return {"incomplete": [d.isoformat() for d in days_list]}


# ── Query endpoints ───────────────────────────────────────────────────────────

@router.get("/logs")
async def get_logs_for_date(
    date: date = Query(default_factory=date.today),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Return all food log entries for a given FOOD day (04:00 → 04:00), ordered by meal
    type then timestamp."""
    result = await db.execute(
        select(FoodLog)
        .where(FoodLog.timestamp >= food_day_start(date))
        .where(FoodLog.timestamp < food_day_end(date))
        # id last: a combo's parts share a timestamp, and the base is written first —
        # without the tiebreak a topping could list above its bagel.
        .order_by(FoodLog.meal_type, FoodLog.timestamp, FoodLog.id)
    )
    rows = result.scalars().all()
    return [_serialize(r) for r in rows]


@router.post("/logs/{log_id}/save")
async def toggle_save(
    log_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Toggle the is_saved flag on a food log entry."""
    result = await db.execute(select(FoodLog).where(FoodLog.id == log_id))
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Log entry not found")
    entry.is_saved = not entry.is_saved
    await db.commit()
    return {"id": entry.id, "is_saved": entry.is_saved}


# A phone photo is a few MB; the client already downscales, so anything past this is
# not a label picture. Well under the API's 5 MB per-image ceiling once base64 inflates
# it ~4/3.
MAX_LABEL_BYTES = 6 * 1024 * 1024
_LABEL_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


@router.post("/parse-label")
async def parse_label(
    file: UploadFile = File(...),
    _: str = Depends(get_current_user),
):
    """Read a Nutrition Facts photo into per-serving macros. Does NOT save.

    Returns values exactly as printed on the label, for one serving. The client sets
    portion quantity to 1 serving so the existing portion-scaling handles "I ate two".
    """
    ctype = (file.content_type or "").lower()
    if ctype not in _LABEL_TYPES:
        # Some phones send octet-stream; fall back to the extension.
        if not (file.filename or "").lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            raise HTTPException(status_code=422, detail="Not an image")
        ctype = "image/jpeg"

    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Empty file")
    if len(data) > MAX_LABEL_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Image too large ({len(data)//1024} KB) — retake or crop it")

    from app.services.ai_summaries import parse_nutrition_label
    result = await parse_nutrition_label(data, ctype)
    if result is None:
        raise HTTPException(status_code=503,
                            detail="AI parsing unavailable — check API key / AI setting")
    if result.get("error"):
        raise HTTPException(
            status_code=422,
            detail="That doesn't look like a nutrition label — try a straight-on photo "
                   "of the Nutrition Facts panel.")
    return result


# A meal's rank should reflect how likely you are to want it *again*, which is
# neither pure frequency (something eaten 60x last year outranks this week's staple)
# nor pure recency (a one-off dinner outranks the bar you eat five times a week).
# Summing a half-life decay over every instance gives both: each logging contributes
# 0.5^(age/HALF_LIFE), so habits float and abandoned foods sink on their own.
MEAL_SCORE_HALF_LIFE_DAYS = 90.0


@router.get("/last-meal")
async def get_last_meal(
    meal_type: str = Query(...),
    before: Optional[date] = Query(default=None, description="food day to look back from (default: today)"),
    days: int = Query(default=14, ge=1, le=60),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """The most recent earlier FOOD day's entries for one meal — "same as last lunch"
    (backlog #22, 2026-10-21). Looks back up to `days`. Skipped meals and rows written by
    the alcohol / caffeine loggers (source_kind set) are left out: those are one half of
    an intake pair, and re-adding them as food would drop the SDU / mg record."""
    before = before or food_today()
    rows = (await db.execute(
        select(FoodLog)
        .where(func.coalesce(FoodLog.meal_type, "snack") == meal_type)
        .where(FoodLog.timestamp >= food_day_start(before - timedelta(days=days)))
        .where(FoodLog.timestamp < food_day_start(before))
        .where((FoodLog.skipped == False) | (FoodLog.skipped.is_(None)))  # noqa: E712
        .where(FoodLog.source_kind.is_(None))
        .order_by(FoodLog.timestamp, FoodLog.id)
    )).scalars().all()
    if not rows:
        return {"date": None, "items": []}
    last = max(food_day(r.timestamp) for r in rows)
    return {"date": last.isoformat(),
            "items": [_serialize(r) for r in rows if food_day(r.timestamp) == last]}


@router.get("/recent")
async def get_recent_meals(
    limit: int = Query(default=60, ge=1, le=300),
    q: Optional[str] = Query(default=None, description="substring filter on name/description/brand"),
    meal_type: Optional[str] = Query(default=None),
    sort: str = Query(default="score", pattern="^(score|recent|frequent)$"),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Meal picker source: every distinct item ever logged, ranked by re-use likelihood.

    `meal_type` (2026-10-21): only entries logged AT that meal count — uses, recency and
    the representative portion are all per meal. (It used to filter on whichever meal an
    item was last logged at, so a lunch staple last eaten as a snack vanished from lunch.)
    `sort`: score (recency-decayed frequency, the default), recent (newest first — the
    Log Food picker's default) or frequent (most uses, newest breaking ties).

    Replaces a scan of the last 200 rows, which on this logging rate reached back
    barely five weeks — so anything older was invisible unless explicitly starred,
    and only 15 of 685 entries ever were. History is now unbounded; ranking, not
    truncation, decides what surfaces first.

    Grouping is by NAME (the canonical thing the parser extracted) rather than the
    free-text description. Keying on description treated "50g gouda cheese",
    "29g of gouda cheese" and "37 g of gouda cheese" as three separate meals — one
    item had seven such variants — which is exactly the clutter that made the list
    hard to navigate.
    """
    rq = (select(FoodLog)
          .where((FoodLog.skipped == False) | (FoodLog.skipped.is_(None)))  # noqa: E712
          .order_by(FoodLog.timestamp.desc()))
    if meal_type:
        rq = rq.where(func.coalesce(FoodLog.meal_type, "snack") == meal_type)
    rows = (await db.execute(rq)).scalars().all()

    now = datetime.utcnow()
    groups: dict[str, dict] = {}
    for r in rows:
        key = ((r.name or "").strip().lower()
               or (r.description or "").strip().lower())
        if key and r.brand:
            # Kirkland cream cheese and generic cream cheese are different foods.
            key = f"{r.brand.strip().lower()}|{key}"
        if not key:
            continue
        age_days = max(0.0, (now - r.timestamp).total_seconds() / 86400.0)
        g = groups.get(key)
        if g is None:
            # rows are newest-first, so the first one seen is the representative
            g = groups[key] = {"row": r, "uses": 0, "score": 0.0, "saved": False,
                               "assessed": None}
        g["uses"] += 1
        g["score"] += 0.5 ** (age_days / MEAL_SCORE_HALF_LIFE_DAYS)
        g["saved"] = g["saved"] or bool(r.is_saved)
        # Carry forward the NEWEST row that actually carries a parser assessment,
        # which is usually not the representative row. Picking a saved meal re-uses
        # a previous estimate verbatim, so it inherits that estimate's confidence and
        # omission — nothing new was measured, and promoting a repeat to "high" would
        # invent certainty. Without this, one assessed parse of an item is stranded on
        # a single row while all 87 future re-logs read NULL forever.
        if g["assessed"] is None and r.confidence is not None:
            g["assessed"] = r

    items = list(groups.values())
    if q:
        needle = q.strip().lower()
        items = [g for g in items
                 if needle in ((g["row"].name or "") + " " + (g["row"].description or "")
                               + " " + (g["row"].brand or "")).lower()]

    # Ranked by score alone. Starred meals used to outrank everything, but the ★ UI
    # was retired 2026-09-13 (My Foods replaces it) and a pin nobody can remove is
    # just a permanent wall at the top. is_saved is kept on the rows and still
    # feeds My Foods candidate seeding ("starred").
    if sort == "recent":
        items.sort(key=lambda g: g["row"].timestamp, reverse=True)
    elif sort == "frequent":
        items.sort(key=lambda g: (g["uses"], g["row"].timestamp), reverse=True)
    else:
        items.sort(key=lambda g: -g["score"])

    # Which My Food each recent entry IS, so the picker shows the library version once
    # instead of the same food twice (Phase 2). A row linked by apply-to-past says so
    # itself; otherwise its name / text is matched against every key a food answers to.
    from app.services.food_tags import item_key
    from app.services.my_foods import foods_by_key
    my_foods = await foods_by_key(db)
    out = []
    for g in items[:limit]:
        d = _serialize(g["row"])
        if d.get("my_food_id") is None:
            mf = (my_foods.get(item_key(g["row"].name, None, g["row"].brand))
                  or my_foods.get(item_key(None, g["row"].description, g["row"].brand)))
            d["my_food_id"] = mf.id if mf else None
        d["is_saved"] = g["saved"]      # saved if ANY instance was starred
        d["uses"] = g["uses"]
        # The representative row is chosen by recency; the assessment is chosen by
        # whether it exists at all. Those are different rows more often than not.
        a = g["assessed"]
        if a is not None and d.get("confidence") is None:
            d["confidence"] = a.confidence
            d["omitted_kcal"] = a.omitted_kcal
        out.append(d)
    return out


@router.get("/brands")
async def get_brands(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Brands already used, most-used first — the brand field's suggestion list."""
    from app.services.my_foods import known_brands
    return await known_brands(db)


@router.get("/daily")
async def get_daily_totals(
    start: date = Query(default_factory=lambda: local_today() - timedelta(days=30)),
    end: date = Query(default_factory=date.today),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Return per-day nutrition totals for charting."""
    result = await db.execute(
        select(
            food_date_sql(FoodLog.timestamp).label("date"),
            func.sum(FoodLog.calories).label("calories"),
            func.sum(FoodLog.protein_g).label("protein_g"),
            func.sum(FoodLog.carbs_g).label("carbs_g"),
            func.sum(FoodLog.fat_g).label("fat_g"),
            func.sum(FoodLog.fiber_g).label("fiber_g"),
            func.sum(FoodLog.sugar_g).label("sugar_g"),
            func.sum(FoodLog.sodium_mg).label("sodium_mg"),
            # Kept OUT of `calories` deliberately — see migration 0059. A day reads
            # "2,430 kcal, ~180 not counted", never one number pretending to be both.
            func.sum(FoodLog.omitted_kcal).label("omitted_kcal"),
            # SUM alone cannot tell "nothing was left out" from "nothing was looked
            # at": both come back 0/NULL. These two counts are what let the UI say
            # "~180 not counted" only when it actually knows, and stay quiet
            # otherwise. Rows logged before 2026-09-04 are permanently unassessed.
            #
            # Only rows the parser COULD have assessed count (2026-09-13): drink-logger
            # rows are never parsed and a My Food carries checked numbers, so counting
            # them made the Today line read "2 of 3 not assessed" for a coffee and a
            # library bagel — noise, not a gap.
            func.sum(case((and_(FoodLog.source_kind.is_(None), FoodLog.my_food_id.is_(None)), 1),
                          else_=0)).label("n_items"),
            func.sum(case((and_(FoodLog.source_kind.is_(None), FoodLog.my_food_id.is_(None),
                                FoodLog.confidence.isnot(None)), 1), else_=0)).label("n_assessed"),
        )
        .where(FoodLog.timestamp >= food_day_start(start))
        .where(FoodLog.timestamp < food_day_end(end))
        .group_by(food_date_sql(FoodLog.timestamp))
        .order_by(food_date_sql(FoodLog.timestamp))
    )
    rows = result.all()

    def _r(v, dec=0):
        if v is None:
            return None
        return round(v, dec) if dec else round(v)

    return [
        {
            "date": str(r.date),
            "calories": _r(r.calories),
            "protein_g": _r(r.protein_g, 1),
            "carbs_g": _r(r.carbs_g, 1),
            "fat_g": _r(r.fat_g, 1),
            "fiber_g": _r(r.fiber_g, 1),
            "sugar_g": _r(r.sugar_g, 1),
            "sodium_mg": _r(r.sodium_mg),
            "omitted_kcal": _r(r.omitted_kcal),
            "n_items": r.n_items,
            "n_assessed": r.n_assessed,
        }
        for r in rows
    ]


@router.get("/export")
async def export_food_logs(
    start: Optional[date] = Query(default=None, description="Inclusive start date (YYYY-MM-DD). Defaults to all history."),
    end: Optional[date] = Query(default=None, description="Inclusive end date (YYYY-MM-DD). Defaults to today."),
    format: str = Query(default="text", description="'text' (default) or 'json'"),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """
    Export all AI food-log entries for evaluation.

    Text format is optimised for pasting directly into an LLM prompt.
    Includes the system prompt used, the user's original input, and the
    full structured output (macros + assumptions) for each entry.
    """
    from app.services.ai_summaries import FOOD_PARSE_SYSTEM

    end_date   = end   or local_today()
    start_date = start or date(2000, 1, 1)

    result = await db.execute(
        select(FoodLog)
        .where(FoodLog.timestamp >= food_day_start(start_date))
        .where(FoodLog.timestamp < food_day_end(end_date))
        .order_by(FoodLog.timestamp)
    )
    rows = result.scalars().all()

    if format == "json":
        return [
            {
                "id":          r.id,
                "date":        food_day(r.timestamp).isoformat(),
                "meal_type":   r.meal_type,
                "input":       r.description,
                "output": {
                    "name":             r.name,
                    "brand":            r.brand,
                    "portion_quantity": r.portion_quantity,
                    "portion_unit":     r.portion_unit,
                    "calories":         r.calories,
                    "protein_g":        r.protein_g,
                    "carbs_g":          r.carbs_g,
                    "fat_g":            r.fat_g,
                    "fiber_g":          r.fiber_g,
                    "sugar_g":          r.sugar_g,
                    "sodium_mg":        r.sodium_mg,
                    "notes":            r.llm_notes,
                },
            }
            for r in rows
        ]

    # ── Text format ────────────────────────────────────────────────────────────
    def _f(v, dec=0, suffix=""):
        if v is None:
            return "—"
        return f"{round(v, dec) if dec else round(v)}{suffix}"

    lines = [
        "FOOD LOG AI EVALUATION EXPORT",
        f"Generated : {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        f"Date range: {start_date} → {end_date}",
        f"Entries   : {len(rows)}",
        "",
        "═" * 72,
        "SYSTEM PROMPT (sent with every request)",
        "═" * 72,
        FOOD_PARSE_SYSTEM,
        "",
        "═" * 72,
        "ENTRIES",
        "═" * 72,
    ]

    for i, r in enumerate(rows, 1):
        lines += [
            "",
            f"[{i}] {food_day(r.timestamp)}  {(r.meal_type or 'snack').upper()}",
            f"INPUT : {r.description}",
            "OUTPUT:",
            f"  Name    : {r.name or '—'}" + (f" ({r.brand})" if r.brand else ""),
            f"  Portion : {_f(r.portion_quantity)} {r.portion_unit or ''}".rstrip(),
            f"  Calories: {_f(r.calories)} kcal  |  Protein: {_f(r.protein_g, 1)}g  |  Carbs: {_f(r.carbs_g, 1)}g  |  Fat: {_f(r.fat_g, 1)}g",
            f"  Fiber   : {_f(r.fiber_g, 1)}g  |  Sugar: {_f(r.sugar_g, 1)}g  |  Sodium: {_f(r.sodium_mg)} mg",
            f"  Notes   : {r.llm_notes or '—'}",
            "─" * 72,
        ]

    return PlainTextResponse("\n".join(lines))


@router.get("/daily-breakdown")
async def get_daily_breakdown(
    start: date = Query(default_factory=lambda: local_today() - timedelta(days=30)),
    end: date = Query(default_factory=date.today),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Per-day calories, protein and fiber broken down by meal type
    (breakfast/lunch/dinner/dessert/drinks/other — `other` is snacks).

    `complete` is False for today (still being logged) and for incomplete_data days,
    so a rolling average can skip them — a partial day reads as a low day (note 45)."""
    NAMED = ("breakfast", "lunch", "dinner", "dessert", "drinks")

    def _cal(meal_type):
        return func.sum(
            case((FoodLog.meal_type == meal_type, FoodLog.calories), else_=0)
        )

    def _pro(meal_type):
        return func.sum(
            case((FoodLog.meal_type == meal_type, FoodLog.protein_g), else_=0)
        )

    def _fib(meal_type):
        return func.sum(
            case((FoodLog.meal_type == meal_type, FoodLog.fiber_g), else_=0)
        )

    other_fib = func.sum(
        case((FoodLog.meal_type.not_in(NAMED), FoodLog.fiber_g), else_=0)
    )
    other_cal = func.sum(
        case((FoodLog.meal_type.not_in(NAMED), FoodLog.calories), else_=0)
    )
    other_pro = func.sum(
        case((FoodLog.meal_type.not_in(NAMED), FoodLog.protein_g), else_=0)
    )

    result = await db.execute(
        select(
            food_date_sql(FoodLog.timestamp).label("date"),
            _cal("breakfast").label("breakfast_cal"),
            _cal("lunch").label("lunch_cal"),
            _cal("dinner").label("dinner_cal"),
            _cal("dessert").label("dessert_cal"),
            _cal("drinks").label("drinks_cal"),
            other_cal.label("other_cal"),
            _pro("breakfast").label("breakfast_pro"),
            _pro("lunch").label("lunch_pro"),
            _pro("dinner").label("dinner_pro"),
            _pro("dessert").label("dessert_pro"),
            _pro("drinks").label("drinks_pro"),
            other_pro.label("other_pro"),
            _fib("breakfast").label("breakfast_fib"),
            _fib("lunch").label("lunch_fib"),
            _fib("dinner").label("dinner_fib"),
            _fib("dessert").label("dessert_fib"),
            _fib("drinks").label("drinks_fib"),
            other_fib.label("other_fib"),
        )
        .where(FoodLog.timestamp >= food_day_start(start))
        .where(FoodLog.timestamp < food_day_end(end))
        .group_by(food_date_sql(FoodLog.timestamp))
        .order_by(food_date_sql(FoodLog.timestamp))
    )
    rows = result.all()

    from app.services.day_flags import nutrition_excluded_dates
    _today = food_today().isoformat()
    _partial = {d.isoformat() for d in await nutrition_excluded_dates(db, start, end)}

    def _r(v, dec=0):
        if v is None:
            return None
        return round(v, dec) if dec else round(v)

    return [
        {
            "date":          str(r.date),
            "breakfast_cal": _r(r.breakfast_cal),
            "lunch_cal":     _r(r.lunch_cal),
            "dinner_cal":    _r(r.dinner_cal),
            "dessert_cal":   _r(r.dessert_cal),
            "drinks_cal":    _r(r.drinks_cal),
            "other_cal":     _r(r.other_cal),
            "breakfast_pro": _r(r.breakfast_pro, 1),
            "lunch_pro":     _r(r.lunch_pro, 1),
            "dinner_pro":    _r(r.dinner_pro, 1),
            "dessert_pro":   _r(r.dessert_pro, 1),
            "drinks_pro":    _r(r.drinks_pro, 1),
            "other_pro":     _r(r.other_pro, 1),
            "breakfast_fib": _r(r.breakfast_fib, 1),
            "lunch_fib":     _r(r.lunch_fib, 1),
            "dinner_fib":    _r(r.dinner_fib, 1),
            "dessert_fib":   _r(r.dessert_fib, 1),
            "drinks_fib":    _r(r.drinks_fib, 1),
            "other_fib":     _r(r.other_fib, 1),
            "complete":      str(r.date) < _today and str(r.date) not in _partial,
        }
        for r in rows
    ]
