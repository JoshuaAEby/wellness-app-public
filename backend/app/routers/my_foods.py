"""My Foods API (docs/MY_FOODS.md, phase 1). Archive, never delete: logged rows may
link to a food, and history must stay explainable."""
import json
from collections import Counter, defaultdict
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.foodday import food_day
from app.models import FoodLog, MyFood, MyFoodBackfill, MyFoodDismissal
from app.services.food_tags import item_key
from app.services.my_foods import (
    MACROS, food_aliases, food_components, food_keys, food_units, past_matches, scaled,
    seed_candidates, servings_for,
)

router = APIRouter(prefix="/api/my-foods", tags=["my-foods"])


class UnitIn(BaseModel):
    label: str = Field(min_length=1, max_length=24)
    factor: float = Field(gt=0)          # servings per ONE of this unit


class ComponentIn(BaseModel):
    my_food_id: int
    servings: float = Field(default=1.0, gt=0)


# Drink kinds (0067, note 34) — the alcohol logger's estimate categories plus a slot for
# the non-alcoholic drinks that share the Drinks meal (cold brew, prebiotic soda).
DrinkKind = Literal["beer", "beer_light", "wine", "liquor", "cocktail", "seltzer", "non_alcoholic"]


def _check_drink(abv, serving_unit) -> None:
    """An alcoholic drink's serving must be in oz — standard drinks are oz x ABV."""
    if abv and abv > 0 and (serving_unit or "").strip().lower() != "oz":
        raise HTTPException(status_code=422,
                            detail="A drink with an ABV needs its serving in oz (e.g. 12 oz).")


class MyFoodIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    brand: Optional[str] = Field(default=None, max_length=40)   # 0070, note 60
    role: Literal["food", "topping", "combo"] = "food"
    meal_type: Optional[str] = None
    serving_label: Optional[str] = None
    serving_qty: float = Field(default=1.0, gt=0)
    serving_unit: str = "serving"
    units: list[UnitIn] = []
    calories: Optional[float] = Field(default=None, ge=0)   # computed for a combo
    protein_g: Optional[float] = Field(default=None, ge=0)
    carbs_g: Optional[float] = Field(default=None, ge=0)
    fat_g: Optional[float] = Field(default=None, ge=0)
    fiber_g: Optional[float] = Field(default=None, ge=0)
    sugar_g: Optional[float] = Field(default=None, ge=0)
    sodium_mg: Optional[float] = Field(default=None, ge=0)
    source: Literal["label", "confirmed", "estimate"] = "confirmed"
    notes: Optional[str] = None
    aliases: list[str] = []              # other names / item keys this food answers to
    components: list[ComponentIn] = []   # role='combo' only
    drink_kind: Optional[DrinkKind] = None
    abv: Optional[float] = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def _combo_rules(self):
        if self.role == "combo" and not self.components:
            raise ValueError("a combo needs at least one component")
        if self.role != "combo" and self.calories is None:
            raise ValueError("calories is required")
        return self


class MyFoodPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    brand: Optional[str] = Field(default=None, max_length=40)   # "" clears it
    role: Optional[Literal["food", "topping", "combo"]] = None
    meal_type: Optional[str] = None
    serving_label: Optional[str] = None
    serving_qty: Optional[float] = Field(default=None, gt=0)
    serving_unit: Optional[str] = None
    units: Optional[list[UnitIn]] = None
    calories: Optional[float] = Field(default=None, ge=0)
    protein_g: Optional[float] = Field(default=None, ge=0)
    carbs_g: Optional[float] = Field(default=None, ge=0)
    fat_g: Optional[float] = Field(default=None, ge=0)
    fiber_g: Optional[float] = Field(default=None, ge=0)
    sugar_g: Optional[float] = Field(default=None, ge=0)
    sodium_mg: Optional[float] = Field(default=None, ge=0)
    source: Optional[Literal["label", "confirmed", "estimate"]] = None
    notes: Optional[str] = None
    aliases: Optional[list[str]] = None
    components: Optional[list[ComponentIn]] = None
    drink_kind: Optional[DrinkKind] = None
    abv: Optional[float] = Field(default=None, ge=0, le=100)


