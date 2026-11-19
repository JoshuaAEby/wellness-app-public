"""
Verify "numbers the user typed are final" (services/ai_summaries._stated_values and the
`locked` path through _normalise_item / _reconcile_atwater). FREE — no API calls.

Why hedges and per-unit numbers are NOT locked: the Kirkland bar was typed as "about
200 cal and 20g protein" for months while its label says 170 / 22 — a hedge is a hint
the reference table may beat. "each one is 180 cal" needs multiplying, which is the
model's job, not a regex's.

Run:  python backend/tests/verify_stated.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

STATED = [
    ("label transcription -> every stated field",
     "i had a protein bar with 210 cal, 10 g of protein, 12 g of sugar, 2 g of fiber and 170 mg sodium",
     {"calories": 210, "protein_g": 10, "sugar_g": 12, "fiber_g": 2, "sodium_mg": 170}),
    ("compact form", "an iq protein bar with 180 cal 12 g protein 1 g sugar",
     {"calories": 180, "protein_g": 12, "sugar_g": 1}),
    ("hedged -> nothing locked", "kirkland protein bar, about 200 cal and 20g protein", {}),
    ("per-unit -> nothing locked", "One chicken melt with American cheese, each one is 180 cal", {}),
    ("no numbers", "parmesan bagel with cream cheese", {}),
]


def main():
    from app.services.ai_summaries import _normalise_item, _stated_values

    fails = []
    for label, text, want in STATED:
        got = _stated_values(text)
        ok = {k: float(v) for k, v in want.items()} == got
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"\n          got {got}"))
        if not ok:
            fails.append(label)

    # Locked calories + protein, model over-accounted via fat: fat must come down,
    # the typed numbers must not move.
    item = {"calories": 210, "protein_g": 10.0, "carbs_g": 22.0, "fat_g": 20.0, "fiber_g": 2.0,
            "confidence": "medium"}
    out = _normalise_item(dict(item), "snack", "bar 210 cal 10 g protein", {"calories", "protein_g"})
    ok = (out["calories"] == 210 and out["protein_g"] == 10.0 and out["fat_g"] < 20.0
          and out["confidence"] == "high")
    print(f"  {'ok  ' if ok else 'FAIL'}  locked fields survive Atwater; an unlocked macro absorbs the gap"
          + ("" if ok else f"\n          got {out}"))
    if not ok:
        fails.append("lock")

    # Everything locked and inconsistent: flag, but never call the user's numbers "low".
    item = {"calories": 100, "protein_g": 30.0, "carbs_g": 30.0, "fat_g": 20.0, "confidence": "high"}
    out = _normalise_item(dict(item), "snack", "x",
                          {"calories", "protein_g", "carbs_g", "fat_g"})
    ok = (out.get("_atwater_unreconciled") is not None and out["confidence"] == "high"
          and out["calories"] == 100)
    print(f"  {'ok  ' if ok else 'FAIL'}  all fields locked -> flagged, untouched, not downgraded"
          + ("" if ok else f"\n          got {out}"))
    if not ok:
        fails.append("all-locked")

    total = len(STATED) + 2
    print(f"\n{total - len(fails)}/{total} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
