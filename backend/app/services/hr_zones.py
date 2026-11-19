"""Heart-rate zone boundaries — the SINGLE source of truth.

Before 2026-08-08 the cutoffs were hardcoded absolute bpm (115/135/155) in
`metrics_engine._workout_metrics`, and *separately* hardcoded again — with a fifth
boundary at 170 — in `routers/metrics.get_training_zones`. Two copies of the same
constants is how they silently diverge. Everything now derives from here.

## Karvonen (%HRR), not %HRmax

    bpm(p) = rest + p * (max - rest)

%HRR is preferred over %HRmax because it accounts for resting heart rate, so the
bands track the actual working range rather than a fraction of peak. Boundaries use
the conventional 5-zone split at 60/70/80/90% HRR.

For this athlete the old fixed cutoffs were 1-2 zones too low: 115/135/155 bpm work
out to 39%/54%/70% HRR against a measured max of 194 and resting ~65, so what the app
called "Zone 2" sat below most models' Zone 1, and "high intensity" began where a
standard Zone 3 begins.

## Why the resting reference is FROZEN, not the rolling baseline

`daily_metrics.rhr_baseline` drifts daily. Deriving zone edges from it would move the
zone definitions under the data — the exact instability that argues against adopting
Apple's (algorithm-may-change) zones in the first place. A definition that shifts
mid-series silently breaks every longitudinal comparison and correlation.

So `user_settings.hrr_rest_ref` is a deliberately STATIC reference, set once from the
observed resting baseline and changed only on purpose. `max_hr` may change (it is
measured, and drifts ~1 bpm/yr with age) — but rarely, and a full history recompute
follows when it does, keeping the series internally consistent.
"""
from typing import Optional

# Conventional Karvonen 5-zone split, as fractions of heart-rate reserve.
ZONE_HRR_FRACTIONS = (0.60, 0.70, 0.80, 0.90)

# Fallbacks when settings are incomplete. 60 bpm mirrors the resting-HR default
# already used for TRIMP in metrics_engine.
DEFAULT_MAX_HR = 185
DEFAULT_REST_REF = 60


def zone_bounds(max_hr: Optional[int], rest_ref: Optional[int]) -> tuple[float, float, float, float]:
    """Absolute bpm at the Z2 / Z3 / Z4 / Z5 boundaries.

    Returns (z2_start, z3_start, z4_start, z5_start). Zone 1 is everything below
    z2_start. The app's 4-bucket storage merges Z4+Z5, i.e. "high intensity" is
    anything >= z4_start; the Z5 edge is returned for the 5-zone training view.
    """
    mx = max_hr or DEFAULT_MAX_HR
    rest = rest_ref or DEFAULT_REST_REF
    hrr = mx - rest
    if hrr <= 0:                      # nonsense settings — degrade, don't explode
        mx, rest = DEFAULT_MAX_HR, DEFAULT_REST_REF
        hrr = mx - rest
    return tuple(rest + p * hrr for p in ZONE_HRR_FRACTIONS)


def zone_index(bpm: float, bounds: tuple[float, float, float, float]) -> int:
    """0-based zone for a sample: 0=Z1, 1=Z2, 2=Z3, 3=Z4, 4=Z5."""
    z2, z3, z4, z5 = bounds
    if bpm < z2:
        return 0
    if bpm < z3:
        return 1
    if bpm < z4:
        return 2
    if bpm < z5:
        return 3
    return 4


def describe(max_hr: Optional[int], rest_ref: Optional[int]) -> dict:
    """Human-readable derivation, for surfacing in the UI / debugging."""
    z2, z3, z4, z5 = zone_bounds(max_hr, rest_ref)
    return {
        "max_hr": max_hr or DEFAULT_MAX_HR,
        "rest_ref": rest_ref or DEFAULT_REST_REF,
        "method": "Karvonen %HRR at 60/70/80/90",
        "bounds": {"z2": round(z2, 1), "z3": round(z3, 1),
                   "z4": round(z4, 1), "z5": round(z5, 1)},
    }
