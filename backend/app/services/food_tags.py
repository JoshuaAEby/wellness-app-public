"""Food-group tagging, composition and the coverage tripwire.

WHY THIS EXISTS, AND WHY IT IS BUILT THIS WAY
---------------------------------------------
The question is "would I notice if my diet quietly got worse?". The obvious
implementation — keyword-match the text of each food entry, count the days that
mention a vegetable — was built and measured, and it does not answer that question.
It measures the LOGGING CHANNEL.

Between 2026-05 and 2026-06 saved-item reuse rose from 35% to 60% of entries. A
reused item carries a canonical name ("kirkland protein bar"), not an ingredient
list, so keyword coverage of leafy greens fell 77% -> 30% while nothing about the
diet changed. Three independent measures agree it was an artifact: freshly-typed
entries held flat, the per-entry vegetable mention rate was ~10x higher for typed
entries than for reused ones, and fiber per day was best in the very month the
regex called worst.

Tagging the ITEM rather than the text is what makes a tripwire safe here. An item
tag can be wrong, but it is wrong identically every time that item appears, which
turns a time-varying bias into a constant one. Constant bias moves a baseline; it
does not manufacture drift. Since the tripwire only reads drift against that same
baseline, it is structurally immune to the error that would otherwise have fired a
false alarm in June — and rising reuse now IMPROVES the metric, because more of the
log resolves to items of known composition.

Two guards preserved from that investigation:

  * `coverage()` reports `tagged_entry_pct`. If a meaningful share of the window
    resolves to no tag, the numbers are not trustworthy and the caller is told so
    rather than shown a confident figure.
  * A group falling to exactly zero is reported as `absent`, never as "you stopped
    eating this". Logged coffee went 13% -> 0% across the same period purely
    because caffeine moved to the intake logger. A hard zero means "no source seen"
    and may be a channel change; a partial decline is the shape a real dietary
    change takes.

Quantities are deliberately not modelled. "No vitamin-A source logged in 22 days"
is checkable against stored data; "47% of the vitamin A RDA" would require portion
mass, nutrient concentration and bioavailability, none of which are available here.
The groups imply the nutrient profile without inventing a number.

NOTE ON TIMESTAMPS: `food_log.timestamp` is naive **LOCAL** (unlike health_metrics,
workouts and sleep, which are naive UTC). Its local calendar day is therefore plain
`func.date(...)` / `substr(ts,1,10)` — do NOT apply timeutils.local_date_sql here.
"""
import json
import logging
import re
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FoodItemTag, FoodLog
from app.timeutils import local_today
from app.foodday import food_day, food_day_end, food_day_start

logger = logging.getLogger(__name__)

# Sonnet rather than the Haiku used by the food-description parser: splitting a
# composite dish's calories across its components is a judgement task, not a
# lookup, and the whole library re-tags for well under a dollar (~13.5K in /
# 18K out ≈ $0.21). Ongoing cost is a handful of new items a day.
MODEL = "claude-sonnet-5"

BATCH_SIZE = 20          # items per LLM request. Kept small because adaptive
                         # thinking shares the output budget with the answer:
                         # 40 items truncated mid-JSON at max_tokens=8000.
MAX_BATCHES_PER_RUN = 30 # ceiling so a backfill can't run away

