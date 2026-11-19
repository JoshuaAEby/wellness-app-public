"""services/citations.check — the note-55 misquotes must be caught, real quotes must pass."""
from app.services.citations import check

INPUTS = """PERSONAL PATTERNS
  • Later bedtime → lower Energy (ρ=-0.32, n=88)
  • Phone in the evening → lower Mood (ρ=-0.31, n=64, CI -0.5 to -0.1)
  • Alcohol → higher respiratory rate (ρ=+0.34, n=71)"""


def test_exact_and_formats_pass():
    assert check("Later bedtime lowers energy (ρ=-0.32, n=88).", INPUTS) == []
    assert check("Phone → mood (ρ−0.31).", INPUTS) == []          # U+2212, no "="
    assert check("alcohol and resp rate (ρ 0.34)", INPUTS) == []   # unsigned: magnitude
    assert check("ρ=-0.315", INPUTS) == []                          # rounding tolerance
    assert check("Sleep 7.4h, protein 140 g.", INPUTS) == []


def test_note55_misquotes_caught():
    assert check("screen→mood (ρ −0.38)", INPUTS) == ["ρ −0.38"]            # wrong value
    assert check("later bedtime → higher energy (ρ=+0.32)", INPUTS) == ["ρ=+0.32"]  # sign
    assert check("alcohol → RHR (ρ=0.07, n=125)", INPUTS) == ["n=125", "ρ=0.07"]  # invented


# ── Worded citations (check_words, 2026-10-28) ───────────────────────────────
from app.services.citations import check_words  # noqa: E402

WORD_INPUTS = """WHAT'S NEW (lead with these):
- [lever] Finding CONFIRMED since the last briefing: More water intake → better Mood (ρ=+0.50, n=41)

YOUR PERSONAL PATTERNS (from your own correlation engine):
Confirmed levers (statistically robust — prioritise):
  • Less alcohol → lower RHR (effect next day) (ρ=+0.41, n=320)
  • More sleep → better Energy (ρ=+0.35, n=152)
Suggestive (emerging, needs more data):
  • More water intake → better Energy (effect next day) (ρ=+0.46, n=41)
CONFOUNDED associations — never a lever:
  • Caffeine → higher Energy (next day) (ρ=+0.28, n=147)
"""


def test_supported_worded_citations_pass():
    assert check_words("• Confirmed: water→mood link — yesterday's water was typical", WORD_INPUTS) == []
    assert check_words("RHR up, consistent with alcohol (a confirmed lever)", WORD_INPUTS) == []
    assert check_words("More sleep → better energy remains your strongest lever", WORD_INPUTS) == []
    assert check_words("Caffeine → energy is a confounded pattern, not advice", WORD_INPUTS) == []
    # No link being cited: a trajectory with an arrow, a bare number.
    assert check_words("• Protein gap widening: 162→123g over 2 weeks", WORD_INPUTS) == []
    assert check_words("HRV -0.25σ, within normal range", WORD_INPUTS) == []


def test_10_26_new_suggestive_finding_is_caught():
    bad = check_words("• New: low water (30oz vs usual 60) linked to worse next-day Energy", WORD_INPUTS)
    assert len(bad) == 1 and "not in WHAT'S NEW" in bad[0]


def test_wrong_status_and_unsupported_links_are_caught():
    assert "called confirmed" in check_words("Water → energy is now confirmed", WORD_INPUTS)[0]
    assert "CONFOUNDED" in check_words("Caffeine → better energy, a pattern worth repeating", WORD_INPUTS)[0]
    assert "no finding" in check_words("Protein is linked to better mood", WORD_INPUTS)[0]
