"""Standing check for services/voice_time.py (docs/VOICE_LOGGING.md).

Pure function, no DB: run from backend/ with `python tests/verify_voice_time.py`.
Each case: (said, now, expected local datetime "YYYY-MM-DD HH:MM", expected leftover
text, estimated?). Run after ANY edit to the time reader.
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.voice_time import parse_spoken_time, say_when  # noqa: E402

MORNING = datetime(2026, 9, 28, 10, 0)    # Mon 10:00
EVENING = datetime(2026, 9, 28, 21, 0)    # Mon 21:00
EARLY = datetime(2026, 9, 28, 7, 0)       # Mon 07:00

CASES = [
    # nothing said → now
    ("16 oz", MORNING, "2026-09-28 10:00", "16 oz", False),
    ("a glass", EVENING, "2026-09-28 21:00", "a glass", False),
    # bare hour = most recent past occurrence
    ("16 oz at 8", MORNING, "2026-09-28 08:00", "16 oz", False),
    ("16 oz at 8", EVENING, "2026-09-28 20:00", "16 oz", False),
    ("16 oz at 8", EARLY, "2026-09-27 20:00", "16 oz", False),
    ("cold brew at 9:30", MORNING, "2026-09-28 09:30", "cold brew", False),
    # explicit am/pm; a later-than-now time with no day word = yesterday
    ("a glass at 7 pm", MORNING, "2026-09-27 19:00", "a glass", False),
    ("a glass at 7 p.m.", EVENING, "2026-09-28 19:00", "a glass", False),
    ("a pint 9:15 AM", MORNING, "2026-09-28 09:15", "a pint", False),
    ("at 11 pm a bottle", MORNING, "2026-09-27 23:00", "a bottle", False),
    ("20 oz at 14:30", EVENING, "2026-09-28 14:30", "20 oz", False),
    # word numbers
    ("a glass at eight thirty", MORNING, "2026-09-28 08:30", "a glass", False),
    ("a glass at seven", EVENING, "2026-09-28 19:00", "a glass", False),
    # relative
    ("a glass an hour ago", MORNING, "2026-09-28 09:00", "a glass", False),
    ("16 oz 20 minutes ago", MORNING, "2026-09-28 09:40", "16 oz", False),
    ("half an hour ago a pint", MORNING, "2026-09-28 09:30", "a pint", False),
    ("two hours ago 12 oz", MORNING, "2026-09-28 08:00", "12 oz", False),
    # day words with a clock
    ("a glass yesterday at 7 pm", MORNING, "2026-09-27 19:00", "a glass", False),
    ("a glass yesterday at 7", MORNING, "2026-09-27 19:00", "a glass", False),
    ("8:30 this morning 16 oz", EVENING, "2026-09-28 08:30", "16 oz", False),
    ("a pint at 3 this afternoon", EVENING, "2026-09-28 15:00", "a pint", False),
    ("last night at 11 a bottle", MORNING, "2026-09-27 23:00", "a bottle", False),
    ("last night at 1 a glass", MORNING, "2026-09-28 01:00", "a glass", False),
    # day / meal words alone → estimated typical time
    ("a glass with lunch", EVENING, "2026-09-28 12:00", "a glass", True),
    ("a glass with dinner", MORNING, "2026-09-27 19:00", "a glass", True),
    ("16 oz this morning", EVENING, "2026-09-28 09:00", "16 oz", True),
    ("16 oz yesterday", MORNING, "2026-09-27 12:00", "16 oz", True),
    ("a glass tonight", EVENING, "2026-09-28 20:00", "a glass", True),
    ("a glass tonight", MORNING, "2026-09-28 10:00", "a glass", True),   # not future
    # noon / midnight
    ("a bottle at noon", EVENING, "2026-09-28 12:00", "a bottle", False),
    ("a bottle at noon", MORNING, "2026-09-27 12:00", "a bottle", False),
    # numbers that are NOT times stay in the text
    ("12 oz coffee", MORNING, "2026-09-28 10:00", "12 oz coffee", False),
    ("2 glasses", MORNING, "2026-09-28 10:00", "2 glasses", False),
    ("16 ounce IPA at 7 percent", MORNING, "2026-09-28 10:00", "16 ounce IPA at 7 percent", False),
    ("IPA at 7% at 8", EVENING, "2026-09-28 20:00", "IPA at 7%", False),
    ("wine at twelve percent", MORNING, "2026-09-28 10:00", "wine at twelve percent", False),
    ("coffee at 200 mg at 9", MORNING, "2026-09-28 09:00", "coffee at 200 mg", False),
    ("a beer at 5 abv", MORNING, "2026-09-28 10:00", "a beer at 5 abv", False),
    ("12 oz at 9 am", MORNING, "2026-09-28 09:00", "12 oz", False),
]

fails = 0
for said, now, want_when, want_text, want_est in CASES:
    r = parse_spoken_time(said, now)
    got_when = r.when.strftime("%Y-%m-%d %H:%M")
    ok = got_when == want_when and r.text == want_text and r.estimated == want_est
    if not ok:
        fails += 1
    print(f"{'PASS' if ok else 'FAIL'}  {said!r:38s} @ {now:%H:%M} -> {got_when} "
          f"text={r.text!r} est={r.estimated}"
          + ("" if ok else f"   WANT {want_when} {want_text!r} est={want_est}"))

print("\nsay_when:", say_when(datetime(2026, 9, 28, 14, 14), MORNING),
      "|", say_when(datetime(2026, 9, 27, 19, 0), MORNING),
      "|", say_when(datetime(2026, 9, 25, 8, 0), MORNING, estimated=True))
print(f"\n{len(CASES) - fails}/{len(CASES)} passed")
sys.exit(1 if fails else 0)
