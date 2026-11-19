"""INPUT_FAMILIES registry sanity (2026-10-27): every member is a real discovery input,
and no input belongs to two families (Findings keys groups by a single family)."""
from app.services.correlations import (
    FAMILY_OF, INPUT_FAMILIES, INPUTS, METRIC_LABELS, _DAY_INPUTS, _NIGHT_INPUTS, _STRENGTH_INPUTS,
)


def test_members_are_labelled_inputs():
    discovery = set(_DAY_INPUTS) | set(_NIGHT_INPUTS) | set(_STRENGTH_INPUTS)
    for fid, spec in INPUT_FAMILIES.items():
        assert spec["label"] and spec["blurb"]
        for m in spec["members"]:
            assert m in METRIC_LABELS, (fid, m)
            assert m in INPUTS, (fid, m)
            assert m in discovery, (fid, m)


def test_no_input_in_two_families():
    members = [m for spec in INPUT_FAMILIES.values() for m in spec["members"]]
    assert len(members) == len(set(members)) == len(FAMILY_OF)


def test_sleep_duration_and_bedtime_stay_separate_levers():
    assert "sleep_total_h" not in FAMILY_OF and "bedtime_hour" not in FAMILY_OF
