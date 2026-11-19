"""My Foods — the personal food library (docs/MY_FOODS.md). Phase 1: backend.

A My Food is one food with numbers the user has checked: macros PER SERVING (carbs TOTAL,
fiber included), one serving definition, and alternate units as factors of it. Logged
rows keep their own macros — a snapshot — and change only through apply-to-past.
"""
from __future__ import annotations

import json
import re
import statistics as st
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FoodLog, MyFood, MyFoodDismissal
from app.services.food_tags import item_key

MACROS = ("calories", "protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g", "sodium_mg")
_WHOLE = ("calories", "sodium_mg")          # rounded to integers, the rest to 0.1

_ALIASES = {
    "pieces": "piece", "pc": "piece", "pcs": "piece", "each": "piece", "item": "piece",
    "items": "piece", "bars": "bar", "bagels": "bagel", "slices": "slice",
    "servings": "serving", "links": "link", "cans": "can", "bottles": "bottle",
    "eggs": "egg", "patties": "patty", "scoops": "scoop", "pies": "pie", "pizzas": "pie",
    "pizza": "pie", "sandwiches": "sandwich", "burgers": "burger", "tacos": "taco",
    "cookies": "cookie", "burritos": "burrito", "grams": "g", "gram": "g", "gr": "g",
    "ounce": "oz", "ounces": "oz", "fl oz": "oz", "floz": "oz", "milliliter": "ml",
    "milliliters": "ml", "mls": "ml", "tablespoon": "tbsp", "tablespoons": "tbsp",
    "tbs": "tbsp", "teaspoon": "tsp", "teaspoons": "tsp", "cups": "cup",
}
# Units that each mean "one of the thing": a bar logged as "1 piece" is one bar. NOT
# "slice" — a pizza slice is a fraction of the serving and belongs in `units`.
COUNT_UNITS = {"piece", "bar", "bagel", "serving", "link", "egg", "patty", "can", "bottle",
               "scoop", "pie", "sandwich", "burger", "taco", "cookie", "burrito"}
MEAL_ORDER = ("breakfast", "lunch", "dinner", "dessert", "snack", "drinks")
_COMBO_RE = re.compile(r"\b(with|and)\b|&|\+", re.I)


# Names that describe a logging channel, not a food ("Scanned nutrition label" was five
# different products merged by name in the live candidate list).
_GENERIC_KEYS = {"scanned nutrition label", "skipped", "meal", "food", "snack", "unknown"}
_STOP = {"with", "and", "the", "a", "an", "of", "i", "had", "some", "small", "large", "style"}


def name_tokens(name: str) -> set:
    return {t for t in re.findall(r"[a-z0-9]+", (name or "").lower()) if t not in _STOP}


