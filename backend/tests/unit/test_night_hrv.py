"""Nightly HRV aggregation (metrics_engine._night_hrv, 2026-10-28)."""
import math

from app.services.metrics_engine import HRV_NIGHT_MIN_SAMPLES, _night_hrv


def test_too_few_samples_is_no_reading():
    # A morning snapshot taken when only two readings were in.
    assert HRV_NIGHT_MIN_SAMPLES == 3
    assert _night_hrv([164.8, 99.4]) is None
    assert _night_hrv([50.0, None, 0.0]) is None      # None / non-positive don't count


def test_geometric_mean_damps_one_high_reading():
    night = [28.8, 26.9, 164.8, 99.4]                 # the settled night
    g = _night_hrv(night)
    assert math.isclose(g, math.exp(sum(map(math.log, night)) / 4))
    assert round(g) == 60                              # arithmetic mean would be 80
    assert math.isclose(_night_hrv([55.0, 55.0, 55.0]), 55.0)
