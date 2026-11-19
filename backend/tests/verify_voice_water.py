"""Standing check for the water amount reader (services/voice.interpret_water_amount).

Run inside the container (needs app deps):
  docker exec -i -w /app wellness-app python tests/verify_voice_water.py
Presets are the shipped defaults with a 30 oz bottle. Time phrases are stripped by
voice_time BEFORE this runs, so they are not exercised here (verify_voice_time.py).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.voice import interpret_water_amount  # noqa: E402

PRESETS = [{"id": "bottle", "label": "Refill", "oz": 30},
           {"id": "glass", "label": "Glass", "oz": 8},
           {"id": "pint", "label": "Pint", "oz": 16}]

CASES = [   # (said, expected oz or None)
    ("16 oz", 16), ("16 ounces", 16), ("16 ounce", 16), ("12 fl oz", 12),
    ("16 ounces of water", 16), ("20", 20), ("8.5 oz", 8.5),
    ("a glass", 8), ("glass of water", 8), ("two glasses", 16), ("2 glasses", 16),
    ("a cup", 8), ("a pint", 16), ("half a pint", 8),
    ("a bottle", 30), ("my water bottle", 30), ("a refill", 30), ("refill", 30),
    ("two refills", 60), ("half a bottle", 15),
    ("500 ml", 16.9), ("500 milliliters", 16.9), ("a liter", 33.8), ("1 liter", 33.8),
    ("one litre", 33.8), ("1.5 liters", 50.7), ("two liters", 67.6),
    ("water", None), ("some", None), ("", None),
]

fails = 0
for said, want in CASES:
    got = interpret_water_amount(said, PRESETS)
    oz = round(got[0], 1) if got else None
    ok = (oz is None and want is None) or (oz is not None and want is not None and abs(oz - want) < 0.15)
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {said!r:26s} -> {oz!s:6s} {(got[1] or '') if got else ''}"
          + ("" if ok else f"   WANT {want}"))
print(f"\n{len(CASES) - fails}/{len(CASES)} passed")
sys.exit(1 if fails else 0)
