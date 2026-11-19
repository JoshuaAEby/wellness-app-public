"""Household-app recipes, read-only, with per-serving nutrition estimated once (note 37).

WHY READ-ONLY. The recipes live in the household app's own database (lists.home), mounted
here at `settings.household_db_path` from the `household_data` volume with `:ro`. This app
must never change or delete one. Everything wellness works out — the per-serving estimate,
a servings override, whether the user confirmed it — lives in wellness's own `recipe_nutrition`
table, keyed by the household recipe id.

WHY ESTIMATES ARE STORED AND HASHED. Each estimate is a model call, so it is cached.
`source_hash` covers the ingredient list and the servings, so editing the recipe over there
marks the estimate STALE rather than letting a confirmed row describe the old version.

WHY NOT MY FOODS. 2026-09-15: "I don't want them gumming up the space reserved for
things I eat a lot (My Foods is that)." Recipes are their own section everywhere.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

# household recipes.category → the meal the logger should default to. The household app
# split "meal" into main/soup/side/sauce on 2026-10-28 (its 0030/0032); all four keep the
# old "meal" default, and "meal" stays for any row it left unmigrated.
MEAL_BY_CATEGORY = {"meal": "dinner", "main": "dinner", "soup": "dinner", "side": "dinner",
                    "sauce": "dinner", "dessert": "dessert", "drink": "drinks"}

# Columns read from the household `recipes` table. Only id and title are required; the
# rest are read when present. That table belongs to another app and changes without
# telling this one: its 0031 dropped `tags` on 2026-10-28, a hard-coded SELECT then failed
# on every call, /api/recipes returned 500 and Log Food's Recipes button vanished.
_REQUIRED = ("id", "title")
_OPTIONAL = ("servings", "ingredients", "steps", "tags", "category", "updated_at")
_reported_missing: set[str] = set()


def available() -> bool:
    """False when the household DB isn't mounted — the UI says so instead of erroring."""
    return bool(settings.household_db_path) and os.path.exists(settings.household_db_path)


def _loads(raw, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def ingredient_text(ingredients: list) -> str:
    """"3 pounds chicken tenderloin pieces (sliced), 1 red onion, salt to taste"."""
    parts = []
    for i in ingredients or []:
        if not isinstance(i, dict):
            continue
        qty, unit, name, note = i.get("quantity"), i.get("unit"), i.get("name"), i.get("notes")
        if not name:
            continue
        amount = ""
        if qty is not None:
            q = f"{float(qty):g}"
            amount = f"{q} {unit} " if unit else f"{q} "
        parts.append(f"{amount}{name}" + (f" ({note})" if note else ""))
    return ", ".join(parts)


def recipe_hash(ingredients_raw: Optional[str], servings) -> str:
    h = hashlib.sha256()
    h.update((ingredients_raw or "").encode("utf-8"))
    h.update(f"|{servings}".encode("utf-8"))
    return h.hexdigest()[:16]


def list_recipes() -> list[dict]:
    """Every household recipe, newest-updated first. Read-only; never raises on a
    missing DB — an unmounted volume is reported as an empty list by the router."""
    if not available():
        return []
    con = sqlite3.connect(f"file:{settings.household_db_path}?mode=ro", uri=True)
    try:
        have = {row[1] for row in con.execute("pragma table_info(recipes)")}
        missing = [c for c in _OPTIONAL if c not in have]
        if set(missing) - _reported_missing:
            _reported_missing.update(missing)
            logger.info("household recipes table lacks %s; reading without them", missing)
        cols = [c for c in (*_REQUIRED, *_OPTIONAL) if c in have or c in _REQUIRED]
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            f"select {', '.join(cols)} from recipes order by title"
        ).fetchall()]
    finally:
        con.close()
    out = []
    for r in rows:
        ings = _loads(r.get("ingredients"), [])
        out.append({
            "recipe_id": r["id"],
            "title": r["title"],
            "servings": r.get("servings"),
            "category": r.get("category"),
            "tags": _loads(r.get("tags"), []),
            "ingredients": ings,
            "ingredient_text": ingredient_text(ings),
            "n_ingredients": len(ings),
            "updated_at": r.get("updated_at"),
            "source_hash": recipe_hash(r.get("ingredients"), r.get("servings")),
            "default_meal_type": MEAL_BY_CATEGORY.get((r.get("category") or "").lower()),
        })
    return out


def get_recipe(recipe_id: int) -> Optional[dict]:
    return next((r for r in list_recipes() if r["recipe_id"] == recipe_id), None)