def _ser(f: MyFood, uses: int = 0, last_used=None) -> dict:
    return {
        "id": f.id, "name": f.name, "brand": f.brand, "item_key": f.item_key, "role": f.role,
        "meal_type": f.meal_type, "serving_label": f.serving_label,
        "serving_qty": f.serving_qty, "serving_unit": f.serving_unit, "units": food_units(f),
        **{m: getattr(f, m) for m in MACROS},
        "source": f.source, "notes": f.notes, "archived": bool(f.archived),
        "aliases": food_aliases(f), "components": food_components(f),
        "drink_kind": f.drink_kind, "abv": f.abv,
        "uses": uses, "last_used": last_used.date().isoformat() if last_used else None,
        "updated_at": f.updated_at.isoformat() if f.updated_at else None,
    }


async def _get(db: AsyncSession, food_id: int) -> MyFood:
    f = (await db.execute(select(MyFood).where(MyFood.id == food_id))).scalar_one_or_none()
    if f is None:
        raise HTTPException(status_code=404, detail="My Food not found")
    return f


async def _key_free(db: AsyncSession, key: str, except_id: Optional[int] = None):
    """A key (own name or alias) may belong to one food only — or two foods would
    fight over the same logged entries."""
    for other in (await db.execute(select(MyFood))).scalars().all():
        if other.id != except_id and key in food_keys(other):
            raise HTTPException(status_code=409, detail={
                "message": f"'{other.name}' already answers to that name"
                           + (" (archived)" if other.archived else ""),
                "id": other.id})


async def _alias_keys(db: AsyncSession, names: list[str], own_key: str,
                      except_id: Optional[int] = None) -> list[str]:
    keys = sorted({item_key(n, None) for n in names if n and n.strip()} - {own_key, ""})
    for k in keys:
        await _key_free(db, k, except_id)
    return keys


async def _combo_totals(db: AsyncSession, comps: list[dict]) -> dict:
    """Per-serving macros of a combo = the sum of its parts x their servings."""
    ids = [c["my_food_id"] for c in comps]
    parts = {f.id: f for f in (await db.execute(select(MyFood).where(MyFood.id.in_(ids)))).scalars().all()}
    tot = {m: None for m in MACROS}
    for c in comps:
        p = parts.get(c["my_food_id"])
        if p is None or p.archived:
            raise HTTPException(status_code=422, detail=f"component {c['my_food_id']} missing or archived")
        if p.role == "combo":
            raise HTTPException(status_code=422, detail="a combo cannot contain another combo")
        for m, v in scaled(p, c["servings"]).items():
            if v is not None:
                tot[m] = round((tot[m] or 0) + v, 1)
    if tot["calories"] is not None:
        tot["calories"] = round(tot["calories"])
    return tot


async def _resync_combos(db: AsyncSession, part_id: int) -> None:
    """A part changed: every combo containing it re-sums (library only — past
    entries still move only through apply-to-past)."""
    combos = (await db.execute(select(MyFood).where(MyFood.role == "combo"))).scalars().all()
    for cb in combos:
        comps = food_components(cb)
        if any(c["my_food_id"] == part_id for c in comps):
            try:
                for m, v in (await _combo_totals(db, comps)).items():
                    setattr(cb, m, v)
                cb.updated_at = datetime.utcnow()
            except HTTPException:
                pass                     # an archived part: leave the combo as it was


