"""Goal-gap direction (summary_context, 2026-11-01): computed, then enforced on the text."""
from app.services.summary_context import enforce_gap_direction, gap_direction

BLOCK = """GOAL TRAJECTORY (last 4 weeks):
  Protein: GAP WIDENING (-5 g, latest week vs the earlier weeks; target 146 g — FIXED)
    weekly mean:  -- -> -- -> 138 -> 133 g
  Fiber: GAP CLOSING (+6 g, latest week vs the earlier weeks; target 28 g — FIXED)
  Calories: GAP FLAT (-20 kcal, latest week vs the earlier weeks; target 2436 kcal)
"""


def test_a_thin_first_week_no_longer_decides_it():
    # A 3-day week is dropped before this, leaving 138 -> 133.
    assert gap_direction([None, None, 138, 133], 146)[0] == "widening"


def test_latest_week_is_compared_with_all_earlier_weeks():
    # Over 6 weeks the endpoints said -4 g; every earlier week counts now.
    direction, delta = gap_direction([132, 159, 140, None, None, 128], 146)
    assert direction == "widening" and round(delta) == -16


def test_flat_within_two_percent_and_lower_is_better():
    assert gap_direction([140, 141], 146)[0] == "flat"
    assert gap_direction([2700, 2500], 2450, higher_is_better=False)[0] == "closing"
    assert gap_direction([2400, 2600], 2450, higher_is_better=False)[0] == "widening"


def test_needs_two_counted_weeks():
    assert gap_direction([None, 133], 146) is None


def test_contradicted_direction_is_rewritten():
    # A real line, read against a WIDENING label.
    out = enforce_gap_direction("• Protein gap closing: 136→128g 2 weeks ago", BLOCK)
    assert out == "• Protein gap widening: 136→128g 2 weeks ago"


def test_tense_capitals_and_other_phrasings():
    assert enforce_gap_direction("Fiber gap widened again", BLOCK) == "Fiber gap closed again"
    assert enforce_gap_direction("Protein is closing fast", BLOCK) == "Protein is widening fast"
    assert enforce_gap_direction("Protein gap Closing", BLOCK) == "Protein gap Widening"


def test_correct_lines_are_untouched():
    for line in ("• Protein gap widened again: 146→124g", "Fiber gap closing toward 28g",
                 "Calorie gap flat at 40 kcal"):
        assert enforce_gap_direction(line, BLOCK) == line


def test_each_metric_is_judged_on_its_own_label():
    line = "Protein up but fiber gap closing"
    assert enforce_gap_direction(line, BLOCK) == line


def test_negation_and_value_statements_are_left_alone():
    for line in ("Protein gap isn't closing", "Calories flat at 2500", "Protein bars are fine"):
        assert enforce_gap_direction(line, BLOCK) == line


def test_no_block_means_no_change():
    assert enforce_gap_direction("Protein gap closing", "") == "Protein gap closing"
