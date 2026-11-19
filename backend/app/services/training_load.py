"""
HR-based internal training load — Banister TRIMP.

TRIMP = Σ Δt(min) · HRr · 0.64 · e^(k·HRr),   HRr = (HR − HRrest)/(HRmax − HRrest)
k = 1.92 (men), 1.67 (women)   — Banister 1991, blood-lactate-derived, sex-specific.

Per-sample integration (not avg-HR) is used wherever the intra-workout heart-rate
series is available: the weighting is convex, so averaging HR first and then applying
the exponential underestimates load for variable-intensity sessions (Jensen's
inequality). Falls back to a session-level estimate from avg_hr only when no samples
exist.

This is the single load currency for daily_load/ATL/CTL/ACWR (chosen over sRPE because
measured RPE coverage was ~23% — too sparse to be the consistent metric the ACWR ratio
requires). It is a cardiovascular-load measure and structurally
under-weights pure resistance work — an accepted limitation of any HR-based load.
"""
import math
from datetime import datetime
from typing import Optional, Sequence

# Cap the time attributed to a single sample so a gap in the HR stream (watch paused,
# dropped samples) doesn't inflate load. 30 s matches the workout zone-breakdown logic.
_GAP_CAP_S = 30.0


def _k(sex: Optional[str]) -> float:
    return 1.92 if (sex or "male").strip().lower().startswith("m") else 1.67


def _sample_weight(hrr: float, k: float) -> float:
    hrr = max(0.0, min(1.0, hrr))  # clamp: below rest → 0; cap spikes at HRmax
    return hrr * 0.64 * math.exp(k * hrr)


def trimp_from_samples(
    samples: Sequence[tuple[datetime, float]],
    hr_rest: float,
    hr_max: float,
    sex: Optional[str],
    gap_cap_s: float = _GAP_CAP_S,
) -> float:
    """Per-sample Banister TRIMP. `samples` = (timestamp, hr) sorted ascending."""
    denom = hr_max - hr_rest
    if denom <= 0 or len(samples) < 2:
        return 0.0
    k = _k(sex)
    trimp = 0.0
    for i in range(len(samples) - 1):
        hr = samples[i][1]
        dt_s = (samples[i + 1][0] - samples[i][0]).total_seconds()
        if dt_s <= 0:
            continue
        dt_min = min(dt_s, gap_cap_s) / 60.0
        trimp += dt_min * _sample_weight((hr - hr_rest) / denom, k)
    return trimp


def trimp_from_avg(
    avg_hr: Optional[float],
    duration_min: Optional[float],
    hr_rest: float,
    hr_max: float,
    sex: Optional[str],
) -> float:
    """Session-level Banister TRIMP from average HR — fallback when no HR series exists."""
    denom = hr_max - hr_rest
    if denom <= 0 or not avg_hr or not duration_min:
        return 0.0
    return duration_min * _sample_weight((avg_hr - hr_rest) / denom, _k(sex))
