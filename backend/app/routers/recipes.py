"""My Recipes — household-app recipes with per-serving nutrition (note 37).

Read side is the household DB (read-only, services/recipes); everything written lives in
`recipe_nutrition`. Kept OUT of /api/my-foods deliberately: recipes are their own section
so they don't crowd the foods the user eats every day.
"""
import json
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import FoodLog, RecipeNutrition
from app.services import recipes as rs

router = APIRouter(prefix="/api/recipes", tags=["recipes"])

MACROS = ("calories", "protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g", "sodium_mg")


def _ser(r: dict, n: Optional[RecipeNutrition], uses: int = 0) -> dict:
    out = {**r, "uses": uses, "nutrition": None}
    if n is not None:
        out["nutrition"] = {
            "servings": n.servings, **{m: getattr(n, m) for m in MACROS},
            "meal_type": n.meal_type, "source": n.source, "notes": n.notes,
            "hidden": bool(n.hidden),
            # What the estimate assumed (0069), so the numbers can be judged.
            "servings_basis": n.servings_basis,
            "serving_description": n.serving_description,
            "assumptions": n.assumptions,
            "total_weight_g": n.total_weight_g,
            "total": json.loads(n.total_json) if n.total_json else None,
            # The recipe changed in the household app since this estimate was made.
            "stale": bool(n.source_hash and n.source_hash != r["source_hash"]),
            "updated_at": n.updated_at.isoformat() if n.updated_at else None,
        }
    return out


@router.get("")
async def list_recipes(
    include_hidden: bool = False,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Every household recipe merged with what we know about its nutrition.

    `available: false` (rather than an error) when the household DB is not mounted, so the
    UI can explain itself instead of showing an empty list that looks like a bug.
    """
    if not rs.available():
        return {"available": False, "recipes": [],
                "detail": "The household app's database is not mounted read-only at "
                          f"{rs.settings.household_db_path}."}
    stored = {n.recipe_id: n for n in
              (await db.execute(select(RecipeNutrition))).scalars().all()}
    uses: dict[int, int] = {}
    for rid, in (await db.execute(
        select(FoodLog.recipe_id).where(FoodLog.recipe_id.isnot(None))
    )).all():
        uses[rid] = uses.get(rid, 0) + 1
    out = []
    for r in rs.list_recipes():
        n = stored.get(r["recipe_id"])
        if n is not None and n.hidden and not include_hidden:
            continue
        out.append(_ser(r, n, uses.get(r["recipe_id"], 0)))
    return {"available": True, "recipes": out}


@router.post("/{recipe_id}/estimate")
async def estimate(
    recipe_id: int,
    servings: Optional[float] = None,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Estimate (or re-estimate) one recipe. Costs one model call, so it is explicit."""
    r = rs.get_recipe(recipe_id)
    if r is None:
        raise HTTPException(status_code=404, detail="recipe not found")
    row = (await db.execute(
        select(RecipeNutrition).where(RecipeNutrition.recipe_id == recipe_id)
    )).scalar_one_or_none()
    # NO SERVINGS IS NOT A REFUSAL (fixed 2026-09-22). 16 of 41 household recipes never
    # say how many servings they make, and refusing them with a 422 was every recipe the user
    # tapped. The estimator works from ingredient WEIGHTS and estimates the yield itself;
    # a stored servings correction still overrides it.
    n_serv = servings or (row.servings if row is not None else None) or r["servings"]
    est = await rs.estimate_recipe(r, float(n_serv) if n_serv else None)
    if est is None:
        raise HTTPException(status_code=502, detail="Could not estimate this recipe's nutrition.")
    if row is None:
        row = RecipeNutrition(recipe_id=recipe_id, created_at=datetime.utcnow())
        db.add(row)
    row.title = r["title"]
    row.servings = est["servings"]
    for m in MACROS:
        setattr(row, m, est["per_serving"][m])
    row.meal_type = row.meal_type or est["meal_type"]
    row.total_json = json.dumps(est["total"])
    row.total_weight_g = est["total_weight_g"]
    row.servings_basis = est["servings_basis"]
    row.serving_description = est["serving_description"]
    row.assumptions = est["assumptions"]
    # A re-estimate is an estimate again: it describes the CURRENT recipe, and the
    # confirmation applied to the numbers it replaced.
    row.source = "estimate"
    row.source_hash = r["source_hash"]
    row.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(row)
    return _ser(r, row)


class RecipePatch(BaseModel):
    servings: Optional[float] = Field(default=None, gt=0)
    calories: Optional[float] = Field(default=None, ge=0)
    protein_g: Optional[float] = Field(default=None, ge=0)
    carbs_g: Optional[float] = Field(default=None, ge=0)
    fat_g: Optional[float] = Field(default=None, ge=0)
    fiber_g: Optional[float] = Field(default=None, ge=0)
    sugar_g: Optional[float] = Field(default=None, ge=0)
    sodium_mg: Optional[float] = Field(default=None, ge=0)
    meal_type: Optional[str] = None
    notes: Optional[str] = None
    hidden: Optional[bool] = None
    source: Optional[Literal["estimate", "confirmed"]] = None


@router.put("/{recipe_id}")
async def update_recipe_nutrition(
    recipe_id: int,
    body: RecipePatch,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Correct or confirm the numbers, set servings, or hide a recipe you don't eat.

    Writes ONLY to recipe_nutrition — the household app's recipe is never touched.
    """
    r = rs.get_recipe(recipe_id)
    if r is None:
        raise HTTPException(status_code=404, detail="recipe not found")
    row = (await db.execute(
        select(RecipeNutrition).where(RecipeNutrition.recipe_id == recipe_id)
    )).scalar_one_or_none()
    if row is None:
        row = RecipeNutrition(recipe_id=recipe_id, title=r["title"], created_at=datetime.utcnow())
        db.add(row)
    data = body.model_dump(exclude_unset=True)
    # Editing the numbers by hand IS the confirmation, unless told otherwise.
    touched_macros = any(m in data for m in MACROS) or "servings" in data
    for k, v in data.items():
        setattr(row, k, v)
    # A SERVINGS CORRECTION RE-DIVIDES the stored whole-recipe totals (0069) — the model
    # estimated the recipe, not the portion, so changing the yield needs no second call.
    if "servings" in data and not any(m in data for m in MACROS) and row.total_json:
        total = json.loads(row.total_json)
        n = float(data["servings"] or 0) or 1.0
        for m in MACROS:
            t = total.get(m)
            setattr(row, m, None if t is None else round(float(t) / n, 0 if m == "calories" else 1))
        row.servings_basis = "user"
    if touched_macros and "source" not in data:
        row.source = "confirmed"
    if row.source == "confirmed":
        row.source_hash = r["source_hash"]
    row.title = r["title"]
    row.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(row)
    return _ser(r, row)
