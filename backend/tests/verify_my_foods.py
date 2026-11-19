"""
Verify the My Foods serving/unit arithmetic (services/my_foods). FREE — pure functions.

The cases are the unit chaos that motivated My Foods: a bar logged as "piece", a pizza
as a fraction of a pie and as slices, a soda as a can and in ounces.

Run:  python backend/tests/verify_my_foods.py
"""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")


def main():
    from app.services.my_foods import SIMILAR_MIN, scaled, servings_for, similar_score, split_hint

    bar = NS(serving_qty=1, serving_unit="bar", units="[]", calories=170, protein_g=22,
             carbs_g=22, fat_g=4, fiber_g=11, sugar_g=2, sodium_mg=None)
    pie = NS(serving_qty=1, serving_unit="pie", units=json.dumps([{"label": "slice", "factor": 0.125}]))
    can = NS(serving_qty=1, serving_unit="can", units=json.dumps([{"label": "oz", "factor": 1 / 12}]))
    cheese = NS(serving_qty=28, serving_unit="g", units="[]")
    cases = [
        ("bar logged as '1 piece' is one bar", servings_for(bar, 1, "piece"), 1.0),
        ("'2 bars' is two", servings_for(bar, 2, "bars"), 2.0),
        ("no unit = that many servings", servings_for(bar, 1, None), 1.0),
        ("0.625 of a pie", servings_for(pie, 0.625, "piece"), 0.625),
        ("3 slices via the alternate unit", servings_for(pie, 3, "slices"), 0.375),
        ("12 oz of a 1-can serving", round(servings_for(can, 12, "ounces"), 6), 1.0),
        ("56 g of a 28 g serving", servings_for(cheese, 56, "grams"), 2.0),
        ("foreign unit -> None, never a guess", servings_for(cheese, 1, "cup"), None),
        ("zero quantity -> None", servings_for(bar, 0, "bar"), None),
    ]
    fails = 0
    for label, got, want in cases:
        ok = got == want
        fails += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"   got {got}, want {want}"))
    got = scaled(bar, 2)
    want = {"calories": 340, "protein_g": 44.0, "carbs_g": 44.0, "fat_g": 8.0, "fiber_g": 22.0,
            "sugar_g": 4.0, "sodium_mg": None}
    ok = got == want
    fails += not ok
    print(f"  {'ok  ' if ok else 'FAIL'}  scaled(bar, 2) keeps None as None" + ("" if ok else f"   got {got}"))
    sim = [
        ("same food, 'with' dropped", "Personal pizza with chickpea crust", "Personal pizza chickpea crust", True),
        ("same food, extra word", "Roasted salmon", "Roasted salmon fillet", True),
        ("different bagels sharing a topping are NOT merged", "Parmesan bagel with cream cheese",
         "Berry bagel with cream cheese", False),
        ("different bars", "Kize protein bar", "IQ Protein Bar", False),
    ]
    for label, a, b, want in sim:
        ok = (similar_score(a, b) >= SIMILAR_MIN) == want
        fails += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"   score {similar_score(a, b):.2f}"))
    got = split_hint("Parmesan bagel with cream cheese")
    ok = got == {"base": "Parmesan bagel", "topping": "Cream cheese"}
    fails += not ok
    print(f"  {'ok  ' if ok else 'FAIL'}  split hint: base + topping" + ("" if ok else f"   got {got}"))
    ok = split_hint("Kirkland protein bar") is None
    fails += not ok
    print(f"  {'ok  ' if ok else 'FAIL'}  no split for a single food")
    total = len(cases) + 1 + len(sim) + 2
    print(f"\n{total - fails}/{total} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
