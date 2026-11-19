"""ACWR bands: one rule for every label, chart band and prompt (2026-11-04).

Rho's rule (coach_plan, 2026-10-29) is the one kept: load only changes the plan when it is
"super high". His ACWR has mean 0.99 and SD 0.69 (median 1.18), so the textbook 0.8–1.3
band for a smoothed 7:28 ratio called 13 of the 91 days to 11-04 "overreaching", the
briefing called 1.51 "high-risk", and Progress drew a 1.5 "danger zone", all while Rho
changed nothing.

Labels only. Decision rules that predate this keep their own numbers on purpose:
training_rec (experiment eligibility reads it), coach.STRONG_ACWR and the 2.2 overnight
alert in scheduler.py.
"""
from typing import Optional

ACWR_LOW = 0.8          # below: a light week against your base. Not a problem; Rho treats it as fresh.
ACWR_HIGH = 2.0         # above: Rho eases effort
ACWR_VERY_HIGH = 2.5    # above: Rho says easy only


def acwr_band(acwr: Optional[float]) -> Optional[str]:
    """light / normal / high / very_high, with the same edges coach_plan acts on."""
    if acwr is None:
        return None
    if acwr > ACWR_VERY_HIGH:
        return "very_high"
    if acwr > ACWR_HIGH:
        return "high"
    if acwr < ACWR_LOW:
        return "light"
    return "normal"