RECIPE_SYSTEM = """You estimate the nutrition of a WHOLE RECIPE from its ingredient list.

Work like this:
1. Convert every ingredient to grams (or ml). Use standard weights, e.g. 1 large potato
   ~300 g, 1 medium onion ~110 g, 1 tbsp oil ~14 g, 1 large egg ~50 g, 1 cup flour ~120 g,
   1 cup milk ~240 ml, 1 stick butter ~113 g, 1 clove garlic ~3 g.
2. Use typical per-100 g values (USDA-style) for each ingredient as it goes in.
3. Sum them into the recipe TOTAL. Cooking drives off water, not calories — never scale
   the total down for cooking losses.
4. SERVINGS: if the recipe states a number, use it exactly. If it does not, estimate the
   yield from the total cooked weight and a normal adult portion for this kind of dish —
   a main course ~350-450 g, a side ~150 g, a soup ~350 ml, a cookie/bar ~30-40 g, a
   muffin ~80 g, a sauce/dressing serving ~30 g, a cocktail ~150 ml.

Return ONLY this JSON, no prose:
{"total": {"calories": n, "protein_g": n, "carbs_g": n, "fat_g": n, "fiber_g": n,
           "sugar_g": n, "sodium_mg": n},
 "total_weight_g": n,
 "servings": n,
 "servings_basis": "stated" | "estimated",
 "serving_description": "short, e.g. about 1.5 cups (400 g)",
 "assumptions": "one short sentence on the biggest assumptions"}

Rules: plain numbers only — no units, ranges or nulls inside "total". carbs_g is TOTAL
carbohydrate INCLUDING fiber. Salt counts: 1 tsp salt = 2300 mg sodium. An ingredient
listed "to taste" gets a small typical amount. Ingredients used for frying that are
mostly discarded (a pan of oil) still count the absorbed part, ~20% of it."""


async def estimate_recipe(recipe: dict, servings: Optional[float] = None) -> Optional[dict]:
    """Nutrition for one recipe, estimated from its ingredients BY WEIGHT.

    2026-09-22: "The recipes don\'t have pre-built macros/cals/nutrients, that was
    really the whole point. Based on the ingredients we should be able to estimate that
    based on weight/portion size."

    This is its own model call rather than the food parser: a recipe needs gram
    conversions for raw ingredients and, for the 16 of 41 household recipes that never say,
    a YIELD estimate. The first cut reused parse_food_description and refused (422) whenever
    servings were missing — which is every recipe the user actually tapped.

    `servings` overrides the recipe\'s own count (a correction the user made). The TOTAL is
    returned alongside the per-serving numbers so a later servings change just re-divides,
    with no second call.
    """
    from app.config import settings as app_settings
    from app.services import ai_health
    from app.services.ai_summaries import MODEL, _log_call, _text_of

    text = recipe.get("ingredient_text") or ingredient_text(recipe.get("ingredients") or [])
    if not text:
        return None
    if not app_settings.anthropic_api_key:
        ai_health.record_disabled("No ANTHROPIC_API_KEY configured.")
        return None

    stated = servings if servings and float(servings) > 0 else recipe.get("servings")
    serving_line = (f"The recipe states it makes {float(stated):g} servings."
                    if stated and float(stated) > 0 else
                    "The recipe does NOT say how many servings it makes — estimate the yield.")
    user_msg = (
        f"Recipe: {recipe['title']}\n"
        f"Kind: {recipe.get('category') or 'unknown'}\n"
        f"{serving_line}\n"
        f"Ingredients:\n- " + "\n- ".join(
            (recipe.get("ingredient_text") or text).split(", ")) + "\n"
    )

    import anthropic
    client = anthropic.AsyncAnthropic(api_key=app_settings.anthropic_api_key)
    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=1500,
            extra_body={"temperature": 0},
            system=[{"type": "text", "text": RECIPE_SYSTEM}],
            messages=[{"role": "user", "content": user_msg}],
        )
        if response.stop_reason == "max_tokens":
            raise ValueError("Recipe estimate hit max_tokens — the reply was cut off mid-JSON.")
        raw = _text_of(response)
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        data = json.loads(raw)
        await _log_call("recipe_estimate", response.usage.input_tokens, response.usage.output_tokens)
    except Exception as exc:
        ai_health.record_failure("recipe_estimate", exc)
        logger.error("Recipe estimate failed for %s: %s", recipe.get("title"), exc)
        return None

    total = data.get("total") or {}
    if not isinstance(total.get("calories"), (int, float)):
        logger.error("Recipe estimate for %s had no total calories", recipe.get("title"))
        return None
    n = data.get("servings")
    n = float(n) if isinstance(n, (int, float)) and float(n) > 0 else float(stated or 1)
    basis = "stated" if (stated and float(stated) > 0) else (
        "estimated" if data.get("servings_basis") != "stated" else "estimated")

    keys = ("calories", "protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g", "sodium_mg")
    tot = {k: (round(float(total[k]), 1) if isinstance(total.get(k), (int, float)) else None)
           for k in keys}
    per = {k: (None if tot[k] is None else round(tot[k] / n, 0 if k == "calories" else 1))
           for k in keys}
    return {
        "per_serving": per,
        "total": tot,
        "servings": round(n, 2),
        "servings_basis": basis,
        "serving_description": (data.get("serving_description") or "")[:120] or None,
        "assumptions": (data.get("assumptions") or "")[:400] or None,
        "total_weight_g": (round(float(data["total_weight_g"]))
                           if isinstance(data.get("total_weight_g"), (int, float)) else None),
        "meal_type": recipe.get("default_meal_type") or "dinner",
    }
