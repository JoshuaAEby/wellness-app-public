"""Alcohol + water logging API. See services/intake for the storage rationale."""
from datetime import date as Date, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.services import intake

alcohol_router = APIRouter(prefix="/api/alcohol", tags=["alcohol"])
water_router = APIRouter(prefix="/api/water", tags=["water"])


# ── Alcohol ───────────────────────────────────────────────────────────────────

@alcohol_router.get("/presets")
async def get_alcohol_presets(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)):
    return {"presets": await intake.effective_alcohol_presets(db),
            "defaults": [{**p, "sdu": intake.preset_sdu(p)} for p in intake.ALCOHOL_PRESETS]}


class AlcoholPresetsIn(BaseModel):
    presets: list | None = None      # None resets to defaults


@alcohol_router.put("/presets")
async def put_alcohol_presets(body: AlcoholPresetsIn, db: AsyncSession = Depends(get_db),
                              _: str = Depends(get_current_user)):
    if body.presets is not None:
        for p in body.presets:
            if not all(k in p for k in ("id", "label")):
                raise HTTPException(status_code=422, detail="each preset needs id, label")
            if p.get("sdu") is None and not all(k in p for k in ("oz", "abv")):
                raise HTTPException(status_code=422, detail="preset needs oz+abv or an explicit sdu")
    return {"presets": await intake.save_alcohol_presets(db, body.presets)}


class AlcoholIn(BaseModel):
    date: Date
    at_time: str | None = None       # "HH:MM" local; omitted = now (today) / noon (past)
    kind: str | None = None          # preset id
    quantity: float = 1.0            # multiples of the preset
    oz: float | None = None          # custom volume …
    abv: float | None = None         # … and strength
    sdu: float | None = None         # or an explicit SDU override
    kcal: float | None = None        # overrides the preset's calories
    label: str | None = None         # name for the linked nutrition entry
    category: str | None = None      # beer/wine/liquor/… — only used to ESTIMATE kcal
                                     # for a custom oz+abv drink (notes 26/27)
    my_food_id: int | None = None    # a named drink from My Foods (0067, note 34) —
    servings: float = 1.0            # supplies oz, ABV, calories, macros and the label
    brand: str | None = None         # optional; else the preset's (note 98)


@alcohol_router.get("/categories")
async def get_alcohol_categories(_: str = Depends(get_current_user)):
    """Drink categories for the custom-entry picker, with their carb densities.

    Served rather than hardcoded in the UI so the densities have exactly one home —
    the same reason the caffeine and protein constants were consolidated.
    """
    # The constants ride along so the client can preview an estimate per keystroke
    # WITHOUT a request per keystroke, and without a second copy of the densities. One
    # home (this module), fetched once.
    return {"categories": intake.ALCOHOL_CATEGORIES,
            "default": intake.DEFAULT_CATEGORY,
            "ethanol_g_per_oz_pct": intake.ETHANOL_G_PER_OZ_PCT,
            "kcal_per_g_ethanol": intake.KCAL_PER_G_ETHANOL,
            "ethanol_oz_per_sdu": intake.ETHANOL_OZ_PER_SDU}


@alcohol_router.post("/estimate")
async def estimate_alcohol(oz: float, abv: float, category: str | None = None,
                           _: str = Depends(get_current_user)):
    """Preview SDU + calories for a size/strength/category, without logging anything.

    Lets the form show the numbers as they are typed, so the user sees what a 16 oz 7%
    pour actually costs BEFORE committing it.
    """
    est = intake.estimate_alcohol_nutrition(oz, abv, category)
    return {**est, "sdu": intake.sdu_from(oz, abv)}


