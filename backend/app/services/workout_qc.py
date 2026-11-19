"""Detect workouts the watch recorded but the athlete never actually did.

A watch workout that is started and never stopped keeps recording. Two exist in
this history:

    a ~64 h "Traditional Strength" session spanning three days
    a ~46 h "Outdoor Walk" spanning two

Neither is a workout. The giveaway isn't only the duration — the first one accounts
for just 390 minutes of measured heart-rate time across 63.8 hours, 98% of it in
Zone 1, at an average HR of 79. That is a wrist sitting at resting heart rate for
two and a half days, not exercise.

## What they corrupted

`daily_load` is bounded (TRIMP is attributed once, to the start day) but two other
things are not:

  * every duration aggregate — January 2026 reported 2,870 training minutes across
    four sessions, i.e. twelve hours each
  * the 7-day zone distribution — those 381 and 367 Zone 1 minutes are resting HR
    being counted as training time, dragging `zone1_pct_7d` up for two whole weeks

## Why flag rather than delete or truncate

Truncating to a plausible length would be fabrication: the real session is not
recoverable (no `hr_samples` were stored for either row), so any duration chosen
would be invented. Deleting would erase the fact that something happened at all.
Flagging keeps the record, states the reason, excludes it from aggregates, and is
reversible if a rule ever misfires.

## Thresholds

Deliberately far from anything plausible. The longest genuine session in this
history is 134 minutes; a marathon is ~4 h. Both rules below clear that by a wide
margin, so a real workout should never trip them — and if one ever does, the flag
is a boolean that can be cleared.
"""
from datetime import datetime
from typing import Optional

# Three times the longest real session on record, and twice a marathon.
MAX_PLAUSIBLE_DURATION_SEC = 8 * 3600

# A late gym session can legitimately cross midnight; a three-hour one that does is
# a watch left running. This catches a shorter unattended recording that the
# absolute duration rule alone would miss.
OVERNIGHT_DURATION_SEC = 3 * 3600


def suspect_reason(duration_sec: Optional[float],
                   start_time: Optional[datetime],
                   end_time: Optional[datetime]) -> Optional[str]:
    """Why this workout looks like a watch left running — or None if it looks real.

    Returns a human-readable reason so the flag explains itself wherever it
    surfaces, rather than being an opaque boolean.
    """
    if not duration_sec or duration_sec <= 0:
        return None

    hours = duration_sec / 3600.0
    if duration_sec > MAX_PLAUSIBLE_DURATION_SEC:
        return f"implausible duration ({hours:.1f} h) — watch likely left running"

    if (start_time and end_time
            and duration_sec > OVERNIGHT_DURATION_SEC
            and end_time.date() != start_time.date()):
        return (f"spans {start_time.date()} to {end_time.date()} over {hours:.1f} h "
                "— watch likely left running")

    return None


def is_suspect(duration_sec, start_time, end_time) -> bool:
    return suspect_reason(duration_sec, start_time, end_time) is not None