# ── Taxonomy ─────────────────────────────────────────────────────────────────
#
# Three dimensions, each answering a different question:
#
#   `components` — calorie shares that DECOMPOSE an item and sum to 100. Drives the
#     composition view. This is the one that answers "what is my diet made of".
#   `groups`     — multi-label and selective: only nutrition-relevant memberships.
#     Drives the coverage tripwire.
#   `primary`    — single dominant label. Retained and still populated, but nothing
#     reads it: as a part-to-whole view it failed, because most food is composite
#     and a `mixed_dish` catch-all absorbed 71% of calories (see migration 0056).
#
# COMPONENT_ORDER below is also the RENDER order, and it is not arbitrary — it is
# the validated adjacency order of the categorical palette (`--food-*` in
# index.css). Reordering the display without revalidating the palette can put two
# colours that a colourblind reader cannot separate next to each other.
PRIMARY_CATS = {
    "dairy":          "Milk, cheese, yogurt, butter, cream.",
    "mixed_dish":     "Composite prepared food where no single ingredient dominates: pizza, sandwiches, burritos, casseroles, bagel-with-spread, salads-as-a-meal.",
    "vegetable":      "The item is primarily vegetable.",
    "grain":          "Bread, rice, pasta, cereal, oats, crackers, tortillas eaten on their own.",
    "fruit":          "The item is primarily fruit.",
    "sweet_snack":    "Desserts, candy, chocolate, ice cream, pastries, chips and other salty snack food.",
    "protein_animal": "Meat, poultry, fish, seafood, eggs eaten as the main item.",
    "other":          "Beverages, condiments, sauces, supplements, and anything that fits nothing above.",
}

# Coverage-tracked groups. Each names a real dietary source whose ABSENCE is worth
# noticing. Deliberately not a nutrient list — see module docstring.
GROUPS = {
    "leafy_green":    "Spinach, kale, chard, broccoli, lettuce, arugula, cabbage, brussels sprouts, asparagus, green beans.",
    "orange_red_veg": "Carrot, sweet potato, pumpkin, winter squash, red/orange bell pepper, tomato. (Carotenoid / vitamin-A sources.)",
    "other_veg":      "Any other vegetable: onion, mushroom, cucumber, celery, courgette, peas, corn.",
    "fruit":          "Any fruit, fresh, frozen or dried. Not fruit juice, not fruit-flavoured.",
    "legumes":        "Beans, lentils, chickpeas, hummus, tofu, tempeh, edamame, peanuts.",
    "nuts_seeds":     "Tree nuts, seeds, nut butters (peanut butter counts here and in legumes).",
    "whole_grain":    "Whole grains: oats, brown rice, quinoa, whole wheat, barley, buckwheat. Not refined white flour or white rice.",
    "fish_seafood":   "Fish and shellfish of any kind. (Omega-3 / iodine sources.)",
    "fermented":      "Live-cultured foods: yogurt, kefir, kimchi, sauerkraut, miso, live-culture cheese.",
}

# Calorie-share components. Unlike PRIMARY_CATS these DECOMPOSE an item rather
# than labelling it, so vegetables inside a burrito are counted as vegetables and
# no catch-all bucket can absorb the diet (see migration 0056).
#
# ORDER IS THE VALIDATED PALETTE ORDER — it is the sequence that clears
# adjacent-pair colour-vision separation on the card surface. `other` is a
# neutral, deliberately not a 9th hue.
COMPONENTS = {
    "dairy":          "Milk, cheese, yogurt, cream, cream cheese. Butter counts here only when eaten as dairy rather than used as cooking fat.",
    "grain":          "The starch backbone: flour, bread, bagel, pizza crust, rice, pasta, tortilla, oats, cereal, crackers, chips made from grain.",
    "vegetable":      "Every vegetable INCLUDING ones inside a composite dish: salad greens, tomato, onion, pepper, potato, corn, salsa vegetables.",
    "added_fat":      "Fat added rather than intrinsic: cooking oil, frying fat, butter as a spread, mayonnaise, salad dressing, queso's oil fraction.",
    "fruit":          "Whole or cut fruit, berries, dried fruit. NOT juice and NOT fruit flavouring.",
    "plant_protein":  "Beans, lentils, chickpeas, hummus, tofu, tempeh, edamame, nuts, seeds, nut butters, isolated plant protein.",
    "added_sugar":    "Added sugars: sugar, syrup, honey, chocolate, candy, the sweetened part of a dessert or protein bar. NOT the sugar naturally in fruit or milk.",
    "animal_protein": "Meat, poultry, fish, seafood, eggs.",
    "other":          "Alcohol, coffee/tea, soda, broth, condiments, spices, and anything not covered above.",
}
COMPONENT_LABELS = {
    "dairy": "Dairy",
    "grain": "Grain",
    "vegetable": "Vegetable",
    "added_fat": "Added fat",
    "fruit": "Fruit",
    "plant_protein": "Plant protein",
    "added_sugar": "Added sugar",
    "animal_protein": "Animal protein",
    "other": "Other",
}