@alcohol_router.post("/logs", status_code=201)
async def add_alcohol(body: AlcoholIn, db: AsyncSession = Depends(get_db),
                      _: str = Depends(get_current_user)):
    """Log one drink. SDU is resolved in priority order: explicit > oz+abv > preset.

    A My Foods drink (my_food_id) is turned into oz + ABV + kcal here, so it takes the
    oz+abv path below and writes the SDU and the linked nutrition row in one go — the
    same pair the presets write. This is also the path the Log Food modal uses for an
    alcoholic My Food, so a drink logged from either screen is counted exactly once.
    """
    food = None
    food_macros = None
    if body.my_food_id is not None:
        from app.models import MyFood
        food = (await db.execute(select(MyFood).where(MyFood.id == body.my_food_id))).scalar_one_or_none()
        if food is None or not food.abv or food.abv <= 0 \
                or (food.serving_unit or "").strip().lower() != "oz":
            raise HTTPException(status_code=422,
                                detail="that My Food is not a drink with an ABV and an oz serving")
        sv = float(body.servings or 1.0)
        if sv <= 0:
            raise HTTPException(status_code=422, detail="servings must be positive")
        food_macros = {k: round(getattr(food, k) * sv, 2)
                       for k in ("protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g", "sodium_mg")
                       if getattr(food, k) is not None} or None
        body = body.model_copy(update={
            "oz": float(food.serving_qty or 1.0) * sv, "abv": float(food.abv), "sdu": None,
            "kind": None, "quantity": 1.0, "category": food.drink_kind,
            "kcal": body.kcal if body.kcal is not None else round(float(food.calories) * sv),
            "label": body.label or food.name,
        })
    if body.sdu is not None:
        sdu = float(body.sdu)
    elif body.oz is not None and body.abv is not None:
        sdu = intake.sdu_from(body.oz, body.abv)
    elif body.kind:
        presets = await intake.effective_alcohol_presets(db)
        preset = next((p for p in presets if p["id"] == body.kind), None)
        if preset is None:
            raise HTTPException(status_code=422, detail=f"unknown preset {body.kind}")
        sdu = preset["sdu"]
    else:
        raise HTTPException(status_code=422, detail="supply kind, oz+abv, or sdu")
    # Calories + label ride along from the preset unless overridden, so one tap logs
    # both the SDU and the Nutrition "drinks" entry.
    preset = None
    if body.kind:
        preset = next((p for p in await intake.effective_alcohol_presets(db)
                       if p["id"] == body.kind), None)
    qty = float(body.quantity or 1.0)

    # A CUSTOM drink now gets estimated calories instead of none (notes 26/27). Before
    # this, logging by oz+abv produced an SDU but a nutrition entry with NULL calories —
    # which reads as "unmeasured" rather than as the ~150 kcal it plainly was, and
    # quietly understated every day containing a hand-entered drink.
    #
    # Priority is unchanged where it already existed: an explicit kcal still wins, and a
    # preset still supplies its own hand-tuned number. The estimate only fills the gap.
    estimated = None
    if body.kcal is None and preset is None and body.oz is not None and body.abv is not None:
        estimated = intake.estimate_alcohol_nutrition(body.oz, body.abv, body.category)

    kcal = body.kcal if body.kcal is not None else (
        (preset or {}).get("kcal") if preset else (estimated or {}).get("kcal"))
    kcal = float(kcal) * qty if kcal else None
    label = body.label or (preset or {}).get("label") or (
        intake.CATEGORY_BY_ID.get(estimated["category"], {}).get("label")
        if estimated else None) or "Alcoholic drink"

    sdu = round(sdu * qty, 3)
    if sdu <= 0:
        raise HTTPException(status_code=422, detail="sdu must be positive")
    row = await intake.log_alcohol(db, body.date, sdu, body.at_time)
    await intake.sync_nutrition_entry(db, source_kind="alcohol", source_id=row.id,
                                      label=label, kcal=kcal, ts_utc=row.timestamp,
                                      macros=food_macros if food is not None else intake.scale_macros(
                                          (preset or {}).get("macros")
                                          or (estimated or {}).get("macros"), qty),
                                      my_food_id=food.id if food is not None else None,
                                      servings=float(body.servings or 1.0) if food is not None else None,
                                      drink_kind=(preset or {}).get("id") or body.category,
                                      brand=(body.brand or (preset or {}).get("brand") or "").strip()[:40] or None)
    return intake.serialize(row)


