"""Alcohol pharmacokinetics — standard drink units still on board at bedtime.

Built 2026-08-30 from the user's note of 2026-08-24. Until now alcohol had NO
pharmacokinetic model at all: `log_alcohol` wrote a raw SDU HealthMetric and the
correlation engine used `alcohol_sdu`, the day's total. That total cannot answer
the question that actually matters for sleep.

WHY THIS IS NOT JUST CAFFEINE WITH DIFFERENT CONSTANTS. Caffeine clears by
first-order decay — a fixed FRACTION per unit time, hence the half-life. Alcohol
clears by ZERO-ORDER kinetics: the liver processes a roughly constant ~1 standard
drink per hour regardless of how much is present (Widmark; alcohol dehydrogenase
saturates at very low concentrations). So the residual is a linear drawdown with
a FLOOR AT ZERO, not an exponential tail.

That floor is the whole point, because it makes the result path-dependent in a
way a daily total cannot see (measured, not illustrative):

    4 drinks hourly 18:00–21:00, bed 23:00  →  1.00 SDU on board
    4 drinks 21:00–22:30,        bed 23:00  →  2.30 SDU on board

Same `alcohol_sdu` = 4 on both days. Very different nights: alcohol at sleep
onset is what suppresses REM and drives the elevated sleep HR the app already
tracks as `sleep_hr_elevation`. This gives the correlation engine a term that
distinguishes them.

EXPECT ZERO-INFLATION, AND DO NOT READ IT AS A BUG. On real history the metric is
non-zero on only about a quarter of drinking days, with a median of 0.
That is the physiology, not a failure: drinks logged in the early evening with bed
5–6 h later are genuinely cleared, and two drinks at 19:00 really are gone by
midnight. The metric is therefore a "drank enough, late enough, to still be
metabolising at sleep onset" indicator — genuinely complementary to
`alcohol_sdu`, never a replacement for it. The ties do cost Spearman power, so
give it time before reading anything into its correlations.

The intake window matters far more here than it does for caffeine. Caffeine's
5 h half-life dwarfs an hour of sipping, so the window is nearly a constant
factor; alcohol's ~1 h-per-drink elimination is the SAME timescale as drinking,
so when you drank genuinely changes what is left.

CALIBRATION HONESTY. ELIM_SDU_PER_H is a population average and real clearance
varies with sex, body mass, liver mass and food. As with HALF_LIFE_H in
caffeine.py, within-person day-to-day comparison only needs the ordering of days
to be right, and the ordering is robust to the exact rate. Do NOT read the
absolute SDU figure as a blood-alcohol estimate — it is a relative exposure
index, not a BAC.
"""
from datetime import datetime, timedelta

# Standard drink units cleared per hour. DERIVED, not tuned:
#   one US standard drink = 14 g ethanol
#   Widmark BAC rise      = 14 / (r · W) with r ≈ 0.68, W ≈ 80 kg → ≈ 0.026 g/dL
#   elimination           ≈ 0.015 g/dL/h  →  0.015 / 0.026 ≈ 0.6 SDU/h
# The familiar "one drink per hour" rule is a deliberately conservative heuristic,
# not the physiology, and using it here is actively harmful: at exactly 1.0 the
# intake rate of a nursed drink CANCELS elimination, so every night paced at one
# drink an hour collapses to exactly 0.00 and the metric goes blind on the pace it
# most needs to resolve.
#
# This value was NOT chosen to make the output look better — 0.6 comes from the
# Widmark arithmetic above. It does depend on body mass, so read the result as a
# relative exposure index rather than a BAC.
ELIM_SDU_PER_H = 0.6

# How long one drink takes to consume. Same assumption as caffeine's default —
# The user's note: "an assumption that I drink it in about an hour".
DEFAULT_INTAKE_MIN = 60.0

# Simulation step. Zero-order elimination is exactly linear between events, so the
# only discretisation error is at the zero floor; 5 minutes is far finer than the
# hour-scale signal and keeps the correlation refresh cheap.
_STEP_MIN = 5


def onboard_at_bed(instances: list, bedtime: datetime,
                   intake_min: float = DEFAULT_INTAKE_MIN) -> float | None:
    """Standard drink units still on board at `bedtime` (naive UTC).

    `instances` are HealthMetric rows (metric_type='alcohol', value in SDU). Each
    drink is absorbed uniformly over `intake_min`; the pool is then drawn down at
    ELIM_SDU_PER_H and clamped at zero.

    Forward simulation rather than a closed form precisely BECAUSE of that clamp:
    with zero-order elimination the pool can empty between drinks, and once it has
    emptied the liver cannot "run a deficit" against the next one. A closed-form
    sum would let an early drink's unused clearance cancel a later drink, which is
    physiologically wrong and would understate exactly the late-drinking nights
    this metric exists to flag.

    Returns None only when bedtime is unknown; a day with no drinks is 0.0.
    """
    if bedtime is None:
        return None

    drinks = [(r.timestamp, float(r.value)) for r in instances
              if r.timestamp is not None and r.value is not None
              and r.timestamp < bedtime]
    if not drinks:
        return 0.0

    d_h = max(intake_min, 0.0) / 60.0
    step = timedelta(minutes=_STEP_MIN)
    onboard = 0.0
    t = min(ts for ts, _ in drinks)

    while t < bedtime:
        nxt = min(t + step, bedtime)
        dt_h = (nxt - t).total_seconds() / 3600.0

        for ts, sdu in drinks:
            if d_h <= 0:
                # Instantaneous drink: credit it in the step containing it.
                if t <= ts < nxt:
                    onboard += sdu
                continue
            lo, hi = max(t, ts), min(nxt, ts + timedelta(hours=d_h))
            if hi > lo:
                onboard += sdu * ((hi - lo).total_seconds() / 3600.0) / d_h

        onboard = max(0.0, onboard - ELIM_SDU_PER_H * dt_h)
        t = nxt

    return round(onboard, 2)