PRIMARY_ORDER = list(PRIMARY_CATS.keys())
GROUP_ORDER = list(GROUPS.keys())
COMPONENT_ORDER = list(COMPONENTS.keys())

# Human labels for the UI live server-side so the two views cannot drift apart.
PRIMARY_LABELS = {
    "dairy": "Dairy",
    "mixed_dish": "Mixed dish",
    "vegetable": "Vegetable",
    "grain": "Grain",
    "fruit": "Fruit",
    "sweet_snack": "Sweets & snacks",
    "protein_animal": "Animal protein",
    "other": "Other",
}
GROUP_LABELS = {
    "leafy_green": "Leafy greens",
    "orange_red_veg": "Orange / red veg",
    "other_veg": "Other vegetables",
    "fruit": "Fruit",
    "legumes": "Legumes",
    "nuts_seeds": "Nuts & seeds",
    "whole_grain": "Whole grains",
    "fish_seafood": "Fish & seafood",
    "fermented": "Fermented",
}


# ── Item identity ────────────────────────────────────────────────────────────

_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


def item_key(name: str | None, description: str | None, brand: str | None = None) -> str:
    """Canonical key for a food item.

    The AI-generated short `name` is the canonical handle when present; it is what
    makes two loggings of the same food resolve to one tag. `description` is only a
    fallback for rows predating the name column.

    `brand` (0070, note 60) is PREFIXED, so "Cream cheese" + "Kirkland" keys to exactly
    what "Kirkland Cream cheese" always did. That is the whole trick that let brands move
    out of names without orphaning a tag, My Food link or dismissal — and it keeps a
    branded food distinct from the generic one. No brand = byte-identical to before.

    Must stay deterministic and must not change once tags exist: altering it
    silently orphans every stored tag.
    """
    raw = (name or description or "").lower().strip()
    if brand and brand.strip() and raw:
        raw = f"{brand.strip().lower()} {raw}"
    raw = _PUNCT.sub(" ", raw)
    raw = _WS.sub(" ", raw).strip()
    return raw[:80]


