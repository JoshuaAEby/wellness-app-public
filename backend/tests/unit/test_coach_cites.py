"""Coach extra lines cite the LIVE cache (coach_plan._live_finding / _cite, 2026-10-28)."""
from app.services.coach_plan import _cite, _live_finding


def _row(r, n, sig=False, sug=False, check="passes"):
    return {"spearman_r": r, "n": n, "significant": sig, "suggestive": sug, "reverse_check": check}


def test_only_surviving_unconfounded_findings_are_cited():
    cache = {
        ("alcohol_sdu", "hrv_z_score", 1): _row(-0.204, 317, sig=True),
        ("zone2_pct_7d", "subj_mood", 1): _row(0.349, 147),                  # no longer survives
        ("wfh_day", "subj_energy", 1): _row(0.28, 147, sug=True, check="confounded"),
    }
    assert _live_finding(cache, "alcohol_sdu", "hrv_z_score", 1) is not None
    assert _live_finding(cache, "zone2_pct_7d", "subj_mood", 1) is None
    assert _live_finding(cache, "wfh_day", "subj_energy", 1) is None
    assert _live_finding(cache, "missing", "pair", 1) is None


def test_cite_states_current_numbers_and_tier():
    assert _cite(_row(-0.204, 317, sig=True)) == "ρ -0.20, n=317, confirmed"
    assert _cite(_row(0.28, 147, sug=True)) == "ρ +0.28, n=147, suggestive"