class AlcoholPatch(BaseModel):
    at_time: str | None = None
    oz: float | None = None
    abv: float | None = None
    sdu: float | None = None
    kcal: float | None = None
    label: str | None = None


@alcohol_router.patch("/logs/{row_id}")
async def patch_alcohol(row_id: int, body: AlcoholPatch, db: AsyncSession = Depends(get_db),
                        _: str = Depends(get_current_user)):
    """Correct a logged drink: its time, its strength, or its calories."""
    sdu = body.sdu
    if sdu is None and body.oz is not None and body.abv is not None:
        sdu = intake.sdu_from(body.oz, body.abv)
    row = await intake.update_entry(db, "alcohol", row_id, value=sdu, at_time=body.at_time)
    if row is None:
        raise HTTPException(status_code=404, detail="entry not found")
    # Keep the linked nutrition row in step — including its timestamp, or an edited
    # drink would sit at one time and its calories at another.
    if body.kcal is not None or body.label is not None or body.at_time is not None:
        from app.models import FoodLog
        from sqlalchemy import select as _select
        linked = (await db.execute(
            _select(FoodLog).where(FoodLog.source_kind == "alcohol")
            .where(FoodLog.source_id == row_id))).scalar_one_or_none()
        await intake.sync_nutrition_entry(
            db, source_kind="alcohol", source_id=row_id,
            label=body.label or (linked.name if linked else "Alcoholic drink"),
            kcal=body.kcal if body.kcal is not None else (linked.calories if linked else None),
            ts_utc=row.timestamp,
            macros=None if linked is None else {
                k: getattr(linked, k) for k in
                ("protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g")
                if getattr(linked, k) is not None})
    return intake.serialize(row)


@alcohol_router.get("/day")
async def alcohol_day(date: Date = Query(default_factory=Date.today),
                      db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)):
    """Day's drinks plus the SDU still on board at that night's bedtime.

    The residual lives here rather than in intake.day_entries because that helper
    is shared with water and naps, neither of which has a pharmacokinetic curve.
    """
    from app.models import HealthMetric
    from app.services.alcohol import onboard_at_bed
    from app.services.bedtime import bedtime_for_day
    from app.foodday import on_food_days_utc

    payload = await intake.day_entries(db, "alcohol", date)

    rows = (await db.execute(
        select(HealthMetric)
        .where(HealthMetric.metric_type == "alcohol")
        .where(on_food_days_utc(HealthMetric.timestamp, date))      # same day as the card
    )).scalars().all()
    bed, bed_actual = await bedtime_for_day(db, date)
    payload["onboard_at_bed"] = onboard_at_bed(rows, bed) if bed else None
    payload["bedtime_projected"] = (not bed_actual) if bed else None
    return payload


@alcohol_router.delete("/logs/{row_id}", status_code=204)
async def delete_alcohol(row_id: int, db: AsyncSession = Depends(get_db),
                         _: str = Depends(get_current_user)):
    if not await intake.delete_entry(db, "alcohol", row_id):
        raise HTTPException(status_code=404, detail="entry not found")


# ── Water ─────────────────────────────────────────────────────────────────────

@water_router.get("/presets")
async def get_water_presets(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)):
    return {"presets": await intake.effective_water_presets(db),
            "defaults": intake.WATER_PRESETS}


class WaterPresetsIn(BaseModel):
    presets: list | None = None


@water_router.put("/presets")
async def put_water_presets(body: WaterPresetsIn, db: AsyncSession = Depends(get_db),
                            _: str = Depends(get_current_user)):
    if body.presets is not None:
        for p in body.presets:
            if not all(k in p for k in ("id", "label", "oz")):
                raise HTTPException(status_code=422, detail="each preset needs id, label, oz")
    return {"presets": await intake.save_water_presets(db, body.presets)}


