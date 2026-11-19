"""Rho's briefing tone (ai_summaries.rho_tone, voice v5, 2026-10-29) — decided in code."""
from app.services.ai_summaries import rho_tone

# A synthetic month of check-ins (24 of them, mean ~3.1).
MONTH = [2.1, 2.3, 2.5, 2.5, 2.6, 2.6, 2.7, 2.7, 2.8, 2.9, 3.0, 3.1, 3.2, 3.3, 3.3, 3.4, 3.5, 3.5, 3.6, 3.6, 3.7, 3.8, 3.9, 4.0]


def test_push_on_a_training_call_with_an_ordinary_check_in():
    assert rho_tone("CLASS", 3.4, MONTH, None, False, "clear")[0] == "push"


def test_flat_check_in_is_steady():
    # A check-in among the month's lowest — the case v4's prompt-only rule got wrong.
    tone, why = rho_tone("CLASS", 2.3, MONTH, None, False, "clear")
    assert tone == "steady" and "2.3" in why


def test_small_dip_still_pushes():
    assert rho_tone("CLASS", 2.7, MONTH, None, False, "clear")[0] == "push"


def test_alarm_soreness_and_sick_are_steady():
    assert rho_tone("HARD", 3.5, MONTH, None, False, "elevated")[0] == "steady"
    assert rho_tone("HARD", 3.5, MONTH, "left quad", False, "clear")[0] == "steady"
    assert rho_tone("HARD", None, None, None, True, None)[0] == "steady"
    assert rho_tone("HARD", 3.5, MONTH, None, False, "watch")[0] == "push"   # one marker isn't the alarm


def test_rest_call_and_no_call():
    assert rho_tone("REST", 4.0, MONTH, None, False, "clear")[0] == "rest"
    assert rho_tone(None, 4.0, MONTH, None, False, "clear") is None


def test_rest_call_with_a_home_block_is_rest_from_the_gym_only():
    tone, why = rho_tone("REST", 4.0, MONTH, None, False, "clear", home_block=True)
    assert tone == "rest" and "home block" in why
    assert rho_tone("CLASS", 3.4, MONTH, None, False, "clear", home_block=True)[0] == "push"


def test_no_baseline_means_no_energy_judgement():
    assert rho_tone("CLASS", 1.5, None, None, False, None)[0] == "push"
    assert rho_tone("CLASS", 1.5, [3.0, 3.1], None, False, None)[0] == "push"   # < 5 check-ins


def test_hrv_run_counts_only_unbroken_nights():
    from datetime import date
    from app.services.ai_summaries import hrv_run
    d = lambda n: date(2026, 11, n)
    # Up and down: only last night is below baseline.
    assert hrv_run([(d(1), 0.4), (d(2), -0.6), (d(3), 0.1), (d(4), -1.9)]) == (1, 1)
    assert hrv_run([(d(1), -0.2), (d(2), -1.0), (d(3), -1.4)]) == (3, 2)
    assert hrv_run([(d(1), -0.5), (d(3), -0.5)]) == (1, 0)            # a missing date ends it
    assert hrv_run([(d(1), -0.5), (d(2), None), (d(3), -0.5)]) == (1, 0)
    assert hrv_run([(d(1), -0.5), (d(2), 0.3)]) == (0, 0)
    assert hrv_run([]) == (0, 0)


def test_run_counts_in_the_text_follow_the_code():
    from app.services.ai_summaries import enforce_run_counts
    t = "• HRV run: 3 nights below baseline — this is the fourth straight soft night, not a blip."
    assert "third straight soft night" in enforce_run_counts(t, 3, 0)
    assert enforce_run_counts("• HRV -1.9σ, 3rd straight night below baseline", 3, 1).endswith("3rd straight night below baseline")
    assert "2 nights in a row" in enforce_run_counts("HRV down 4 nights in a row", 2, 0)
    # Other lines (training days) are left alone, and so is a count when there is no run.
    assert enforce_run_counts("5 straight training days", 1, 0) == "5 straight training days"
    assert enforce_run_counts("HRV 4th straight night", 0, 0) == "HRV 4th straight night"


def test_alarm_is_called_elevated_only_when_it_is():
    from app.services.ai_summaries import enforce_alarm_level
    t = "**Watch:** Illness/overtraining alarm elevated — HRV suppressed, monitor."
    assert enforce_alarm_level(t, "watch") == "**Watch:** Illness/overtraining alarm WATCH — HRV suppressed, monitor."
    assert "the WATCH alarm" in enforce_alarm_level("Mind the elevated alarm.", "watch")
    assert enforce_alarm_level(t, "elevated") == t
    # Marker wording, and an alarm named correctly, stay as written.
    ok = "Alarm WATCH — resting HR elevated, 1 of 4 markers."
    assert enforce_alarm_level(ok, "watch") == ok
    ok2 = "Alarm WATCH, resting HR elevated."
    assert enforce_alarm_level(ok2, "watch") == ok2
