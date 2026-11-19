"""
Verify the food parser's two behavioural contracts: COOKING FAT and ITEM SPLITTING.

WHY THIS EXISTS: both are prompt rules, so nothing in the type system or the test
suite catches a regression — a reworded prompt silently changes what gets logged,
and the only symptom is a slow drift in daily calories that looks like diet change.
The item-splitting rules were measured 12/12 ad hoc when they shipped (da13e11) and
then had no standing check; this file is that check, extended to cooking fat.

COOKING FAT (the rule under test, added 2026-09-02): the user does not say whether
he used oil, but roasting/frying means he did. Silence is not evidence of a dry pan.
So fat is inferred from the METHOD, while ingredient fidelity still governs FOODS —
an unmentioned side dish is still never invented. The three directions that matter:

  ADD      method requires fat (roast, sauté, stir-fry, pan-fry, scrambled eggs)
  NONE     method needs none (boil, steam, poach, raw) or none is stated
  ALREADY  fat is already in the number (deep-fried, chain item, user-supplied total)

ALREADY is the one worth guarding hardest: it is the direction that silently DOUBLE
COUNTS, and it fails upward, which is the direction that looks like diligence.

Bounds are ranges, not point values — these are estimates and a tight assertion
would fail on ordinary model variation. The exception is a user-supplied total,
which must come back EXACTLY: transcription, not estimation.

Run:  ANTHROPIC_API_KEY=... python backend/tests/verify_food_parse.py
      (add --model to check a different model; costs a few cents on Haiku)
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ── cases ────────────────────────────────────────────────────────────────────
# Each: (label, description, meal, check(items) -> None | str-explaining-failure)

def _one(items):
    return items[0] if len(items) == 1 else None


def _total(items, field):
    return sum((i.get(field) or 0) for i in items)


def _fat_between(lo, hi):
    def check(items):
        f = _total(items, "fat_g")
        if not (lo <= f <= hi):
            return f"fat_g {f:.1f} outside [{lo}, {hi}]"
        return None
    return check


def _n_items(n):
    def check(items):
        if len(items) != n:
            return f"got {len(items)} items, expected {n}: {[i.get('name') for i in items]}"
        return None
    return check


def _exact(cal, protein, fat):
    def check(items):
        if len(items) != 1:
            return f"expected 1 item, got {len(items)}: {[i.get('name') for i in items]}"
        it = items[0]
        for field, want in (("calories", cal), ("protein_g", protein), ("fat_g", fat)):
            got = it.get(field)
            if got is None or abs(got - want) > max(1.0, want * 0.02):
                return f"{field} {got} != user-supplied {want}"
        return None
    return check


def _confidence(*allowed):
    def check(items):
        bad = [i.get("confidence") for i in items if i.get("confidence") not in allowed]
        if bad:
            return f"confidence {bad} not in {list(allowed)}"
        return None
    return check


def _no_options():
    def check(items):
        offending = [i.get("name") for i in items if i.get("portion_options")]
        if offending:
            return f"portion_options offered on a pinned item: {offending}"
        return None
    return check


def _has_options():
    def check(items):
        if not any(i.get("portion_options") for i in items):
            return "no portion_options on an unquantified item"
        for i in items:
            opts = i.get("portion_options") or []
            if opts and not any(
                    abs((o.get("calories") or 0) - (i.get("calories") or 0)) < 1
                    for o in opts):
                return (f"{i.get('name')}: item calories {i.get('calories')} match no "
                        f"option {[o.get('calories') for o in opts]} — the options are "
                        "meant to include what was chosen, not replace it")
        return None
    return check


def _omitted(lo, hi):
    def check(items):
        tot = sum((i.get("omitted_kcal") or 0) for i in items)
        if not (lo <= tot <= hi):
            return f"omitted_kcal {tot} outside [{lo}, {hi}]"
        return None
    return check


def _names_within(n):
    def check(items):
        long = [(i.get("name"), len(i.get("name") or "")) for i in items
                if len(i.get("name") or "") > n]
        if long:
            return f"name longer than {n} chars: {long}"
        return None
    return check


def _both(*checks):
    def check(items):
        for c in checks:
            r = c(items)
            if r:
                return r
        return None
    return check


CASES = [
    # ── ADD: the method implies fat the user never mentions ──────────────────
    # Plain roasted veg is ~1-2 tsp absorbed per serving. Dry would be <2 g.
    ("ADD  roasted veg", "Roasted broccoli and carrots, about 2 cups", "dinner",
     _fat_between(4, 22)),
    # Chicken breast alone is ~4 g fat / 150 g. A stir fry adds oil on top.
    ("ADD  stir fry", "Stir fry with chicken, peppers, onion and rice", "dinner",
     _fat_between(10, 40)),
    # 2 eggs carry ~10 g fat themselves; scrambling adds butter/oil on top.
    ("ADD  scrambled eggs", "Two scrambled eggs", "breakfast",
     _fat_between(11, 24)),
    # 6 oz salmon is ~12 g fat raw; pan-frying adds to that.
    ("ADD  pan-fried salmon", "Pan-fried salmon fillet, 6 oz", "dinner",
     _fat_between(15, 38)),

    # ── NONE: no fat-bearing method, so nothing may be invented ──────────────
    ("NONE steamed veg", "Steamed broccoli, 2 cups", "dinner", _fat_between(0, 2)),
    ("NONE boiled potato", "Boiled potatoes, 200 g", "dinner", _fat_between(0, 2)),
    ("NONE raw salad", "A bowl of raw spinach and cherry tomatoes", "lunch",
     _fat_between(0, 3)),

    # ── ALREADY: fat is in the number already; adding more double counts ─────
    # 4 falafel balls ~3 g fat each = ~12 g. Double-counting shows up above ~20.
    ("ALRDY falafel", "Four pieces of falafel", "lunch", _fat_between(6, 20)),
    # Big Mac is ~590 cal / 34 g fat. Chain figures already include cooking fat.
    ("ALRDY chain item", "A Big Mac", "lunch",
     _both(_n_items(1), _fat_between(25, 42))),
    # A user-supplied total is transcription, not estimation — must be exact.
    ("ALRDY stated total",
     "Kirkland frozen cheese pizza, 1200 cal 48g protein 48g fat total", "lunch",
     _exact(1200, 48, 48)),

    # ── SPLITTING regressions (must not break while fixing fat) ──────────────
    ("SPLIT sub + chips", "A meatball sub and a small bag of potato chips", "lunch",
     _n_items(2)),
    ("SPLIT composite is one",
     "Two Costco beef hamburger patties on a bun with cheddar cheese", "lunch",
     _n_items(1)),
    ("SPLIT burger + fries",
     "5 guys cheeseburger, about 950 calories, and about 15 French fries", "lunch",
     _n_items(2)),
    ("SPLIT quantity not split", "Two bagels", "breakfast", _n_items(1)),

    # ── SELF-REPORTING: confidence / omitted_kcal / portion_options ──────────
    # The invariant that actually matters. A pinned number must never be offered a
    # size chooser — doing so tells the user the estimate is softer than it is — so
    # this one is enforced in _normalise_item rather than trusted to the prompt.
    ("SELF high has no options",
     "Kirkland protein bar, about 200 cal and 20g protein", "snack",
     _both(_confidence("high"), _no_options())),
    ("SELF chain item is high", "A Big Mac", "lunch",
     _both(_confidence("high"), _no_options(), _omitted(0, 0))),
    ("SELF weighed item is high", "150 g cooked chicken breast", "dinner",
     _both(_confidence("high"), _no_options())),

    # The case portion_options exists for: ~40% of real entries name no quantity.
    ("SELF unquantified offers sizes", "Two handfuls of potato chips", "snack",
     _both(_confidence("medium", "low"), _has_options())),
    ("SELF bowl offers sizes", "A bowl of oatmeal with blueberries", "breakfast",
     _both(_confidence("medium", "low"), _has_options())),
    # Missing INGREDIENT, not missing size — must read low, and a size chooser
    # cannot answer it either way, so options are allowed but not required.
    ("SELF missing ingredient is low", "Stir fry with rice", "dinner",
     _confidence("low")),

    # EXCLUDE AND DISCLOSE (settled 2026-09-04). `calories` is a floor: it holds what
    # was named and nothing else, and an unnamed spread or dressing goes to
    # omitted_kcal instead. Before the rule was made absolute these were coin flips
    # at temperature 0 — "Turkey sandwich" returned [0, 90, 90, 0]. After, four runs
    # each: sandwich [90]x4 (calories 380 -> 320, the mayo leaving the total),
    # bagel [90]x4, chicken salad [100]x4, pancakes [150]x4.
    ("SELF unnamed spread is excluded and reported", "Turkey sandwich", "lunch",
     _omitted(40, 250)),
    ("SELF unnamed dressing is excluded and reported", "Grilled chicken salad", "lunch",
     _omitted(40, 250)),
    # The other direction, which is what stops the field becoming noise. An extra the
    # DISH NAME already implies is not an omission — Caesar dressing is constitutive
    # of a Caesar salad, not an unnamed addition to it — and neither is a chain figure
    # or a user-supplied total.
    ("SELF packaged item omits nothing",
     "Kirkland frozen cheese pizza, 1200 cal 48g protein 48g fat total", "lunch",
     _omitted(0, 0)),
    ("SELF plain weighed food omits nothing", "150 g cooked chicken breast", "dinner",
     _omitted(0, 0)),

    # The picker truncates at ~28 chars, so a name longer than that is a name the
    # user never actually reads. 26% of stored names were 6+ words before this rule.
    ("SELF name fits the picker",
     "Texas Roadhouse chicken critters with fries and a caesar salad", "dinner",
     _names_within(28)),
]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None, help="override the production model")
    ap.add_argument("--repeat", type=int, default=1,
                    help="runs per case; >1 surfaces nondeterminism")
    args = ap.parse_args()

    os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    from app.services import ai_summaries as ai

    if args.model:
        ai.MODEL = args.model

    print(f"model: {ai.MODEL}   cases: {len(CASES)}   repeat: {args.repeat}\n")

    failures = []
    for label, desc, meal, check in CASES:
        for run in range(args.repeat):
            result = await ai.parse_food_description(desc, meal)
            if result is None:
                failures.append((label, "parse returned None (API key / AI disabled?)"))
                print(f"  ERROR {label}: parse returned None")
                continue
            items = result.get("items") or []
            problem = check(items)
            tag = f"{label}" + (f" #{run + 1}" if args.repeat > 1 else "")
            if problem:
                failures.append((tag, problem))
                print(f"  FAIL  {tag}\n          {problem}")
            else:
                summary = " + ".join(
                    f"{i.get('name')} {i.get('calories')}kcal/{i.get('fat_g')}g fat"
                    for i in items)
                print(f"  ok    {tag}: {summary}")

    total = len(CASES) * args.repeat
    print(f"\n{total - len(failures)}/{total} passed")
    if failures:
        print("\nfailures:")
        for label, problem in failures:
            print(f"  {label}: {problem}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