class WaterPatch(BaseModel):
    at_time: str | None = None
    oz: float | None = None


@water_router.patch("/logs/{row_id}")
async def patch_water(row_id: int, body: WaterPatch, db: AsyncSession = Depends(get_db),
                      _: str = Depends(get_current_user)):
    row = await intake.update_entry(db, "water", row_id, value=body.oz, at_time=body.at_time)
    if row is None:
        raise HTTPException(status_code=404, detail="entry not found")
    return intake.serialize(row)


class WaterIn(BaseModel):
    date: Date
    at_time: str | None = None
    kind: str | None = None          # preset id
    oz: float | None = None          # explicit size, overrides the preset


@water_router.post("/logs", status_code=201)
async def add_water(body: WaterIn, db: AsyncSession = Depends(get_db),
                    _: str = Depends(get_current_user)):
    """Log water from a preset, an explicit size, or the default Refill."""
    if body.oz is not None:
        oz = float(body.oz)
    elif body.kind:
        preset = next((p for p in await intake.effective_water_presets(db)
                       if p["id"] == body.kind), None)
        if preset is None:
            raise HTTPException(status_code=422, detail=f"unknown preset {body.kind}")
        oz = float(preset["oz"])
    else:
        oz = float(await intake.bottle_oz(db))
    if oz <= 0:
        raise HTTPException(status_code=422, detail="oz must be positive")
    row = await intake.log_water(db, body.date, oz, body.at_time)
    return intake.serialize(row)


@water_router.get("/day")
async def water_day(date: Date = Query(default_factory=Date.today),
                    db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)):
    data = await intake.day_entries(db, "water", date)
    data["bottle_oz"] = await intake.bottle_oz(db)
    data["presets"] = await intake.effective_water_presets(db)
    # Total fluids from ALL drinks vs the day's training-adjusted target (note 78).
    from app.services.fluids import day_status
    data["fluids"] = await day_status(db, date)
    return data


@water_router.get("/fluids-range")
async def fluids_range(start: Date = Query(default_factory=lambda: Date.today() - timedelta(days=30)),
                       end: Date = Query(default_factory=Date.today),
                       db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)):
    """Total fluids per water-logged food day with its target (Exposures FLUIDS, note 80)."""
    from app.services.fluids import fluids_range as _fluids_range
    return await _fluids_range(db, start, end)


@water_router.delete("/logs/{row_id}", status_code=204)
async def delete_water(row_id: int, db: AsyncSession = Depends(get_db),
                       _: str = Depends(get_current_user)):
    if not await intake.delete_entry(db, "water", row_id):
        raise HTTPException(status_code=404, detail="entry not found")


# ── Naps ──────────────────────────────────────────────────────────────────────
#
# Stored like alcohol and water: one health_metrics row per nap, `value` = minutes,
# `timestamp` = when the nap STARTED. Duration has to be entered because Apple keeps
# one sleep session per date and folds daytime sleep into it.

nap_router = APIRouter(prefix="/api/nap", tags=["nap"])


@nap_router.get("/presets")
async def get_nap_presets(_: str = Depends(get_current_user)):
    return {"presets": intake.NAP_PRESETS}


class NapIn(BaseModel):
    date: Date
    at_time: str | None = None       # local "HH:MM" the nap began
    minutes: float | None = None
    kind: str | None = None          # preset id


@nap_router.post("/logs", status_code=201)
async def add_nap(body: NapIn, db: AsyncSession = Depends(get_db),
                  _: str = Depends(get_current_user)):
    minutes = body.minutes
    if minutes is None and body.kind:
        preset = next((p for p in intake.NAP_PRESETS if p["id"] == body.kind), None)
        if preset is None:
            raise HTTPException(status_code=422, detail=f"unknown preset {body.kind}")
        minutes = preset["minutes"]
    if not minutes or float(minutes) <= 0:
        raise HTTPException(status_code=422, detail="minutes must be positive")
    row = await intake.log_nap(db, body.date, float(minutes), body.at_time)
    return intake.serialize(row)


