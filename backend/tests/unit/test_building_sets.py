"""Which sets count toward a muscle-building dose (strength.counts_as_building, 2026-11-01)."""
import pytest

from app.services.strength import counts_as_building


@pytest.mark.parametrize("title,kg,reps", [
    ("Bicep Curl (Dumbbell)", 18, 10),
    ("Pull Up (Band)", 27, 5),
    ("Push Up", None, 15),
    ("Low Row (Suspension)", 0, 12),
    ("Nordic Hamstrings Curls", None, 6),
    ("Reverse Lunge", None, 12),
])
def test_strength_work_counts(title, kg, reps):
    assert counts_as_building(title, kg, reps)


@pytest.mark.parametrize("title,kg,reps", [
    ("Burpee", None, 12),
    ("Jump Rope (Reps)", None, 100),
    ("Mountain Climber", None, 20),
    ("Plank Pushup", None, 10),
    ("Inchworm with Push Up", None, 8),
    ("Hip Dip", None, 15),
    ("Farmers Walk", 40, None),       # carries have no reps
    ("Wall Sit", None, None),
    ("Bicep Curl (Dumbbell)", 5, 40), # past the rep cap
])
def test_conditioning_and_holds_do_not(title, kg, reps):
    assert not counts_as_building(title, kg, reps)
