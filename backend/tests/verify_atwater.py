"""
Verify the server-side Atwater reconciliation (services/ai_summaries._reconcile_atwater).

FREE TO RUN — pure functions, no API calls. That is deliberate: verify_food_parse.py
costs real money per run, so the logic that can be checked offline is checked offline.

WHAT IT GUARDS. The prompt has instructed the model to self-check 4P + 4C + 9F against
its own calorie total since the beginning, and 84 of 521 food items (16%) still violate
it, median 11% off. Arithmetic instructions are not binding on a model. This is.

THE ASYMMETRY IS THE WHOLE POINT, and it came from the data rather than from theory:

  calories > macros   80 of 84 violators, and 36 of those carry a user-stated calorie
                      figure, so the total is transcription and the macros are the
                      under-accounted derived side  ->  MOVE THE MACROS UP.

  macros > calories   4 violators, but this is the Costco-burger shape: the model
                      listed the right components in its notes and returned 815 instead
                      of 1090. Overstating all three macros at once is far less likely
                      than mis-adding one total  ->  MOVE CALORIES UP.

An earlier version moved macros in BOTH directions and "fixed" the burger by cutting
fat 79 g -> 49 g — destroying correct macros to defend the number that was actually
wrong. Case 3 below exists so that cannot come back.

Run:  python backend/tests/verify_atwater.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")


def _atwater(it):
    return 4 * it["protein_g"] + 4 * it["carbs_g"] + 9 * it["fat_g"]


# (label, item, meal_type, user_text, expectation(before, after) -> None | failure str)
def _moved(field, direction):
    def check(before, after):
        fix = after.get("_atwater_fix")
        if not fix:
            return "no adjustment was made"
        if fix["field"] != field:
            return f"adjusted {fix['field']}, expected {field}"
        if direction == "up" and fix["to"] <= fix["from"]:
            return f"{field} moved {fix['from']} -> {fix['to']}, expected an increase"
        if direction == "down" and fix["to"] >= fix["from"]:
            return f"{field} moved {fix['from']} -> {fix['to']}, expected a decrease"
        # and the books must actually balance afterwards
        gap = abs(_atwater(after) - after["calories"])
        if gap > after["calories"] * 0.081:
            return f"still {gap:.0f} kcal out after adjustment"
        return None
    return check


def _untouched():
    def check(before, after):
        for k in ("calories", "protein_g", "carbs_g", "fat_g"):
            if before[k] != after[k]:
                return f"{k} changed {before[k]} -> {after[k]}; must be left alone"
        if after.get("_atwater_fix"):
            return "reported an adjustment on an item that must be exempt"
        return None
    return check


def _raised_to(expected):
    def check(before, after):
        fix = after.get("_atwater_fix")
        if not fix or fix["field"] != "calories":
            return f"expected calories to be raised, got {fix}"
        if abs(after["calories"] - expected) > 1:
            return f"calories -> {after['calories']}, expected {expected} (the fiber-at-0 floor)"
        return None
    return check


def _refused():
    def check(before, after):
        if after.get("_atwater_unreconciled") is None:
            return "did not flag an unabsorbable gap"
        if after.get("confidence") != "low":
            return f"confidence is {after.get('confidence')}, expected low"
        for k in ("calories", "protein_g", "carbs_g", "fat_g"):
            if before[k] != after[k]:
                return f"{k} was changed on a refused item"
        return None
    return check


CASES = [
    ("calories > macros, user stated the total -> macros rise",
     {"calories": 360, "protein_g": 22.0, "carbs_g": 30.0, "fat_g": 10.0, "confidence": "high"},
     "lunch", "One chicken melt with American cheese, each one is 180 cal",
     _moved("fat_g", "up")),

    ("within tolerance -> nothing happens",
     {"calories": 1015, "protein_g": 64.0, "carbs_g": 28.0, "fat_g": 71.0, "confidence": "high"},
     "lunch", "two Costco patties on a bun with cheddar",
     _untouched()),

    # The regression guard. Macros were right; the TOTAL was the error.
    ("macros > calories, total not stated -> CALORIES rise, macros survive",
     {"calories": 815, "protein_g": 64.0, "carbs_g": 29.0, "fat_g": 79.0, "confidence": "high"},
     "lunch", "Two Costco frozen burgers on a bun with shredded cheese and ranch",
     _moved("calories", "up")),

    # A number the user typed always wins, even against three macros.
    ("macros > calories but the user stated it -> their number wins",
     {"calories": 815, "protein_g": 64.0, "carbs_g": 29.0, "fat_g": 79.0, "confidence": "high"},
     "lunch", "my usual burger, about 815 cal",
     _moved("fat_g", "down")),

    ("beer logged as a drink -> exempt (ethanol is 7 cal/g, not an Atwater term)",
     {"name": "IPA beer", "calories": 255, "protein_g": 2.5, "carbs_g": 15.0,
      "fat_g": 0.0, "confidence": "high"},
     "drinks", "IPA beer",
     _untouched()),

    ("beer logged as a snack -> still exempt, matched on the text",
     {"name": "Two IPA beers", "calories": 400, "protein_g": 4.0, "carbs_g": 34.0,
      "fat_g": 0.0, "confidence": "high"},
     "snack", "Two IPA beers",
     _untouched()),

    # Fiber is carbohydrate but ~0–2 kcal/g (added 2026-09-12). Label-true high-fiber
    # items must pass untouched instead of having their calories "fixed" upward.
    ("high-fiber bar at label values -> untouched (fiber is not 4 kcal/g)",
     {"calories": 170, "protein_g": 22.0, "carbs_g": 22.0, "fat_g": 4.0, "fiber_g": 11.0,
      "confidence": "high"},
     "snack", "kirkland protein bar",
     _untouched()),

    ("two high-fiber bars -> untouched, NOT raised to the 4-4-9 total (was 424)",
     {"calories": 340, "protein_g": 44.0, "carbs_g": 44.0, "fat_g": 8.0, "fiber_g": 22.0,
      "confidence": "high"},
     "snack", "2 kirkland protein bars",
     _untouched()),

    ("prebiotic soda -> untouched",
     {"calories": 45, "protein_g": 0.0, "carbs_g": 16.0, "fat_g": 0.0, "fiber_g": 9.0,
      "confidence": "high"},
     "snack", "an olipop",
     _untouched()),

    ("macros above even the fiber-at-0 total -> calories rise only to that floor",
     {"calories": 100, "protein_g": 10.0, "carbs_g": 30.0, "fat_g": 5.0, "fiber_g": 10.0,
      "confidence": "high"},
     "snack", "a high fiber cereal bowl",
     _raised_to(165)),

    ("gap no macro can absorb -> refuse and downgrade, never fabricate",
     {"calories": 50, "protein_g": 30.0, "carbs_g": 30.0, "fat_g": 20.0, "confidence": "high"},
     "lunch", "something impossible, 50 cal",
     _refused()),
]


def main():
    from app.services.ai_summaries import _normalise_item

    failures = []
    for label, item, meal, text, check in CASES:
        before = dict(item)
        after = _normalise_item(dict(item), meal, text)
        problem = check(before, after)
        if problem:
            failures.append((label, problem))
            print(f"  FAIL  {label}\n          {problem}")
        else:
            print(f"  ok    {label}")

    print(f"\n{len(CASES) - len(failures)}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