def _parse_groups(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return [g for g in v if g in GROUPS] if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


# ── Tagging ──────────────────────────────────────────────────────────────────

def _build_prompt() -> str:
    prim = "\n".join(f"  {k}: {v}" for k, v in PRIMARY_CATS.items())
    grp = "\n".join(f"  {k}: {v}" for k, v in GROUPS.items())
    comp = "\n".join(f"  {k}: {v}" for k, v in COMPONENTS.items())
    return f"""You classify food items for a personal nutrition tracker.

For each item you receive, return three things:

1. "components" — split the item's CALORIES across the components below, as whole
   percentages that sum to exactly 100. This is the important one. Decompose the
   dish: a cheese pizza is mostly grain and dairy, not one indivisible thing.
2. "groups" — every nutrition group the item contains a meaningful amount of.
3. "primary" — the single category the item mostly is.

COMPONENTS (percentages of the item's calories, must total 100):
{comp}

GROUPS (choose all that apply, or none):
{grp}

PRIMARY CATEGORIES (choose exactly one):
{prim}

Rules for components:
- Think in CALORIES, not volume or weight. Fat is ~9 kcal/g against ~4 for protein
  and carbohydrate, so a modest amount of oil, butter or cheese carries far more of
  the calories than its size suggests. A salad with dressing is mostly added_fat by
  calories even though it looks like vegetables.
- Vegetables inside a composite dish DO count as vegetable. That is the entire point.
- Only count what the dish actually contains. Do not invent a vegetable share for a
  plain cheese pizza; do add one for a supreme or a burrito with salsa and peppers.
- Use "other" for alcohol and drinks. A beer is 100 other; a latte is mostly dairy.
- Round to whole numbers and make them total exactly 100.

Rules for groups:
- Include a group only if the item contains a real culinary portion of it. A
  sandwich's single lettuce leaf or a burger's tomato slice does NOT count; a salad's
  bed of greens does.
- Do not infer ingredients that are not stated or implied by the dish name.
- Refined grains get NO "whole_grain" group. Only genuinely whole-grain items do.
- Fruit juice, fruit-flavoured items and fruit snacks are NOT the "fruit" group.

Be consistent: the same food must always get the same answer.

Worked examples:
  "Parmesan bagel with cream cheese"
    -> components {{"grain": 65, "dairy": 30, "added_fat": 5}}, groups [], primary "mixed_dish"
  "Kirkland frozen cheese pizza"
    -> components {{"grain": 45, "dairy": 33, "added_fat": 17, "vegetable": 5}}, groups [], primary "mixed_dish"
  "Chicken burrito with rice, cheese, tomatoes and pinto beans"
    -> components {{"grain": 35, "animal_protein": 22, "plant_protein": 18, "dairy": 15, "vegetable": 10}},
       groups ["legumes", "orange_red_veg"], primary "mixed_dish"
  "Lager beer"
    -> components {{"other": 100}}, groups [], primary "other"

Return ONLY a JSON array, one object per input item, in the SAME ORDER, each shaped:
{{"i": <index>, "components": {{"<component>": <pct>, ...}}, "groups": ["<group>", ...], "primary": "<category>"}}
No prose, no markdown fences."""


async def _tag_batch(items: list[tuple[str, str]]) -> dict[str, dict]:
    """Classify one batch. `items` is [(item_key, display_name)].

    Returns {item_key: {"primary": str, "groups": [str]}} for items the model
    classified. Missing keys mean "not classified" and are left untagged so a later
    run retries them — a wrong tag is worse than a missing one, because a wrong tag
    is silently baked into every past and future appearance of that food.
    """
    from app.config import settings as app_settings

    if not app_settings.anthropic_api_key:
        logger.info("Food tagging: ANTHROPIC_API_KEY not set, skipping")
        return {}

    import anthropic
    client = anthropic.AsyncAnthropic(api_key=app_settings.anthropic_api_key)

    listing = "\n".join(f'{i}. {name}' for i, (_, name) in enumerate(items))

    try:
        # No `temperature`: sampling parameters were REMOVED on Sonnet 5 (and the
        # Opus 5 / 4.7 / 4.8 family) and are rejected with a 400. Consistency across
        # runs comes from the prompt instead — and matters less than it looks, since
        # each item is decomposed once and then stored.
        # `thinking` is omitted, which on this model means adaptive: worth having,
        # because apportioning a dish's calories is reasoning, not lookup.
        response = await client.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=[{"type": "text", "text": _build_prompt()}],
            messages=[{"role": "user", "content": f"Items:\n{listing}"}],
        )
        if response.stop_reason == "max_tokens":
            # Would otherwise surface as an unterminated-JSON parse error several
            # frames away from the real cause.
            logger.error("Food tag batch hit max_tokens (%d items) — lower BATCH_SIZE",
                         len(items))
            return {}
        # With adaptive thinking on, content[0] is a ThinkingBlock — take the text
        # block by type rather than by position.
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else text
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        parsed = json.loads(text)

        try:
            from app.services.ai_summaries import _log_call
            # `model=` is NOT optional: _log_call defaults to ai_summaries.MODEL
            # (Haiku), so omitting it stamped every food_tag row in api_call_log with
            # the wrong model AND priced it at Haiku rates while the call actually ran
            # on claude-sonnet-5. Same trap already documented at the blood_parse_pdf
            # call site. Fixed 2026-09-08.
            await _log_call("food_tag", response.usage.input_tokens,
                            response.usage.output_tokens, model=MODEL)
        except Exception:      # cost logging must never break tagging
            logger.debug("food_tag cost logging failed", exc_info=True)

    except Exception as exc:
        logger.error("Food tag batch failed: %s", exc)
        return {}

    out: dict[str, dict] = {}
    for row in parsed if isinstance(parsed, list) else []:
        try:
            idx = int(row["i"])
            primary = row.get("primary")
            groups = row.get("groups") or []
            comps = row.get("components") or {}
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= idx < len(items)):
            continue
        comps = _clean_components(comps)
        if comps is None:
            continue                      # unusable split -> leave untagged, retry later
        groups = [g for g in groups if g in GROUPS]
        out[items[idx][0]] = {
            "primary": primary if primary in PRIMARY_CATS else None,
            "groups": groups,
            "components": comps,
        }
    return out