@router.get("")
async def list_my_foods(
    include_archived: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    q = select(MyFood).order_by(MyFood.name)
    if not include_archived:
        q = q.where(MyFood.archived.isnot(True))
    foods = (await db.execute(q)).scalars().all()
    usage = {fid: (n, last) for fid, n, last in (await db.execute(
        select(FoodLog.my_food_id, func.count(), func.max(FoodLog.timestamp))
        .where(FoodLog.my_food_id.isnot(None)).group_by(FoodLog.my_food_id)
    )).all()}
    # Logged count per MEAL (2026-10-21): the Log Food picker shows a meal's foods as the
    # ones tagged for it OR actually logged at it — one home meal per food is too narrow
    # (a protein bar is a snack and a lunch).
    meal_uses: dict = defaultdict(dict)
    for fid, mt, n in (await db.execute(
        select(FoodLog.my_food_id, FoodLog.meal_type, func.count())
        .where(FoodLog.my_food_id.isnot(None))
        .where((FoodLog.skipped == False) | (FoodLog.skipped.is_(None)))  # noqa: E712
        .group_by(FoodLog.my_food_id, FoodLog.meal_type)
    )).all():
        meal_uses[fid][mt or "snack"] = n
    # "Usually with" (Phase 2): toppings for each base, from the user's combinations
    # (ranked first) and from foods actually logged together in one meal.
    live = {f.id: f for f in foods}
    pair: dict = defaultdict(Counter)
    for cb in foods:
        if cb.role != "combo":
            continue
        ids = [c["my_food_id"] for c in food_components(cb) if c["my_food_id"] in live]
        if not ids:
            continue
        base = next((i for i in ids if live[i].role != "topping"), ids[0])
        for t in ids:
            if t != base:
                pair[base][t] += 1000
    meals = defaultdict(set)
    for ts, mt, fid in (await db.execute(
        select(FoodLog.timestamp, FoodLog.meal_type, FoodLog.my_food_id).where(FoodLog.my_food_id.isnot(None))
    )).all():
        meals[(ts, mt)].add(fid)
    for ids in meals.values():
        for b in ids:
            if b in live and live[b].role not in ("topping", "combo"):
                for t in ids:
                    if t != b and t in live and live[t].role == "topping":
                        pair[b][t] += 1
    return [{**_ser(f, *usage.get(f.id, (0, None))),
             "meal_uses": meal_uses.get(f.id, {}),
             "usually_with": [t for t, _n in pair[f.id].most_common(3)]} for f in foods]


@router.post("", status_code=201)
async def create_my_food(body: MyFoodIn, db: AsyncSession = Depends(get_db),
                         _: str = Depends(get_current_user)):
    body.brand = (body.brand or "").strip() or None
    key = item_key(body.name, None, body.brand)
    await _key_free(db, key)
    _check_drink(body.abv, body.serving_unit)
    data = body.model_dump()
    data["units"] = json.dumps(data.pop("units"))
    data["aliases"] = json.dumps(await _alias_keys(db, data.pop("aliases"), key))
    comps = data.pop("components")
    data["components"] = json.dumps(comps) if body.role == "combo" else None
    if body.role == "combo":
        data.update(await _combo_totals(db, comps))
    f = MyFood(item_key=key, **data, created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(f)
    await db.commit()
    await db.refresh(f)
    return _ser(f)


@router.put("/{food_id}")
async def update_my_food(food_id: int, body: MyFoodPatch, db: AsyncSession = Depends(get_db),
                         _: str = Depends(get_current_user)):
    """Changes the LIBRARY only. Past entries keep their snapshot until
    apply-to-past is called explicitly."""
    f = await _get(db, food_id)
    data = body.model_dump(exclude_unset=True)
    if "brand" in data:
        data["brand"] = (data["brand"] or "").strip() or None
    if "name" in data or "brand" in data:
        # The key is brand + name, so changing EITHER re-keys the food.
        key = item_key(data.get("name", f.name), None, data.get("brand", f.brand))
        await _key_free(db, key, except_id=f.id)
        f.item_key = key
    if "units" in data:
        data["units"] = json.dumps(data["units"] or [])
    if "aliases" in data:
        data["aliases"] = json.dumps(await _alias_keys(db, data["aliases"] or [], f.item_key, f.id))
    comps = data.pop("components", None)
    _check_drink(data.get("abv", f.abv), data.get("serving_unit", f.serving_unit))
    for k, v in data.items():
        setattr(f, k, v)
    if f.role == "combo":
        if comps is not None:
            f.components = json.dumps(comps)
        for m, v in (await _combo_totals(db, food_components(f))).items():
            setattr(f, m, v)
    else:
        await _resync_combos(db, f.id)
    f.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(f)
    return _ser(f)


@router.delete("/{food_id}")
async def archive_my_food(food_id: int, db: AsyncSession = Depends(get_db),
                          _: str = Depends(get_current_user)):
    """Archive (soft). Linked entries keep their link and their numbers."""
    f = await _get(db, food_id)
    f.archived = True
    f.updated_at = datetime.utcnow()
    await db.commit()
    return {"id": f.id, "archived": True}


class FromLogIn(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    role: Literal["food", "topping"] = "food"
    serving_label: Optional[str] = None


@router.post("/from-log/{log_id}", status_code=201)
async def my_food_from_log(log_id: int, body: FromLogIn, db: AsyncSession = Depends(get_db),
                           _: str = Depends(get_current_user)):
    """"Make this a My Food": one serving = that entry's portion, at its numbers."""
    r = (await db.execute(select(FoodLog).where(FoodLog.id == log_id))).scalar_one_or_none()
    if r is None:
        raise HTTPException(status_code=404, detail="Log entry not found")
    if r.calories is None:
        raise HTTPException(status_code=422, detail="Entry has no calories to build from")
    name = (body.name or r.name or r.description or "").strip()[:80]
    key = item_key(name, None, r.brand)
    await _key_free(db, key)
    qty, unit = (r.portion_quantity or 1.0), (r.portion_unit or "serving")
    f = MyFood(name=name, brand=r.brand, item_key=key, role=body.role, meal_type=r.meal_type,
               serving_label=body.serving_label or f"{qty:g} {unit}", serving_qty=qty,
               serving_unit=unit, units="[]", source="confirmed",
               **{m: getattr(r, m) for m in MACROS},
               created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(f)
    await db.flush()
    r.my_food_id, r.servings = f.id, 1.0
    await db.commit()
    await db.refresh(f)
    return _ser(f, 1, r.timestamp)


class ComboPartIn(BaseModel):
    my_food_id: Optional[int] = None     # an existing food, or…
    food: Optional[MyFoodIn] = None      # …a new one (reused if its name already exists)
    servings: float = Field(default=1.0, gt=0)

    @model_validator(mode="after")
    def _one(self):
        if (self.my_food_id is None) == (self.food is None):
            raise ValueError("each part needs my_food_id or food, not both")
        if self.food is not None and self.food.role == "combo":
            raise ValueError("a part cannot itself be a combo")
        return self


class ComboIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    meal_type: Optional[str] = None
    serving_qty: float = Field(default=1.0, gt=0)
    serving_unit: str = "serving"
    serving_label: Optional[str] = None
    aliases: list[str] = []
    parts: list[ComboPartIn] = Field(min_length=2)
    source: Literal["label", "confirmed", "estimate"] = "confirmed"


@router.post("/combo", status_code=201)
async def create_combo(body: ComboIn, db: AsyncSession = Depends(get_db),
                       _: str = Depends(get_current_user)):
    """Base + toppings + the combo in ONE transaction.

    WHY (2026-09-13). The split form used to make three separate creates. Building
    "Personal pizza with chickpea crust", the base saved WITH the ticked alias, then the
    combo claimed the same alias and got a 409 — leaving an orphan base that the retry
    collided with. Now nothing is written unless everything succeeds, and:
      • a part whose name already belongs to a live food REUSES it (numbers untouched —
        edit them in the Library), so one cream cheese serves every bagel;
      • an alias the combo claims that one of its parts holds MOVES to the combo —
        name variants of the combined dish belong to the combination, not its base.
    """
    now = datetime.utcnow()
    combo_key = item_key(body.name, None)
    foods = list((await db.execute(select(MyFood))).scalars().all())

    def holder(key):
        return next((f for f in foods if key in food_keys(f)), None)

    if (h := holder(combo_key)) is not None:
        raise HTTPException(status_code=409, detail={
            "message": f"'{h.name}' already answers to '{body.name}'", "id": h.id})
    comps, parts_out = [], []
    for p in body.parts:
        if p.my_food_id is not None:
            f = next((x for x in foods if x.id == p.my_food_id), None)
            if f is None or f.archived:
                raise HTTPException(status_code=422, detail=f"food {p.my_food_id} missing or archived")
            reused = True
        else:
            key = item_key(p.food.name, None, (p.food.brand or "").strip() or None)
            if key == combo_key:
                raise HTTPException(status_code=422, detail="a part can't have the combination's own name")
            f = holder(key)
            if f is not None:
                if f.archived:
                    raise HTTPException(status_code=409, detail=f"'{f.name}' is archived — restore or rename it first")
                reused = True
            else:
                data = p.food.model_dump(exclude={"aliases", "components"})
                data["units"] = json.dumps(data.pop("units"))
                f = MyFood(item_key=key, aliases="[]", components=None, **data,
                           created_at=now, updated_at=now)
                db.add(f)
                await db.flush()
                foods.append(f)
                reused = False
        if f.role == "combo":
            raise HTTPException(status_code=422, detail=f"'{f.name}' is a combination; parts must be single foods")
        comps.append({"my_food_id": f.id, "servings": p.servings})
        parts_out.append({"id": f.id, "name": f.name, "role": f.role, "reused": reused})
    part_ids = {c["my_food_id"] for c in comps}
    alias_keys = sorted({item_key(a, None) for a in body.aliases if a and a.strip()} - {combo_key, ""})
    moved = []
    for k in alias_keys:
        h = holder(k)
        if h is None:
            continue
        if h.id in part_ids and k != h.item_key:
            h.aliases = json.dumps([a for a in food_aliases(h) if a != k])
            h.updated_at = now
            moved.append({"alias": k, "from": h.name})
        else:
            raise HTTPException(status_code=409, detail={
                "message": f"'{h.name}' already answers to that name", "id": h.id})
    combo = MyFood(name=body.name.strip(), item_key=combo_key, role="combo", meal_type=body.meal_type,
                   serving_qty=body.serving_qty, serving_unit=body.serving_unit,
                   serving_label=body.serving_label or f"{body.serving_qty:g} {body.serving_unit}",
                   units="[]", aliases=json.dumps(alias_keys), components=json.dumps(comps),
                   source=body.source, created_at=now, updated_at=now,
                   **(await _combo_totals(db, comps)))
    db.add(combo)
    await db.commit()
    await db.refresh(combo)
    return {"combo": _ser(combo), "parts": parts_out, "moved_aliases": moved}


class ComboItemIn(BaseModel):
    """One part of a combination built from what was eaten: a logged entry (log_id) or an
    unsaved Log Food tray item (its fields inline)."""
    log_id: Optional[int] = None
    my_food_id: Optional[int] = None
    servings: Optional[float] = Field(default=None, gt=0)
    name: Optional[str] = None
    brand: Optional[str] = None
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


class ComboFromItemsIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    meal_type: Optional[str] = None
    items: list[ComboItemIn] = Field(min_length=2)
    # Group the logged entries under the new combination, as if it had been logged.
    regroup: bool = True


@router.post("/combo-from-items", status_code=201)
async def combo_from_items(body: ComboFromItemsIn, db: AsyncSession = Depends(get_db),
                           _: str = Depends(get_current_user)):
    """A meal logged piece by piece → one My Food combination (2026-11-12: "spinach
    salad + beef + ranch → Taco salad").

    Each item becomes a part, in the order given (the first is the base):
      • an item that already IS a My Food → that food at the servings it was logged at;
      • an item whose name a live food already answers to → that food, at servings =
        this item's kcal / the food's kcal per serving. Assuming 1 serving would be wrong
        whenever the portion differs (1 tbsp of ranch against a 2 tbsp My Food);
      • anything else → a new single food, one serving = this portion at these numbers,
        source "estimate" (a parse is a guess; a label scan can confirm it later).
    Two items that land on the same food become one part. Nothing is written unless all of
    it succeeds. With `regroup`, the logged entries are linked to their parts and grouped
    under the combination's name, so the day reads as the dish.
    """
    from uuid import uuid4
    now = datetime.utcnow()
    name = body.name.strip()
    combo_key = item_key(name, None)
    foods = list((await db.execute(select(MyFood))).scalars().all())

    def holder(key):
        return next((f for f in foods if key in food_keys(f)), None)

    if (h := holder(combo_key)) is not None:
        raise HTTPException(status_code=409, detail={
            "message": f"'{h.name}' already answers to '{name}'", "id": h.id})

    log_ids = [i.log_id for i in body.items if i.log_id is not None]
    logs = {r.id: r for r in (await db.execute(select(FoodLog).where(FoodLog.id.in_(log_ids)))).scalars().all()} \
        if log_ids else {}
    resolved = []        # (item, log row or None, food, servings, new?)
    for it in body.items:
        row = None
        if it.log_id is not None:
            row = logs.get(it.log_id)
            if row is None:
                raise HTTPException(status_code=404, detail=f"log entry {it.log_id} not found")
            if row.source_kind or row.skipped:
                raise HTTPException(status_code=422, detail="drinks logged with a dose and skipped meals can't be parts")
            it = ComboItemIn(log_id=row.id, my_food_id=row.my_food_id, servings=row.servings,
                             name=row.name, brand=row.brand, description=row.description,
                             portion_quantity=row.portion_quantity, portion_unit=row.portion_unit,
                             **{m: getattr(row, m) for m in MACROS})
        label = (it.name or it.description or "").strip()[:80]
        f = next((x for x in foods if x.id == it.my_food_id), None) if it.my_food_id else None
        if f is not None and not f.archived and f.role != "combo":
            resolved.append((it, row, f, it.servings or 1.0, False))
            continue
        if not label:
            raise HTTPException(status_code=422, detail="every part needs a name")
        if it.calories is None:
            raise HTTPException(status_code=422, detail=f"'{label}' has no calories to build from")
        key = item_key(label, None, (it.brand or "").strip() or None)
        if key == combo_key:
            raise HTTPException(status_code=422, detail="a part can't have the combination's own name")
        f = holder(key)
        if f is not None:
            if f.archived:
                raise HTTPException(status_code=409, detail=f"'{f.name}' is archived — restore or rename it first")
            if f.role == "combo":
                raise HTTPException(status_code=422, detail=f"'{f.name}' is a combination; parts must be single foods")
            servings = round(it.calories / f.calories, 2) if f.calories and it.calories else 1.0
            resolved.append((it, row, f, servings or 1.0, False))
            continue
        qty, unit = (it.portion_quantity or 1.0), (it.portion_unit or "serving")
        f = MyFood(name=label, brand=(it.brand or "").strip() or None, item_key=key, role="food",
                   meal_type=body.meal_type, serving_label=f"{qty:g} {unit}", serving_qty=qty,
                   serving_unit=unit, units="[]", aliases="[]", components=None, source="estimate",
                   **{m: getattr(it, m) for m in MACROS}, created_at=now, updated_at=now)
        db.add(f)
        await db.flush()
        foods.append(f)
        resolved.append((it, row, f, 1.0, True))

    comps: dict[int, float] = {}
    for _it, _row, f, s, _new in resolved:
        comps[f.id] = round(comps.get(f.id, 0) + s, 2)
    if len(comps) < 2:
        raise HTTPException(status_code=422, detail="a combination needs at least two different foods")
    comp_list = [{"my_food_id": fid, "servings": s} for fid, s in comps.items()]
    combo = MyFood(name=name, item_key=combo_key, role="combo", meal_type=body.meal_type,
                   serving_qty=1.0, serving_unit="serving", serving_label="1 serving", units="[]",
                   aliases="[]", components=json.dumps(comp_list), source="estimate"
                   if any(new or f.source == "estimate" for _i, _r, f, _s, new in resolved) else "confirmed",
                   created_at=now, updated_at=now, **(await _combo_totals(db, comp_list)))
    db.add(combo)

    group = uuid4().hex[:12]
    if body.regroup:
        for n, (_it, row, f, s, _new) in enumerate(resolved):
            if row is None:
                continue
            row.my_food_id, row.servings = f.id, s
            row.combo_group, row.combo_name = group, name
            row.combo_part = "base" if n == 0 else "topping"
    await db.commit()
    await db.refresh(combo)
    return {
        "combo": _ser(combo),
        # Per item, in request order: what it became, for the tray to link its rows.
        "items": [{"my_food_id": f.id, "name": f.name, "servings": s, "created": new,
                   "part": "base" if n == 0 else "topping"}
                  for n, (_it, _row, f, s, new) in enumerate(resolved)],
        "combo_group": group,
    }


class DismissIn(BaseModel):
    item_key: str = Field(min_length=1, max_length=120)


@router.post("/candidates/dismiss", status_code=201)
async def dismiss_candidate(body: DismissIn, db: AsyncSession = Depends(get_db),
                            _: str = Depends(get_current_user)):
    """"Skip" in the seeding review — the candidate stays gone until restored."""
    key = item_key(body.item_key, None)
    if (await db.execute(select(MyFoodDismissal).where(MyFoodDismissal.item_key == key))).scalar_one_or_none() is None:
        db.add(MyFoodDismissal(item_key=key, created_at=datetime.utcnow()))
        await db.commit()
    return {"item_key": key, "dismissed": True}


@router.delete("/candidates/dismiss")
async def restore_candidate(item_key_q: str = Query(..., alias="item_key"),
                            db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)):
    row = (await db.execute(select(MyFoodDismissal)
                            .where(MyFoodDismissal.item_key == item_key(item_key_q, None)))).scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.commit()
    return {"restored": row is not None}


@router.get("/candidates/dismissed")
async def list_dismissed(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)):
    rows = (await db.execute(select(MyFoodDismissal).order_by(MyFoodDismissal.created_at.desc()))).scalars().all()
    return [{"item_key": r.item_key, "at": r.created_at.isoformat() if r.created_at else None} for r in rows]


@router.get("/candidates")
async def my_food_candidates(
    days: int = Query(default=180, ge=14, le=730),
    min_uses: int = Query(default=3, ge=2),
    per_group: int = Query(default=15, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Seeding review source: repeat foods grouped by meal type, top N per group."""
    return await seed_candidates(db, days=days, min_uses=min_uses, per_group=per_group)


def _row_change(r: FoodLog, s: Optional[float], food) -> dict:
    after = scaled(food, s) if s is not None else None
    return {"id": r.id, "date": food_day(r.timestamp).isoformat(), "name": r.name,
            "portion": f"{r.portion_quantity or 1:g} {r.portion_unit or ''}".strip(),
            "servings": None if s is None else round(s, 3),
            "before": {m: getattr(r, m) for m in MACROS}, "after": after,
            "changes": after is not None and any(
                (getattr(r, m) or 0) != (after[m] or 0) for m in MACROS if after[m] is not None)}


@router.get("/{food_id}/past-entries")
async def past_entries_preview(food_id: int, db: AsyncSession = Depends(get_db),
                               _: str = Depends(get_current_user)):
    """What apply-to-past WOULD do. Entries in a unit this food cannot convert are
    listed separately and never touched — add the unit, then preview again."""
    f = await _get(db, food_id)
    rows = [_row_change(r, s, f) for r, s in await past_matches(db, f)]
    rows.sort(key=lambda x: x["date"], reverse=True)
    return {"matching": len(rows),
            "would_change": sum(1 for x in rows if x["changes"]),
            "unit_mismatch": [x for x in rows if x["servings"] is None],
            "rows": rows}


class ApplyIn(BaseModel):
    ids: Optional[list[int]] = None      # None = every convertible matching entry


@router.post("/{food_id}/apply-to-past")
async def apply_to_past(food_id: int, body: ApplyIn, db: AsyncSession = Depends(get_db),
                        _: str = Depends(get_current_user)):
    """Rewrite matching past entries to this food's numbers x their servings, and link
    them. Prior values are stored first (my_food_backfills) — undo restores them."""
    f = await _get(db, food_id)
    pick = set(body.ids) if body.ids is not None else None
    targets = [(r, s) for r, s in await past_matches(db, f)
               if s is not None and (pick is None or r.id in pick)]
    if not targets:
        return {"backfill_id": None, "updated": 0}
    snap = [{"id": r.id, "my_food_id": r.my_food_id, "servings": r.servings,
             **{m: getattr(r, m) for m in MACROS}} for r, _s in targets]
    bf = MyFoodBackfill(my_food_id=f.id, created_at=datetime.utcnow(), n_rows=len(snap),
                        rows_json=json.dumps(snap))
    db.add(bf)
    for r, s in targets:
        for m, v in scaled(f, s).items():
            if v is not None:
                setattr(r, m, v)
        r.my_food_id, r.servings = f.id, round(s, 3)
    await db.commit()
    await db.refresh(bf)
    return {"backfill_id": bf.id, "updated": len(targets)}


@router.post("/backfills/{backfill_id}/undo")
async def undo_backfill(backfill_id: int, db: AsyncSession = Depends(get_db),
                        _: str = Depends(get_current_user)):
    bf = (await db.execute(select(MyFoodBackfill).where(MyFoodBackfill.id == backfill_id))).scalar_one_or_none()
    if bf is None:
        raise HTTPException(status_code=404, detail="Backfill not found")
    if bf.undone_at is not None:
        raise HTTPException(status_code=409, detail="Already undone")
    snap = {x["id"]: x for x in json.loads(bf.rows_json)}
    rows = (await db.execute(select(FoodLog).where(FoodLog.id.in_(list(snap))))).scalars().all()
    for r in rows:
        for k, v in snap[r.id].items():
            if k != "id":
                setattr(r, k, v)
    bf.undone_at = datetime.utcnow()
    await db.commit()
    return {"restored": len(rows), "missing": len(snap) - len(rows)}
