"""Maximum-rate finger tapping — scoring.

Same split as PVT: the browser CAPTURES tap timestamps, this module SCORES them, so a
scoring change never requires re-collecting.

## Why this test is in the battery

Not as a headline — as a CONTROL for the PVT. Sleep loss slows PVT responses, but so do
motor and peripheral factors (cold hands, a stiff shoulder, a different phone case). If
PVT slows and tapping does not, the slowing is attentional. If both slow, it isn't
specific to vigilance. Without this, a PVT change has no mechanism attached to it.

## What to read, and what not to

Tapping RATE is comparatively robust to sleep loss — it degrades, but late and modestly.
Motor VARIABILITY (`iti_cv`) is the more sensitive marker, and it is also the one that
moves with dopaminergic state. Read `iti_cv` first, `tap_rate` second.

`rate_decrement_pct` is the within-block fatigue analogue of the PVT's time-on-task slope.
Expect it to be small and noisy in a rested person; like the PVT slope it is an
aggregate-only measure, not a single-session readout.

## Practice effects

Minimal, and they plateau after one or two sessions — this is one of the few tests
alongside PVT that survives daily administration. Expect the first 1–2 sessions to run
slightly slow; do not treat that as a trend.
"""
import statistics as st
from typing import Optional

# Physiological maximum voluntary tapping is roughly 8 Hz, i.e. ~125 ms between taps.
# Anything under this is a double-registration (one press reported twice) or a stray
# multi-finger contact, not a real tap. Such intervals are EXCLUDED from the interval
# statistics and counted in `artifacts` — a nonzero count means the touch digitiser
# misbehaved, and a large one means the session should be distrusted.
MIN_ITI_MS = 60

# Below this there aren't enough intervals for a variability estimate to mean anything.
MIN_TAPS_FOR_STATS = 10


def score(taps: list[int], duration_ms: int) -> dict:
    """Derive summary metrics from raw tap offsets.

    `taps` is a list of millisecond offsets from block start, ascending.
    """
    ordered = sorted(t for t in taps if t is not None and 0 <= t <= duration_ms)
    n = len(ordered)

    base = {
        "n_taps": n,
        "duration_ms": duration_ms,
        "tap_rate": round(n / (duration_ms / 1000.0), 3) if duration_ms else None,
    }

    if n < MIN_TAPS_FOR_STATS:
        return {
            **base, "mean_iti": None, "iti_sd": None, "iti_cv": None,
            "first_half_rate": None, "second_half_rate": None,
            "rate_decrement_pct": None, "artifacts": 0,
        }

    all_itis = [ordered[i + 1] - ordered[i] for i in range(n - 1)]
    itis = [v for v in all_itis if v >= MIN_ITI_MS]
    artifacts = len(all_itis) - len(itis)

    if len(itis) < MIN_TAPS_FOR_STATS - 1:
        return {
            **base, "mean_iti": None, "iti_sd": None, "iti_cv": None,
            "first_half_rate": None, "second_half_rate": None,
            "rate_decrement_pct": None, "artifacts": artifacts,
        }

    mean_iti = st.mean(itis)
    sd_iti = st.stdev(itis) if len(itis) > 1 else 0.0

    # Split by TIME, not by tap index. Splitting the tap LIST in half would put equal
    # counts either side by construction, which makes a rate decrement undetectable —
    # the halves would differ only in how long they took.
    half = duration_ms / 2.0
    first = sum(1 for t in ordered if t < half)
    second = n - first
    first_rate = first / (half / 1000.0)
    second_rate = second / (half / 1000.0)

    return {
        **base,
        "mean_iti": round(mean_iti, 1),
        "iti_sd": round(sd_iti, 1),
        # Coefficient of variation — the headline. Scale-free, so it stays comparable
        # even if overall tapping speed drifts over months.
        "iti_cv": round(100.0 * sd_iti / mean_iti, 2) if mean_iti else None,
        "first_half_rate": round(first_rate, 3),
        "second_half_rate": round(second_rate, 3),
        # Negative = slowed across the block.
        "rate_decrement_pct": (round(100.0 * (second_rate - first_rate) / first_rate, 2)
                               if first_rate else None),
        "artifacts": artifacts,
    }


# ── KSS ──────────────────────────────────────────────────────────────────────────
# Karolinska Sleepiness Scale, the standard 9-point form. Anchors are part of the
# instrument — the numbers alone are not the scale, and relabelling them breaks
# comparability with everything published using it.
KSS_ANCHORS = {
    1: "Extremely alert",
    2: "Very alert",
    3: "Alert",
    4: "Rather alert",
    5: "Neither alert nor sleepy",
    6: "Some signs of sleepiness",
    7: "Sleepy, but no effort to keep awake",
    8: "Sleepy, some effort to keep awake",
    9: "Very sleepy, great effort to keep awake, fighting sleep",
}


def kss_label(score: Optional[int]) -> Optional[str]:
    return KSS_ANCHORS.get(score) if score is not None else None