class NapPatch(BaseModel):
    at_time: str | None = None
    minutes: float | None = None


@nap_router.patch("/logs/{row_id}")
async def patch_nap(row_id: int, body: NapPatch, db: AsyncSession = Depends(get_db),
                    _: str = Depends(get_current_user)):
    row = await intake.update_entry(db, "nap", row_id, value=body.minutes, at_time=body.at_time)
    if row is None:
        raise HTTPException(status_code=404, detail="entry not found")
    return intake.serialize(row)


@nap_router.get("/day")
async def nap_day(date: Date = Query(default_factory=Date.today),
                  db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)):
    data = await intake.day_entries(db, "nap", date)
    data["presets"] = intake.NAP_PRESETS
    return data


@nap_router.delete("/logs/{row_id}", status_code=204)
async def delete_nap(row_id: int, db: AsyncSession = Depends(get_db),
                     _: str = Depends(get_current_user)):
    if not await intake.delete_entry(db, "nap", row_id):
        raise HTTPException(status_code=404, detail="entry not found")


# ── Usual intake: one-tap chips under the intake buttons (note 81, 2026-10-26) ──────
#
# The most frequent entry per kind over the last `days`, returned ONLY when it maps to
# something a single tap can reproduce exactly: a current caffeine/alcohol preset, or a
# water amount (a matching water preset supplies the label). A mode that no longer maps
# (deleted preset, a custom one-off) yields nothing rather than a guess. Mode, not
# "most recent": the chip should be your habit, not whatever you happened to log last.

intake_router = APIRouter(prefix="/api/intake", tags=["intake"])


@intake_router.get("/usual")
async def usual_intake(days: int = Query(default=30, ge=7, le=120),
                       db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)):
    from collections import Counter
    from datetime import datetime, timedelta
    from app.models import CaffeineLog, FoodLog, HealthMetric
    from app.services.caffeine import effective_presets as caffeine_presets

    since = datetime.utcnow() - timedelta(days=days)
    out: dict = {"days": days, "caffeine": None, "water": None, "alcohol": None}

    kinds = Counter(k for (k,) in (await db.execute(
        select(CaffeineLog.kind).where(CaffeineLog.timestamp >= since))).all())
    by_id = {p["id"]: p for p in await caffeine_presets(db)}
    for kind, n in kinds.most_common():
        if kind in by_id:
            p = by_id[kind]
            out["caffeine"] = {"kind": kind, "label": p["label"], "emoji": p.get("emoji"),
                               "mg": p["mg"], "times": n}
            break

    sizes = Counter(round(v, 1) for (v,) in (await db.execute(
        select(HealthMetric.value).where(HealthMetric.metric_type == "water")
        .where(HealthMetric.timestamp >= since))).all() if v)
    if sizes:
        oz, n = sizes.most_common(1)[0]
        wp = next((p for p in await intake.effective_water_presets(db)
                   if float(p.get("oz") or 0) == oz), None)
        out["water"] = {"oz": oz, "kind": wp["id"] if wp else None,
                        "label": wp["label"] if wp else f"{oz:g} oz", "times": n}

    # Alcohol rows keep only SDU; the preset survives as the linked drinks row's name.
    names = Counter(nm for (nm,) in (await db.execute(
        select(FoodLog.name).where(FoodLog.source_kind == "alcohol")
        .where(FoodLog.timestamp >= since - timedelta(hours=14)))).all() if nm)
    ap = {p["label"]: p for p in await intake.effective_alcohol_presets(db)}
    for nm, n in names.most_common():
        if nm in ap:
            p = ap[nm]
            out["alcohol"] = {"kind": p["id"], "label": p["label"], "emoji": p.get("emoji"),
                              "sdu": p["sdu"], "times": n}
            break
    return out