def _clean_components(raw) -> dict[str, float] | None:
    """Validate and renormalise a component split, or None if it is unusable.

    The model is asked for whole percentages totalling 100 and mostly complies, but
    a split that totals 97 or 103 is a rounding slip, not a misunderstanding —
    renormalising is right there. A split that is empty, negative, or wildly off is
    rejected outright so the item stays untagged and gets retried: a fabricated
    decomposition would be silently baked into every appearance of that food.
    """
    if not isinstance(raw, dict):
        return None
    out: dict[str, float] = {}
    for k, v in raw.items():
        if k not in COMPONENTS:
            return None                   # unknown component -> don't guess
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if f < 0:
            return None
        if f > 0:
            out[k] = f
    total = sum(out.values())
    if not out or not (80.0 <= total <= 120.0):
        return None
    return {k: round(v * 100.0 / total, 2) for k, v in out.items()}


async def untagged_items(db: AsyncSession, limit: int | None = None) -> list[tuple[str, str]]:
    """Distinct logged items still needing a tag, most-frequent first.

    "Needing a tag" means **no component split** — not merely no row. Migration 0056
    added `components` as NULL to every row written by 0055, so changing what the
    tagger is asked automatically re-queues the whole library without a separate
    migration or a destructive delete. A hand-corrected row (`source='manual'`) is
    never re-queued.

    Frequency order matters: it is what makes a partial backfill useful straight
    away. 27 items cover half of all logged entries.
    """
    rows = (await db.execute(
        select(FoodLog.name, FoodLog.description, FoodLog.brand)
        .where(FoodLog.skipped.is_(False) | FoodLog.skipped.is_(None))
    )).all()

    counts: dict[str, int] = defaultdict(int)
    display: dict[str, str] = {}
    for name, desc, brand in rows:
        k = item_key(name, desc, brand)
        if not k:
            continue
        counts[k] += 1
        if k not in display:
            display[k] = ((f"{brand} " if brand else "") + (name or desc or ""))[:120]

    done = {
        r[0] for r in (await db.execute(
            select(FoodItemTag.item_key)
            .where((FoodItemTag.components.isnot(None)) | (FoodItemTag.source == "manual"))
        )).all()
    }
    pending = [(k, display[k]) for k in counts if k not in done]
    pending.sort(key=lambda kv: -counts[kv[0]])
    return pending[:limit] if limit else pending


