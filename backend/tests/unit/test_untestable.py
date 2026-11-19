"""Why a pair is untestable (correlations.untestable_reason, 2026-10-28)."""
from app.services.correlations import MIN_SUPPORT_OBS, untestable_reason


def test_reasons_follow_compute_corr_order():
    varied = [float(i % 7) for i in range(60)]
    constant = [1.0] * 60
    nearly = [1.0] * 55 + [0.0] * (MIN_SUPPORT_OBS - 5)        # too few days off the mode
    assert untestable_reason("slept_away", varied, varied) == "by_design"
    assert untestable_reason("alcohol_sdu", [1.0, 2.0], [3.0, 4.0]) == "few_pairs"
    assert untestable_reason("habit_creatine", constant, varied) == "no_variation_input"
    assert untestable_reason("habit_supplements", nearly[:len(varied)], varied) == "no_variation_input"
    assert untestable_reason("alcohol_sdu", varied, constant) == "no_variation_outcome"
    # Passes every gate → the remaining failure mode is the effective-n one.
    assert untestable_reason("alcohol_sdu", varied, varied) == "low_neff"
