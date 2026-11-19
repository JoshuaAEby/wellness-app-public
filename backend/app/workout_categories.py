"""Shared workout intensity classification."""

from typing import Optional

# Apple Health activity types treated as low-intensity / active recovery.
# Excluded from "training session" counts, consecutive training day streaks,
# and monthly workout personal-best alerts. Walks are tracked separately.
LOW_INTENSITY_TYPES: frozenset[str] = frozenset({
    "Outdoor Walk",
    "Indoor Walk",
    "Yoga",
    "Pilates",
    "Mindfulness",
    "Cool Down",
    "Flexibility",
})

# Coarse workout-type categories for the correlation inputs and the
# workout-type contrast view. Explicit mapping — unknown types return None
# rather than being guessed into a bucket. "walk" == LOW_INTENSITY_TYPES to
# stay consistent with the went_for_walk derived habit.
_STRENGTH_TYPES = frozenset({
    "Traditional Strength Training",
    "Functional Strength Training",
})
_HIIT_TYPES = frozenset({
    "High Intensity Interval Training",
})
_CARDIO_TYPES = frozenset({
    "Outdoor Run", "Indoor Run", "Elliptical", "Outdoor Cycle", "Indoor Cycle",
    "Swimming", "Rowing", "Stair Stepper", "Hiking",
})


def categorize_activity(activity_type: Optional[str]) -> Optional[str]:
    """Map an Apple activity type to 'strength' | 'hiit' | 'cardio' | 'walk' | None."""
    at = activity_type or ""
    if at in _STRENGTH_TYPES:
        return "strength"
    if at in _HIIT_TYPES:
        return "hiit"
    if at in _CARDIO_TYPES:
        return "cardio"
    if at in LOW_INTENSITY_TYPES:
        return "walk"
    return None


# ── Gym-session kind: what the class ACTUALLY was ─────────────────────────────
#
# The user's gym labels every class "High Intensity Interval Training", so one Apple label
# covers two different stimuli: conditioning (burpees, box jumps, ropes) and lifting
# (curls, presses, rows). Derived from heart rate rather than relabelled — see the
# 2026-08-19 analysis of the bimodality that set the threshold (19 sessions
# at 0-4 high-intensity min, 9 at 25-34, only 3 in the 15-19 valley) and why deriving
# beats relabelling: objective, retroactive over all history, and independent of
# remembering to change a watch setting.
#
# LIVES HERE, next to categorize_activity, because several callers need it — the
# summaries' period table and workout list, and the Training tab. It was briefly
# inlined in two of those, which is how CONDITIONING_HI_MIN nearly became a second
# constant with its own drift.
CONDITIONING_HI_MIN = 15.0

# What the Apple LABEL claims. An explicit Strength label is TRUSTED (2026-09-27, see
# classify_gym_session); HIIT stays ambiguous and falls through to heart rate.
_LABEL_CLAIMS = {
    "Traditional Strength Training": "lifting",
    "Functional Strength Training": "lifting",
    "High Intensity Interval Training": None,   # ambiguous here: covers both stimuli
}


def classify_by_hr(activity_type: Optional[str],
                   high_intensity_min: Optional[float]) -> Optional[str]:
    """'lifting' | 'conditioning' | None for a gym session from heart rate ALONE.

    Validated: across the 55 sessions labelled Strength before 2026-04, HR agrees on
    48 (87%); all 7 disagreements fall in one contiguous 2025-01-23..2025-03-03 window
    at 16.6-30.9 hard minutes. Kept separate so `gym_session_agreement` still measures
    something — a cross-check against a classifier that already trusts the label would
    always agree with itself.

    None means UNCLASSIFIABLE, not 'lifting'. A session with no HR is unknown, and
    defaulting it into a bucket would quietly inflate whichever bucket is the default —
    the same failure the habit calendar avoids by returning None for a dead watch.
    """
    if categorize_activity(activity_type) not in ("hiit", "strength"):
        return None
    if high_intensity_min is None:
        return None
    return "conditioning" if float(high_intensity_min) >= CONDITIONING_HI_MIN else "lifting"


def classify_gym_session(activity_type: Optional[str],
                         high_intensity_min: Optional[float]) -> Optional[str]:
    """'lifting' | 'conditioning' | None — an explicit Strength LABEL wins, else HR.

    CHANGED 2026-09-27 (was HR-always). 9/22 "Back and glutes" (Hevy), labelled
    Traditional Strength Training, logged 18.0 hard minutes — a heavy lower-body day
    clears the 15-min conditioning threshold — so the weekly summary's series read 1L/2C
    while its workout list read 2 Strength + 1 HIIT, and the model wrote a critique on
    the wrong count. The old reason for HR-always was series continuity for the
    correlation inputs across the 2026-04 Strength→HIIT relabelling; those inputs have
    been continuous `hard_minutes` since note 31 and no longer call this, and the user
    labels lifting as Strength again since 2026-09-11. HIIT still covers both stimuli,
    so HR decides there. Side effect, accepted: the 7 pre-2026-04 Strength-labelled
    sessions HR called conditioning now read lifting.
    """
    if _LABEL_CLAIMS.get(activity_type or "") == "lifting":
        return "lifting"
    return classify_by_hr(activity_type, high_intensity_min)


def gym_session_agreement(activity_type: Optional[str],
                          high_intensity_min: Optional[float]) -> Optional[bool]:
    """Does the explicit LABEL agree with what heart rate says? None when untestable.

    The point of keeping both signals: once explicit labels start arriving, disagreement
    is itself information — either the label was wrong, or the session was unusual for
    its type. Aggregate this before trusting labels over the derived value; a classifier
    validated against ~20 labelled sessions can then be trusted retroactively, which the
    label alone never can be.
    """
    claim = _LABEL_CLAIMS.get(activity_type or "")
    if not claim or high_intensity_min is None:
        return None
    return claim == classify_by_hr(activity_type, high_intensity_min)