async def run_tagging(db: AsyncSession, limit: int | None = None) -> dict:
    """Tag/re-tag every item lacking a component split. Safe to re-run."""
    pending = await untagged_items(db, limit)
    if not pending:
        return {"pending": 0, "tagged": 0, "failed": 0}

    from datetime import datetime
    existing = {
        r.item_key: r for r in (await db.execute(select(FoodItemTag))).scalars().all()
    }

    tagged = failed = 0
    batches = [pending[i:i + BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]
    for batch in batches[:MAX_BATCHES_PER_RUN]:
        result = await _tag_batch(batch)
        now = datetime.utcnow()
        for key, name in batch:
            got = result.get(key)
            if got is None:
                failed += 1
                continue
            row = existing.get(key)
            if row is None:
                row = FoodItemTag(item_key=key, created_at=now)
                db.add(row)
                existing[key] = row
            row.display_name = name
            row.primary_cat = got["primary"]
            row.groups = json.dumps(got["groups"])
            row.components = json.dumps(got["components"])
            row.source = "llm"
            row.model = MODEL
            row.failed = False
            row.updated_at = now
            tagged += 1
        await db.commit()

    logger.info("Food tagging: %d tagged, %d unclassified of %d pending",
                tagged, failed, len(pending))
    return {"pending": len(pending), "tagged": tagged, "failed": failed}


def _parse_components(raw: str | None) -> dict[str, float]:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(v, dict):
        return {}
    return {k: float(x) for k, x in v.items() if k in COMPONENTS}


async def tag_map(db: AsyncSession) -> dict[str, dict]:
    rows = (await db.execute(
        select(FoodItemTag.item_key, FoodItemTag.primary_cat,
               FoodItemTag.groups, FoodItemTag.components)
    )).all()
    return {
        r[0]: {"primary": r[1], "groups": _parse_groups(r[2]),
               "components": _parse_components(r[3])}
        for r in rows
    }


# ── Reading the log ──────────────────────────────────────────────────────────

async def _entries(db: AsyncSession, since: date,
                   until: date | None = None) -> list[tuple[str, str, float]]:
    """(local_day, item_key, calories) for real (non-skipped) entries in [since, until].

    `until` is INCLUSIVE of that calendar day, so the bound is the start of the
    following day — `food_log.timestamp` is naive local, and entries carry a real
    time of day.
    """
    q = (
        select(FoodLog.timestamp, FoodLog.name, FoodLog.description, FoodLog.calories,
               FoodLog.brand)
        .where(FoodLog.timestamp >= food_day_start(since))
        .where(FoodLog.skipped.is_(False) | FoodLog.skipped.is_(None))
    )
    if until is not None:
        q = q.where(FoodLog.timestamp < food_day_end(until))
    rows = (await db.execute(q)).all()
    out = []
    for ts, name, desc, kcal, brand in rows:
        k = item_key(name, desc, brand)
        if k:
            out.append((food_day(ts).isoformat(), k, float(kcal or 0.0)))
    return out


async def composition(db: AsyncSession, days: int = 90,
                      end: date | None = None) -> dict:
    """What the diet is actually MADE OF: calories split across base components.

    Every entry contributes to several components at once, in proportion to its own
    decomposition, so a burrito puts calories into grain, animal protein, legumes,
    dairy AND vegetable rather than into one bucket. That is the difference between
    "what is my diet made of" and the earlier single-label view, where a `mixed_dish`
    catch-all absorbed 71% of calories and could never move.

    Only calorie-weighted. Entry share was dropped with the single-label design: an
    entry has no single category to be counted under any more, and "48% of entries
    were partly grain" is not a quantity anyone can act on.
    """
    end = end or local_today()
    since = end - timedelta(days=days)
    entries = await _entries(db, since, end)
    tags = await tag_map(db)

    by_kcal: dict[str, float] = defaultdict(float)
    untagged = 0
    untagged_kcal = tagged_kcal = total_kcal = 0.0

    for _day, key, kcal in entries:
        total_kcal += kcal
        t = tags.get(key)
        comps = t["components"] if t else None
        if not comps:
            untagged += 1
            untagged_kcal += kcal
            continue
        tagged_kcal += kcal
        for comp, share in comps.items():
            by_kcal[comp] += kcal * share / 100.0

    n_tagged = len(entries) - untagged
    slices = [
        {
            "key": comp,
            "label": COMPONENT_LABELS[comp],
            "kcal": round(by_kcal.get(comp, 0.0)),
            "kcal_pct": (round(100 * by_kcal.get(comp, 0.0) / tagged_kcal, 1)
                         if tagged_kcal > 0 else None),
        }
        for comp in COMPONENT_ORDER
    ]
    return {
        "days": days,
        "end": end.isoformat(),
        "entries_total": len(entries),
        "entries_tagged": n_tagged,
        # The share of the window this view can actually speak for. Low values mean
        # the breakdown is partial and should be shown as such.
        "tagged_entry_pct": round(100 * n_tagged / len(entries), 1) if entries else None,
        "kcal_total": round(total_kcal),
        "kcal_tagged": round(tagged_kcal),
        "slices": [s for s in slices if s["kcal"]],
    }


async def composition_trend(db: AsyncSession, weeks: int = 16) -> dict:
    """Weekly composition — the change-over-time view a single pie cannot give.

    Shares, not absolute counts: weeks differ in how many days were logged, and an
    unlogged week would otherwise read as a dietary change.
    """
    since = local_today() - timedelta(weeks=weeks)
    entries = await _entries(db, since)
    tags = await tag_map(db)

    buckets: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    totals: dict[str, float] = defaultdict(float)
    n_days: dict[str, set] = defaultdict(set)

    for day, key, kcal in entries:
        d = date.fromisoformat(day)
        wk = (d - timedelta(days=d.weekday())).isoformat()   # Monday of that week
        n_days[wk].add(day)
        t = tags.get(key)
        comps = t["components"] if t else None
        if not comps:
            continue
        for comp, share in comps.items():
            buckets[wk][comp] += kcal * share / 100.0
        totals[wk] += kcal

    rows = []
    for wk in sorted(buckets):
        if totals[wk] <= 0 or len(n_days[wk]) < 3:   # <3 logged days is not a week
            continue
        row = {"week": wk, "days": len(n_days[wk])}
        for comp in COMPONENT_ORDER:
            row[comp] = round(100 * buckets[wk].get(comp, 0.0) / totals[wk], 1)
        rows.append(row)
    return {"weeks": rows, "order": COMPONENT_ORDER, "labels": COMPONENT_LABELS}


# ── Coverage and the tripwire ────────────────────────────────────────────────

# A group must clear BOTH gates to be called a decline: statistically separable
# from the baseline, and large enough to matter. With ~30 days at p≈0.5 the
# standard error on a proportion is ~9 points, so single-week wobble stays quiet.
Z_THRESHOLD = -2.0
MIN_ABSOLUTE_DROP = 0.15
# Below this the baseline is too thin to be a baseline. A tripwire with no
# reference cannot distinguish "changed" from "always was".
MIN_BASELINE_DAYS = 21
# Minimum times a group must actually appear in the baseline before its absence
# means anything. A proportion computed from three sightings is not a habit that
# can lapse, and firing on one would spend the alarm budget on noise. Same reason
# correlations.py gates on MIN_SUPPORT_OBS.
MIN_GROUP_SUPPORT = 4
RECENT_DAYS = 30
BASELINE_DAYS = 90
# The metric stops being trustworthy when too much of the window is unclassified.
MIN_TAGGED_PCT = 70.0


def _two_prop_z(k1: int, n1: int, k2: int, n2: int) -> float | None:
    """z for (recent proportion - baseline proportion). Negative means a decline."""
    if n1 < 5 or n2 < 5:
        return None
    p1, p2 = k1 / n1, k2 / n2
    pool = (k1 + k2) / (n1 + n2)
    if pool <= 0 or pool >= 1:
        return None
    se = (pool * (1 - pool) * (1 / n1 + 1 / n2)) ** 0.5
    return (p1 - p2) / se if se > 0 else None


async def coverage(db: AsyncSession,
                   recent_days: int = RECENT_DAYS,
                   baseline_days: int = BASELINE_DAYS) -> dict:
    """Per-group coverage, gap and drift status — the tripwire.

    Reads DRIFT against the user's own history, never an RDA: the reference is what
    this person normally eats, which sidesteps every accuracy problem in estimating
    how much of a nutrient a portion contained.

    Levels are reported alongside deltas on purpose. A tripwire is blind to anything
    that never changed — a group absent for the entire record produces no delta at
    all — so `recent_pct` is returned for every group whether or not it fired.
    """
    today = local_today()
    recent_start = today - timedelta(days=recent_days)
    baseline_start = recent_start - timedelta(days=baseline_days)

    entries = await _entries(db, baseline_start)
    tags = await tag_map(db)

    recent_days_set: set[str] = set()
    baseline_days_set: set[str] = set()
    hit_recent: dict[str, set] = defaultdict(set)
    hit_baseline: dict[str, set] = defaultdict(set)
    n_entries = n_tagged = 0

    for day, key, _kcal in entries:
        n_entries += 1
        in_recent = day >= recent_start.isoformat()
        (recent_days_set if in_recent else baseline_days_set).add(day)
        t = tags.get(key)
        if t is None:
            continue
        n_tagged += 1
        for g in t["groups"]:
            (hit_recent if in_recent else hit_baseline)[g].add(day)

    # Last-seen needs the whole record, not just the baseline window: "22 days since
    # a fish source" and "never logged one" are different answers.
    all_rows = (await db.execute(
        select(FoodLog.timestamp, FoodLog.name, FoodLog.description, FoodLog.brand)
        .where(FoodLog.skipped.is_(False) | FoodLog.skipped.is_(None))
    )).all()
    last_seen: dict[str, str] = {}
    for ts, name, desc, brand in all_rows:
        t = tags.get(item_key(name, desc, brand))
        if t is None:
            continue
        d = food_day(ts).isoformat()
        for g in t["groups"]:
            if g not in last_seen or d > last_seen[g]:
                last_seen[g] = d

    n_recent, n_baseline = len(recent_days_set), len(baseline_days_set)
    tagged_pct = round(100 * n_tagged / n_entries, 1) if n_entries else None
    reliable = tagged_pct is not None and tagged_pct >= MIN_TAGGED_PCT

    groups, alerts = [], []
    for g in GROUP_ORDER:
        k_recent, k_baseline = len(hit_recent[g]), len(hit_baseline[g])
        recent_pct = round(100 * k_recent / n_recent, 1) if n_recent else None
        baseline_pct = round(100 * k_baseline / n_baseline, 1) if n_baseline else None
        seen = last_seen.get(g)
        gap = (today - date.fromisoformat(seen)).days if seen else None
        z = _two_prop_z(k_recent, n_recent, k_baseline, n_baseline)

        if not n_recent:
            status = "no_data"
        elif seen is None:
            status = "never"
        elif n_baseline < MIN_BASELINE_DAYS:
            status = "accruing"
        elif k_baseline < MIN_GROUP_SUPPORT:
            # Too rare in the baseline to have a habit that could lapse. Reported at
            # its true level rather than flagged, so a group that was always near
            # zero is visible without consuming the alarm budget.
            status = "sparse"
        elif k_recent == 0:
            # Deliberately NOT called a dietary change. Logged coffee went 13% -> 0%
            # across this same period only because caffeine moved to the intake
            # logger. A hard zero is "no source seen", which may be a channel change.
            status = "absent"
        elif (z is not None and z <= Z_THRESHOLD
              and (k_baseline / n_baseline - k_recent / n_recent) >= MIN_ABSOLUTE_DROP):
            status = "dropped"
        else:
            status = "ok"

        row = {
            "key": g,
            "label": GROUP_LABELS[g],
            "recent_pct": recent_pct,
            "baseline_pct": baseline_pct,
            # Raw counts travel with the percentages: 10% of days means something
            # different at 9 sightings than at 40, and the UI must be able to say so.
            "recent_days_with": k_recent,
            "baseline_days_with": k_baseline,
            "days_since_last": gap,
            "last_seen": seen,
            "z": round(z, 2) if z is not None else None,
            "status": status,
        }
        groups.append(row)
        # Only a trustworthy window may raise an alarm. A false alarm in a system
        # whose value is silence costs more than a missed one.
        if reliable and status in ("dropped", "absent", "never"):
            alerts.append(row)

    return {
        "recent_days": recent_days,
        "baseline_days": baseline_days,
        "logged_days_recent": n_recent,
        "logged_days_baseline": n_baseline,
        "tagged_entry_pct": tagged_pct,
        "reliable": reliable,
        "min_tagged_pct": MIN_TAGGED_PCT,
        "groups": groups,
        "alerts": alerts,
    }