def similar_score(a: str, b: str) -> float:
    """Jaccard over content words. 0.66 separates "Personal pizza (with) chickpea crust"
    (1.0) and "Roasted salmon (fillet)" (0.67) from Parmesan vs Berry bagel with cream
    cheese (0.6) — different foods that share a topping."""
    ta, tb = name_tokens(a), name_tokens(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


SIMILAR_MIN = 0.66


def split_hint(name: str) -> Optional[dict]:
    """"X with Y" -> base X, topping Y. Only a starting point for the review form."""
    m = re.split(r"\s+(?:with|and)\s+|\s*[&+]\s*", (name or "").strip(), maxsplit=1)
    if len(m) != 2 or not m[0].strip() or not m[1].strip():
        return None
    return {"base": m[0].strip(), "topping": m[1].strip()[:1].upper() + m[1].strip()[1:]}


def _json_list(raw) -> list:
    try:
        v = json.loads(raw or "[]")
        return v if isinstance(v, list) else []
    except (TypeError, ValueError):
        return []


def food_aliases(food) -> list[str]:
    return [a for a in _json_list(getattr(food, "aliases", None)) if isinstance(a, str) and a]


def food_keys(food) -> set:
    return {food.item_key, *food_aliases(food)}


def food_components(food) -> list[dict]:
    out = []
    for c in _json_list(getattr(food, "components", None)):
        try:
            fid, s = int(c["my_food_id"]), float(c.get("servings") or 1)
        except (KeyError, TypeError, ValueError):
            continue
        if s > 0:
            out.append({"my_food_id": fid, "servings": s})
    return out


def norm_unit(u: Optional[str]) -> str:
    u = (u or "").strip().lower().rstrip(".")
    return _ALIASES.get(u, u)


def food_units(food) -> list[dict]:
    try:
        return [u for u in json.loads(food.units or "[]")
                if isinstance(u, dict) and u.get("label") and float(u.get("factor") or 0) > 0]
    except (TypeError, ValueError):
        return []


def servings_for(food, qty, unit) -> Optional[float]:
    """How many of the food's servings `qty unit` is; None when the unit is foreign
    to this food (then nothing is guessed)."""
    try:
        q = 1.0 if qty is None else float(qty)
    except (TypeError, ValueError):
        return None
    if q <= 0:
        return None
    u, su = norm_unit(unit), norm_unit(food.serving_unit)
    sq = float(food.serving_qty or 1)
    if not u or u == su or (u in COUNT_UNITS and su in COUNT_UNITS):
        return q / sq
    for alt in food_units(food):
        if norm_unit(alt["label"]) == u:
            return q * float(alt["factor"])
    return None


def scaled(food, servings: float) -> dict:
    return {m: (None if getattr(food, m) is None else
                round(float(getattr(food, m)) * servings, 0 if m in _WHOLE else 1))
            for m in MACROS}


async def foods_by_key(db: AsyncSession) -> dict:
    """Every key a live food answers to — its own and its aliases."""
    rows = (await db.execute(select(MyFood).where(MyFood.archived.isnot(True)))).scalars().all()
    return {k: f for f in rows for k in food_keys(f)}


async def known_brands(db: AsyncSession) -> list[str]:
    """Distinct brands already used on entries or My Foods, most-used first. Casing is
    the most common spelling, so "kirkland" typed once doesn't fork the list."""
    from sqlalchemy import func
    counts: Counter = Counter()
    spelling: dict[str, Counter] = defaultdict(Counter)
    for model in (FoodLog, MyFood):
        for b, n in (await db.execute(
            select(model.brand, func.count()).where(model.brand.isnot(None)).group_by(model.brand)
        )).all():
            b = (b or "").strip()
            if b:
                counts[b.lower()] += n
                spelling[b.lower()][b] += n
    return [spelling[k].most_common(1)[0][0] for k, _n in counts.most_common()]


async def split_known_brands(db: AsyncSession, items: list[dict]) -> None:
    """Move a leading KNOWN brand out of a parsed item's name, in place.

    The parser still writes "Kirkland cream cheese" into names (and older cached
    parses already say so). Only brands the user has already entered are recognised —
    a deterministic prefix match against their own list, never a guess — so the field
    stays user-owned while typed entries stop re-growing brands in their names.
    """
    todo = [it for it in items if not (it.get("brand") or "").strip() and (it.get("name") or "").strip()]
    if not todo:
        return
    brands = sorted(await known_brands(db), key=len, reverse=True)   # longest first
    for it in todo:
        name = it["name"].strip()
        low = name.lower()
        for b in brands:
            if low.startswith(b.lower() + " ") and len(name) > len(b) + 1:
                rest = name[len(b):].strip()
                it["brand"], it["name"] = b, rest[:1].upper() + rest[1:]
                break


async def apply_my_foods(db: AsyncSession, result: Optional[dict]) -> Optional[dict]:
    """Swap in My Foods numbers for every parsed / cached / history item whose name
    matches a My Food. Numbers the user TYPED exactly (`_stated`) win — they describe
    this instance — and a unit the food cannot convert is left alone with a hint."""
    items = (result or {}).get("items") or []
    if not items:
        return result
    foods = await foods_by_key(db)
    if not foods:
        return result
    by_id = {f.id: f for f in foods.values()}
    out = []
    for n_item, it in enumerate(items):
        f = (foods.get(item_key(it.get("name"), None, it.get("brand")))
             or foods.get(item_key(None, it.get("description"), it.get("brand"))))
        if f is None:
            out.append(it)
            continue
        if it.get("_stated"):
            it["my_food_hint"] = {"id": f.id, "name": f.name, "why": "typed numbers kept"}
            out.append(it)
            continue
        s = servings_for(f, it.get("portion_quantity"), it.get("portion_unit"))
        if s is None:
            it["my_food_hint"] = {"id": f.id, "name": f.name,
                                  "why": f"unit '{it.get('portion_unit')}' not defined for this food"}
            out.append(it)
            continue
        if f.role == "combo":
            # A combination logs as ONE ROW PER PART (2026-09-13) — the bagel
            # and the cream cheese each carry their own numbers and food-group tags.
            parts = [(by_id.get(c["my_food_id"]), c["servings"]) for c in food_components(f)]
            if not parts or any(p is None for p, _ in parts):
                it["my_food_hint"] = {"id": f.id, "name": f.name, "why": "a part of this combo is missing or archived"}
                out.append(it)
                continue
            # The BASE is the first non-topping part, in the order the combo was built;
            # EVERY other part follows it as a topping — on a pizza that is the sauce AND
            # the cheese, even though the cheese is saved as a food (2026-09-13). Base
            # first in the output; one group id per expanded item.
            bi = next((i for i, (p, _s) in enumerate(parts) if p.role != "topping"), 0)
            parts = [parts[bi]] + parts[:bi] + parts[bi + 1:]
            group = f"{f.id}-{n_item}"
            for k, (p, ps) in enumerate(parts):
                sv = s * ps
                out.append({"name": p.name, "brand": p.brand,
                            "description": f"{p.brand} {p.name}" if p.brand else p.name,
                            "portion_quantity": round(sv * float(p.serving_qty or 1), 3),
                            "portion_unit": p.serving_unit, **scaled(p, sv),
                            "my_food_id": p.id, "servings": round(sv, 3), "confidence": "high",
                            "omitted_kcal": 0, "portion_options": None,
                            "notes": f"From My Foods: part of {f.name}.",
                            "combo_group": group, "combo_name": f.name,
                            "combo_part": "base" if k == 0 else "topping"})
            continue
        it.update(scaled(f, s))
        it.update(my_food_id=f.id, servings=round(s, 3), name=f.name, brand=f.brand,
                  confidence="high",
                  portion_options=None,
                  notes=f"From My Foods: {f.serving_label or 'per serving'} × {round(s, 3):g}.")
        out.append(it)
    result["items"] = out
    return result


async def past_matches(db: AsyncSession, food) -> list[tuple]:
    """(row, servings or None) for every entry that is this food: linked rows, plus
    unlinked non-auto rows whose item key matches."""
    rows = (await db.execute(
        select(FoodLog).where(FoodLog.source_kind.is_(None))
        .where((FoodLog.skipped == False) | (FoodLog.skipped.is_(None)))  # noqa: E712
    )).scalars().all()
    out = []
    for r in rows:
        if r.my_food_id == food.id:
            out.append((r, r.servings or servings_for(food, r.portion_quantity, r.portion_unit)))
        elif r.my_food_id is None and item_key(r.name, r.description, r.brand) in food_keys(food):
            out.append((r, servings_for(food, r.portion_quantity, r.portion_unit)))
    return out


async def seed_candidates(db: AsyncSession, days: int = 180, min_uses: int = 3,
                          per_group: int = 15) -> dict:
    """Foods worth a My Food, grouped by the meal they are usually logged in and ranked
    by use. Suggested numbers are medians per unit in the most-used unit; `unit_mixed`
    and `per_unit_spread` show where the history disagrees with itself — the foods a
    My Food helps most."""
    since = datetime.now() - timedelta(days=days)        # food_log is naive LOCAL
    rows = (await db.execute(
        select(FoodLog).where(FoodLog.timestamp >= since)
        .where(FoodLog.source_kind.is_(None)).where(FoodLog.calories.isnot(None))
        .where((FoodLog.skipped == False) | (FoodLog.skipped.is_(None)))  # noqa: E712
    )).scalars().all()
    existing = {k for f in (await db.execute(select(MyFood))).scalars().all() for k in food_keys(f)}
    existing |= set((await db.execute(select(MyFoodDismissal.item_key))).scalars().all())
    existing |= _GENERIC_KEYS
    groups = defaultdict(list)
    for r in rows:
        k = item_key(r.name, r.description, r.brand)
        if k and k not in existing:
            groups[k].append(r)
    by_meal = defaultdict(list)
    for k, rs in groups.items():
        if len(rs) < min_uses:
            continue
        units = Counter(norm_unit(r.portion_unit) or "serving" for r in rs)
        unit = units.most_common(1)[0][0]
        same = [r for r in rs if (norm_unit(r.portion_unit) or "serving") == unit
                and (r.portion_quantity or 1) > 0]
        qty = Counter(round(r.portion_quantity or 1, 3) for r in same).most_common(1)[0][0]
        suggested = {}
        for m in MACROS:
            vals = [getattr(r, m) / (r.portion_quantity or 1) for r in same if getattr(r, m) is not None]
            suggested[m] = None if not vals else round(st.median(vals) * qty, 0 if m in _WHOLE else 1)
        per = [r.calories / (r.portion_quantity or 1) for r in rs if r.calories]
        med = st.median(per) if per else 0
        name = Counter((r.name or r.description or "").strip() for r in rs).most_common(1)[0][0]
        brand = Counter((r.brand or "").strip() for r in rs).most_common(1)[0][0] or None
        by_meal[Counter((r.meal_type or "snack") for r in rs).most_common(1)[0][0]].append({
            "item_key": k, "name": name, "brand": brand, "uses": len(rs),
            "last_used": max(r.timestamp for r in rs).date().isoformat(),
            "serving_qty": qty, "serving_unit": unit, "suggested": suggested,
            "distinct_kcal": len({round(r.calories) for r in rs}),
            "units_seen": dict(units), "unit_mixed": len(units) > 1,
            "per_unit_spread": round((max(per) - min(per)) / med, 2) if len(per) > 1 and med else 0.0,
            "combo": bool(_COMBO_RE.search(name)),
            "starred": any(r.is_saved for r in rs),
        })
    # Likely-duplicate names across ALL groups (a food can be logged at lunch under one
    # name and dinner under another), and a base/topping guess for combinations.
    flat = [c for lst in by_meal.values() for c in lst]
    for c in flat:
        c["similar"] = [{"item_key": o["item_key"], "name": o["name"], "uses": o["uses"]}
                        for o in flat if o is not c and similar_score(c["name"], o["name"]) >= SIMILAR_MIN]
        c["split"] = split_hint(c["name"]) if c["combo"] else None
    order = list(MEAL_ORDER) + sorted(set(by_meal) - set(MEAL_ORDER))
    out = []
    for meal in order:
        lst = sorted(by_meal.get(meal, []), key=lambda c: -c["uses"])
        if lst:
            out.append({"meal_type": meal, "total": len(lst), "candidates": lst[:per_group]})
    return {"days": days, "min_uses": min_uses, "per_group": per_group, "groups": out}
