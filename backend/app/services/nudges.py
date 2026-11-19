"""Actionable suggestions for the top of Today.

"Eat more protein" is not advice — it restates the gap. This builds suggestions
that name a specific food, from the user's OWN rotation, sized to the gap that
actually remains right now:

    50 g of protein to go
      · 90 of 140 g logged
      · Protein bar · 20 g · 190 kcal
      · Greek yogurt, 150 g · 15 g · 130 kcal

## Shape: title + short lines, never a paragraph

Each nudge is a `title` plus `lines`, a list of at most four fragments. This is a
glanceable card at the top of a screen, not something to read — prose forces you to
parse a sentence to recover three numbers that were already structured. Fragments
also keep the food suggestions scannable as a comparison: same fields, same order,
one per line. Keep them terse and do not end them with full stops.

## Rule-based on purpose

No model call. The gap is arithmetic and the food catalogue is the user's own
history, so a template filled from real data is more specific than a generated
sentence and cannot invent a food they don't eat. It's also free and instant,
which matters for something rendered on every Today view.

Variety comes from three places rather than from sampling: the gap itself moves
through the day, the eligible food set changes with the remaining calorie budget,
and equally-good suggestions are rotated by day-of-year so the same one doesn't
appear every morning.

## Not the coach

Deliberately a separate surface from `services/coach`. The coach drives the
randomized experiment arms and is blinded by design; hanging nutrition nudges off
it would entangle a behavioural nudge with an experimental assignment.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Below these, the gap isn't worth a card — closing it is a rounding error.
MIN_PROTEIN_GAP_G = 15
MIN_FIBER_GAP_G = 6
# Before this hour there's a whole day left; a "you're behind" nudge is just noise.
NUDGE_FROM_HOUR = 11
# A food has to have been eaten this often to count as "yours".
MIN_USES_FOR_SUGGESTION = 2
# A pick must close at least this share of the gap. Density ranking alone will happily
# offer broccoli (3 g) against a 60 g protein gap — technically the best grams-per-kcal
# available, but 2% of the need is not a suggestion.
MIN_PICK_SHARE = 0.10
# Near-duplicate detection: the same product logged under two names ("Olipop Prebiotic
# Soda" / "Ollipop Probiotic Drink", "Chipotle bowl with chicken" / "Chipotle chicken
# bowl"). Suggesting both wastes a line and reads as a bug.
#
# Name similarity ALONE cannot do this — the classes overlap in this data. "Chipotle
# bowl with chicken"/"Chipotle chicken bowl" are the same product at ratio 0.68, while
# "IQ Protein Bar"/"Kirkland protein bar" are different products at 0.77. Any threshold
# that catches the first accepts the second.
#
# Macros break the tie, because a duplicate name describes the same food: the Chipotle
# pair is identical (645 kcal / 38 g / 11 g), whereas the two bars differ 12 g vs 22.7 g
# of protein. So require the names to rhyme AND the nutrient under discussion to match.
SAME_NAME_RATIO = 0.60
SAME_NUTRIENT_TOL = 0.15


def _norm_name(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum() or ch == " ").strip()


def _same_product(a: dict, b: dict, nutrient: str) -> bool:
    from difflib import SequenceMatcher
    if SequenceMatcher(None, _norm_name(a["name"]), _norm_name(b["name"])).ratio() < SAME_NAME_RATIO:
        return False
    va, vb = a.get(nutrient) or 0.0, b.get(nutrient) or 0.0
    hi = max(va, vb)
    if hi <= 0:
        return True
    return abs(va - vb) / hi <= SAME_NUTRIENT_TOL


def _fmt_g(x: float) -> str:
    return f"{x:.0f}" if abs(x - round(x)) < 0.05 else f"{x:.1f}"


async def _food_rotation(db: AsyncSession) -> list[dict]:
    """Foods the user actually eats, with per-serving macros.

    Grouped by the parser's canonical `name` (same key the meal picker uses) and
    summarised by the MEDIAN serving rather than the mean, so one 5-oz-bourbon
    outlier doesn't define what "a serving" means.
    """
    from app.models import FoodLog
    # Only the five columns used, as plain rows: building ~1,300 full ORM objects was
    # most of /nutrition/nudges' time (2026-10-21). ORDER BY id pins the scan order the
    # unordered full-row query got implicitly (rowid), which `rs[-1].meal_type` relies on.
    rows = (await db.execute(
        select(FoodLog.name, FoodLog.calories, FoodLog.protein_g, FoodLog.fiber_g, FoodLog.meal_type)
        .where((FoodLog.skipped == False) | (FoodLog.skipped.is_(None)))  # noqa: E712
        .order_by(FoodLog.id)
    )).all()

    groups: dict[str, list] = {}
    for r in rows:
        key = (r.name or "").strip()
        if not key or r.calories is None:
            continue
        groups.setdefault(key, []).append(r)

    def med(vals):
        vals = sorted(v for v in vals if v is not None)
        if not vals:
            return 0.0
        n = len(vals)
        return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2

    out = []
    for name, rs in groups.items():
        if len(rs) < MIN_USES_FOR_SUGGESTION:
            continue
        out.append({
            "name": name,
            "uses": len(rs),
            "kcal": med([r.calories for r in rs]),
            "protein": med([r.protein_g for r in rs]),
            "fiber": med([r.fiber_g for r in rs]),
            "meal_type": rs[-1].meal_type or "snack",
        })
    return out


def _pick_foods(rotation: list[dict], nutrient: str, need: float,
                kcal_budget: Optional[float], seed: int, limit: int = 2) -> list[dict]:
    """Foods from the rotation that best close `need`, within the calorie budget.

    Ranked by nutrient DENSITY (grams per kcal), not absolute grams — in a deficit
    the scarce resource is calories, so the useful suggestion is the one that
    closes the most gap per calorie spent.
    """
    # Must make a material dent, not just rank well on density. No fallback to
    # sub-threshold items: naming a 3 g food against a 60 g gap is worse than the
    # generic advice the caller falls back to.
    floor = need * MIN_PICK_SHARE
    cands = [f for f in rotation if (f.get(nutrient) or 0) >= floor]
    # A budget only constrains when there IS one. Over target, kcal_budget goes
    # negative and every candidate would be rejected — silently degrading exactly
    # when a lean, dense option is the most useful thing to name.
    constrained = kcal_budget is not None and kcal_budget > 0
    if constrained:
        cands = [f for f in cands if f["kcal"] <= kcal_budget]
    if not cands:
        return []
    cands.sort(key=lambda f: (-(f[nutrient] / max(f["kcal"], 1)), -f["uses"]))

    # Rotate within the top band so the same item isn't proposed every single day.
    top = cands[:max(limit + 3, 4)]
    if len(top) > limit:
        offset = seed % len(top)
        top = top[offset:] + top[:offset]

    chosen, running, spent = [], 0.0, 0.0
    for f in top:
        if len(chosen) >= limit or running >= need:
            break
        if constrained and spent + f["kcal"] > kcal_budget:
            continue
        if any(_same_product(f, c, nutrient) for c in chosen):
            continue
        chosen.append(f)
        running += f[nutrient]
        spent += f["kcal"]
    return chosen


def _food_lines(foods: list[dict], nutrient: str) -> list[str]:
    """One line per food: name · nutrient · calories.

    Same fields in the same order every time so two suggestions read as a
    comparison rather than as two sentences.
    """
    return [f"{f['name']} · {_fmt_g(f[nutrient])} g · {f['kcal']:,.0f} kcal"
            for f in foods]


async def build_nudges(db: AsyncSession, now: Optional[datetime] = None) -> list[dict]:
    """Ranked suggestions. Highest priority first; caller renders the top few."""
    from app.config import settings as app_settings
    from app.models import FoodLog, UserSettings
    from app.foodday import food_day, food_day_end, food_day_start
    from app.timeutils import local_day_bounds_utc, to_local

    now = now or datetime.utcnow()
    # The FOOD day (04:00 boundary, app/foodday.py): at 01:00 "today's food" is still
    # the day that just ended. (This used local_day_bounds_utc on local timestamps —
    # an accidental 04:00/05:00 boundary that moved with DST.)
    today = food_day(to_local(now).replace(tzinfo=None))
    # `now` is naive UTC (the storage convention). Reading .hour off it would run
    # every time-of-day rule on UTC — 4-5 hours early in America/New_York, so the
    # "prioritise at dinner" wording would appear over lunch.
    local_now = to_local(now)
    hour = local_now.hour
    nudges: list[dict] = []

    st = (await db.execute(select(UserSettings))).scalars().first()
    day_start, day_end = local_day_bounds_utc(today)     # UTC — the Hevy rule below uses it
    todays = (await db.execute(
        select(FoodLog)
        .where(FoodLog.timestamp >= food_day_start(today)).where(FoodLog.timestamp < food_day_end(today))
        .where((FoodLog.skipped == False) | (FoodLog.skipped.is_(None)))  # noqa: E712
    )).scalars().all()

    kcal = sum(r.calories or 0 for r in todays)
    protein = sum(r.protein_g or 0 for r in todays)
    fiber = sum(r.fiber_g or 0 for r in todays)

    # ── Goals (ONE derivation, shared — see services/nutrition_goals) ────────
    # Was an inline copy that read `HealthMetric.value` RAW, with no unit conversion.
    # It only ever worked because every weight row in this database is stored in `lb`;
    # a single kg reading would have quietly asked for a 76 g protein goal.
    from app.services.nutrition_goals import resolve_protein_goal
    protein_goal = await resolve_protein_goal(db, st)

    # Must go through effective_calorie_goal: reading st.calorie_goal directly ignores
    # calorie_goal_mode, so in pace/balance mode the card judged the day against the
    # stored fixed number while the calorie ring used the dynamic goal — the two
    # disagreed about whether the day was over target.
    from app.services.energy import effective_calorie_goal

    goal_info = await effective_calorie_goal(db, st, today, hour)
    calorie_goal = goal_info["goal"]
    # Derive the fiber target from the STABLE stored calorie number, never the dynamic
    # goal: 14 g per 1,000 kcal against an intraday projection makes the target drift
    # hour to hour (36 g at lunch, 33 g at dinner), which is not a target at all.
    from app.services.nutrition_goals import fiber_goal_for
    fiber_goal = fiber_goal_for(st)

    # ONE LINE PER NUDGE (2026-10-21: "I'll feed myself"; the card was too big).
    # `title` names the thing, `meta` carries the numbers, `lines` is empty. Food
    # suggestions are no longer built — _food_rotation / _pick_foods / _food_lines above
    # are kept, unused, so bringing them back is one call.

    # ── Rule: over the calorie target ────────────────────────────────────────
    if calorie_goal and kcal > calorie_goal:
        nudges.append({
            "id": "calories_over", "priority": 15, "tone": "warn",
            "title": f"Calories · {kcal - calorie_goal:,.0f} over",
            "meta": f"{kcal:,.0f}/{calorie_goal:,.0f}",
            "lines": [],
        })

    # ── Rule: protein gap ────────────────────────────────────────────────────
    if protein_goal and hour >= NUDGE_FROM_HOUR:
        gap = protein_goal - protein
        if gap >= MIN_PROTEIN_GAP_G:
            # PACE LEADS WHEN THERE IS ONE (note 7). "50 g to go" is the same sentence
            # every day and says nothing about whether the day is going badly; "24 g
            # behind your usual by lunch" does. The gap-to-goal stays in `meta`.
            from app.services.nutrition_goals import protein_pace
            pace = await protein_pace(db, today)
            if pace and pace["behind"]:
                title = f"Protein · {abs(pace['delta_g'])} g behind usual pace"
                meta = f"{_fmt_g(gap)} g to go"
            else:
                title = f"Protein · {_fmt_g(gap)} g to go"
                meta = f"{_fmt_g(protein)}/{protein_goal} g"
            nudges.append({
                "id": "protein_gap", "priority": 10, "tone": "focus",
                "title": title, "meta": meta, "lines": [], "pace": pace})

    # ── Rule: fiber gap ──────────────────────────────────────────────────────
    if hour >= NUDGE_FROM_HOUR:
        gap = fiber_goal - fiber
        if gap >= MIN_FIBER_GAP_G:
            nudges.append({
                "id": "fiber_gap", "priority": 8, "tone": "focus",
                "title": f"Fiber · {_fmt_g(gap)} g to go",
                "meta": f"{_fmt_g(fiber)}/{fiber_goal} g",
                "lines": []})

    # ── Rule: nothing logged yet, late in the day ────────────────────────────
    if not todays and hour >= 14:
        nudges.append({
            "id": "unlogged", "priority": 12, "tone": "warn",
            "title": "Nothing logged today",
            "lines": [],
        })

    # ── Rule: lifting volume trending down (weekly, not daily) ───────────────
    try:
        from app.models import HevyWorkout
        recent = (await db.execute(
            select(HevyWorkout).where(HevyWorkout.start_time >= day_start - timedelta(days=28))
        )).scalars().all()
        prior = (await db.execute(
            select(HevyWorkout)
            .where(HevyWorkout.start_time >= day_start - timedelta(days=56))
            .where(HevyWorkout.start_time < day_start - timedelta(days=28))
        )).scalars().all()
        if len(prior) >= 4 and len(recent) < len(prior) * 0.7:
            nudges.append({
                "id": "lifting_slide", "priority": 9, "tone": "focus",
                "title": "Lifting volume sliding",
                "meta": f"{len(recent)} sessions / 4 wk (was {len(prior)})",
                "lines": [],
            })
    except Exception as exc:                      # table may not exist / no Hevy
        logger.debug("lifting nudge skipped: %s", exc)

    # Blood-panel flags used to raise a daily nudge here. REMOVED 2026-08-15: a panel
    # changes a few times a year, so the same flag reappeared in Focus every day for
    # months — the definition of a nudge you stop seeing. The assessment still runs and
    # the flags are still shown on the Body tab, which is where you'd act on them.
    # If it comes back, gate it to the days just after a NEW panel lands, not every day.

    nudges.sort(key=lambda n: -n["priority"])
    return nudges
