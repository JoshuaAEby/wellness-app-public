"""One ACWR rule app-wide (app/acwr.py, 2026-11-04)."""
from app.acwr import ACWR_HIGH, ACWR_LOW, ACWR_VERY_HIGH, acwr_band
from app.services import coach_plan
from app.services.ai_summaries import DAILY_SYSTEM_PROMPT


def test_bands_use_rhos_edges():
    assert [acwr_band(x) for x in (None, 0.5, 0.8, 1.3, 1.51, 2.0, 2.01, 2.5, 2.6)] == [
        None, "light", "normal", "normal", "normal", "normal", "high", "high", "very_high"]


def test_coach_acts_on_the_same_edges():
    assert (coach_plan.ACWR_LIGHTER, coach_plan.ACWR_EASY_ONLY) == (ACWR_HIGH, ACWR_VERY_HIGH)


def test_briefing_rubric_states_the_same_numbers():
    rubric = DAILY_SYSTEM_PROMPT.replace("\\\n", "")
    for edge in (ACWR_LOW, ACWR_HIGH, ACWR_VERY_HIGH):
        assert f"{edge:.1f}" in rubric
    assert "1.3-1.5" not in rubric and "high-risk" not in rubric
