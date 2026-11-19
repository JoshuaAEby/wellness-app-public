"""Marker semantics: which way is *better* — the SINGLE source of truth.

A lab reference range says where the population sits, not whether being outside it
is bad. For most analytes those coincide; for a few the upper bound is meaningless
or inverted. HDL 67 against a printed range of 40-59 is not a problem, it is the
good outcome — but a bare `value > ref_high` test calls it "HIGH" in red.

That bug was live in three places at once (the Body card, `blood_block`'s ⚠ ABOVE
RANGE markers fed to the weekly/monthly summaries, and `events_block`'s
out-of-range count), which is exactly the duplicated-constant failure the HR-zone
consolidation was meant to teach. Everything now derives from here.

## Deliberately conservative

Only markers whose direction is unambiguous and well established are classified.
Everything else stays `two_sided`, which reproduces the old behaviour exactly. The
goal is to remove false alarms we're confident about — not to assert a direction
for every analyte and introduce new wrong ones. Adding a marker here is a claim
that being outside the range on one side genuinely doesn't warrant attention; make
that claim only when it's true.
"""
import re
from typing import Optional

TWO_SIDED = "two_sided"
HIGHER_BETTER = "higher_better"
LOWER_BETTER = "lower_better"

# Above the printed upper bound is fine-to-good; below the lower bound is what matters.
_HIGHER_BETTER = {
    "hdl_c",      # protective; many labs print 40-59 as "normal" with >59 desirable
    "egfr",       # higher filtration is better; >90 is the normal band
}

# Below the printed lower bound is fine; the upper bound is what matters. Mostly this
# suppresses meaningless LOW flags (an hs-CRP under the range is the desired result).
_LOWER_BETTER = {
    "apob", "ldl_c", "lp_a", "triglycerides", "vldl_c", "ldl_hdl_ratio",
    "hs_crp", "hba1c", "fasting_insulin",
    "alt", "ast", "ggt",          # hepatocellular enzymes: low is unremarkable
    # Eosinophils / basophils (2026-10-28): a low count in isolation is not a finding;
    # an eosinophil % of 0.7 on a 0-7 range sat on "watch" for hugging a floor of 0.
    "eosinophils", "basophils", "eosinophils_abs", "basophils_abs",
}


# Canonical unit per marker, used ONLY to repair damage — never to override a clean
# unit the lab actually printed (see normalize_unit).
CANONICAL_UNIT = {
    "albumin": "g/dL", "alp": "U/L", "alt": "U/L", "ast": "U/L", "ggt": "U/L",
    "bilirubin_total": "mg/dL", "total_protein": "g/dL",
    "bun": "mg/dL", "creatinine": "mg/dL", "egfr": "mL/min/1.73m²",
    "sodium": "mmol/L", "potassium": "mmol/L", "chloride": "mmol/L", "co2": "mmol/L",
    "calcium": "mg/dL", "magnesium": "mg/dL", "uric_acid": "mg/dL",
    "fasting_glucose": "mg/dL", "hba1c": "%",
    "total_cholesterol": "mg/dL", "ldl_c": "mg/dL", "hdl_c": "mg/dL",
    "vldl_c": "mg/dL", "triglycerides": "mg/dL", "apob": "mg/dL",
    "hemoglobin": "g/dL", "hematocrit": "%", "rbc": "10⁶/µL", "wbc": "10³/µL",
    "platelets": "10³/µL", "mcv": "fL", "mch": "pg", "mchc": "g/dL",
    "rdw": "%", "mpv": "fL",
    "neutrophils": "%", "lymphocytes": "%", "monocytes": "%",
    "eosinophils": "%", "basophils": "%",
    "tsh": "mIU/L", "hs_crp": "mg/L", "ferritin": "ng/mL", "vitamin_d": "ng/mL",
}

# Definitionally dimensionless — a ratio or computed index never carries a unit.
UNITLESS = {"bun_creatinine_ratio", "ldl_hdl_ratio", "fib_4", "ast_alt_ratio"}

# Words labs print in a unit column that are not units.
_NON_UNITS = {"ratio", "index", "score", "calc", "calculated", "n/a", "na", "-", "--"}

# Superscript/micro characters that survive PDF text extraction as escapes or
# mojibake: "10\S\3/uL", "10³/ÂµL", replacement chars.
_DAMAGED = re.compile(r"\\S\\|\\U\\|Âµ|�")


def normalize_unit(marker: Optional[str], unit: Optional[str]) -> Optional[str]:
    """Repair a unit without ever silently converting one.

    Deliberately narrow. It fixes only cases where the stored text cannot be what
    the lab meant: PDF-extraction damage, an empty cell, a non-unit word like
    "ratio", or a unit on a dimensionless index. It will NOT replace a clean unit
    that merely disagrees with the canonical one — a lab reporting cholesterol in
    mmol/L is legitimate, and rewriting that to mg/dL would corrupt the value by
    ~39x while looking tidy. Those mismatches are surfaced, not silently changed.
    """
    key = (marker or "").strip().lower()
    u = (unit or "").strip()

    if key in UNITLESS:
        return None
    canon = CANONICAL_UNIT.get(key)
    if not u or u.lower() in _NON_UNITS or _DAMAGED.search(u):
        return canon or (None if not u else u)
    return u


def direction(marker: Optional[str]) -> str:
    key = (marker or "").strip().lower()
    if key in _HIGHER_BETTER:
        return HIGHER_BETTER
    if key in _LOWER_BETTER:
        return LOWER_BETTER
    return TWO_SIDED


# ── Optimal targets ───────────────────────────────────────────────────────────
#
# A lab reference range is a POPULATION interval — typically the central 95% of
# whoever the lab used to establish it. That population is not healthy, it is
# average. The classic case is ALT: an upper limit near 50 U/L derives from cohorts
# that included a lot of undiagnosed fatty liver, and hepatology work has argued the
# true healthy ceiling is closer to 30 for men. "In range" and "optimal" are simply
# different questions.
#
# ⚠ THESE ARE OPINIONS, NOT CLINICAL THRESHOLDS. They come from preventive-
# cardiology and metabolic-health literature, where reasonable people disagree and
# guidance shifts. They are seeded defaults, deliberately editable per marker, and
# rendered distinctly from the lab range so the two are never confused. They must
# NEVER drive the same "needs attention" signal as an out-of-range value — being off
# a target you can edit is a different claim from being outside the lab's interval.
#
# (marker) -> (target_low, target_high, one-line rationale)
DEFAULT_TARGETS = {
    # Atherogenic particles — the lipid targets people actually act on.
    "apob": (None, 80.0, "primary-prevention target; <60 often used for high risk"),
    "ldl_c": (None, 70.0, "well below the lab's 100 ceiling"),
    "hdl_c": (50.0, None, "above the 40 floor labs use for men"),
    "triglycerides": (None, 100.0, "metabolic-health target, vs the lab's 150"),
    "lp_a": (None, 75.0, "genetically fixed; measure once, know your risk"),
    # Glycaemic control
    "hba1c": (None, 5.4, "below the 5.7 pre-diabetes line, not merely off it"),
    "fasting_glucose": (None, 95.0, "vs the lab's 99-100 ceiling"),
    "fasting_insulin": (None, 7.0, "insulin sensitivity; labs often accept ~25"),
    # Inflammation
    "hs_crp": (None, 1.0, "low cardiovascular risk band; labs flag only above 3"),
    # Liver — the clearest case of a lax lab ceiling
    "alt": (None, 30.0, "healthy-population ceiling for men is nearer 30 than 50"),
    "ast": (None, 30.0, "as ALT"),
    "ggt": (None, 30.0, "sensitive to alcohol and metabolic load"),
    # Stores
    "vitamin_d": (40.0, 60.0, "sufficiency band, above the 30 floor"),
    "ferritin": (50.0, 150.0, "iron stores; the lab's 30-400 is very wide"),
    "egfr": (90.0, None, "normal filtration, not merely 'not impaired'"),
}


def default_target(marker: Optional[str]):
    """(low, high, rationale) or None."""
    return DEFAULT_TARGETS.get((marker or "").strip().lower())


def vs_target(value: Optional[float], t_low: Optional[float],
              t_high: Optional[float]) -> Optional[str]:
    """'above' / 'below' / 'at' the target band — or None when no target applies.

    Kept separate from `assess()` on purpose: this answers "versus the goal you set",
    not "versus the lab's population interval", and the UI must not conflate them.
    """
    if value is None or (t_low is None and t_high is None):
        return None
    if t_high is not None and value > t_high:
        return "above"
    if t_low is not None and value < t_low:
        return "below"
    return "at"


# ── Panel grouping ────────────────────────────────────────────────────────────
#
# 38 markers in one alphabetical list hides structure: the four liver enzymes moving
# together is obvious inside a Liver group and invisible between "alk phos" and
# "baso%". Grouping is by the panel a lab actually orders, which is also how you'd
# reason about a result.
GROUP_ORDER = ["Lipids", "Metabolic", "Liver", "Kidney & Electrolytes",
               "Blood count", "Thyroid & Hormones", "Inflammation & Nutrients", "Other"]

_GROUP = {
    "Lipids": {"total_cholesterol", "ldl_c", "hdl_c", "vldl_c", "triglycerides",
               "apob", "lp_a", "ldl_hdl_ratio", "non_hdl_c"},
    "Metabolic": {"fasting_glucose", "hba1c", "fasting_insulin", "homa_ir", "uric_acid"},
    "Liver": {"alt", "ast", "alp", "ggt", "bilirubin_total", "albumin",
              "total_protein", "fib_4", "ast_alt_ratio"},
    "Kidney & Electrolytes": {"bun", "creatinine", "egfr", "bun_creatinine_ratio",
                              "sodium", "potassium", "chloride", "co2", "calcium",
                              "magnesium", "phosphorus"},
    "Blood count": {"hemoglobin", "hematocrit", "rbc", "wbc", "platelets", "mcv",
                    "mch", "mchc", "rdw", "mpv", "neutrophils", "lymphocytes",
                    "monocytes", "eosinophils", "basophils", "neutrophils_abs",
                    "lymphocytes_abs", "monocytes_abs", "eosinophils_abs", "basophils_abs"},
    "Thyroid & Hormones": {"tsh", "free_t4", "free_t3", "testosterone_total",
                           "testosterone_free", "estradiol", "shbg", "cortisol", "dhea_s"},
    "Inflammation & Nutrients": {"hs_crp", "ferritin", "iron", "tibc", "transferrin_sat",
                                 "vitamin_d", "vitamin_b12", "folate", "homocysteine"},
}
_MARKER_GROUP = {m: g for g, ms in _GROUP.items() for m in ms}


def group(marker: Optional[str]) -> str:
    return _MARKER_GROUP.get((marker or "").strip().lower(), "Other")


# A value this close to the bound that matters (as a fraction of the reference
# width) is "worth a look" without being out of range. Calibrated against a real
# 38-marker panel: surfaces 2 rows, which is the intended "slightly wider net".
NEAR_BOUND_FRAC = 0.10


def near_bound(marker: Optional[str], value: Optional[float],
               ref_low: Optional[float], ref_high: Optional[float]) -> bool:
    """In range, but close to the edge that actually matters for this marker."""
    if value is None or ref_low is None or ref_high is None or ref_high <= ref_low:
        return False
    a = assess(marker, value, ref_low, ref_high)
    if a["position"] != "in":
        return False                      # already out of range; a stronger signal owns it
    margin = (ref_high - ref_low) * NEAR_BOUND_FRAC
    d = a["direction"]
    if d != HIGHER_BETTER and value >= ref_high - margin:
        return True
    if d != LOWER_BETTER and value <= ref_low + margin:
        return True
    return False


# ── Reference change value ────────────────────────────────────────────────────
#
# Two draws differ for three reasons: the thing actually changed, the analyte varies
# within you day to day (biological variation, CV_i), and the assay has error
# (analytical variation, CV_a). A UI that draws an arrow on any non-zero delta
# claims the first while measuring all three.
#
# RCV is the standard guard: a repeat measurement must change by more than
#
#     RCV% = Z * sqrt(2) * sqrt(CV_a^2 + CV_i^2)          Z = 1.96 -> 2.77 at p<0.05
#
# before the difference is distinguishable from noise. Below that, "+7" is not news.
#
# ⚠ The CVs below are APPROXIMATE literature values (the Westgard / EFLM biological-
# variation database is the usual source). Published estimates differ between studies
# and populations, sometimes substantially — hs-CRP in particular ranges from ~40% to
# ~60% CV_i depending on the cohort. They are good enough to separate "clearly noise"
# from "clearly real" and NOT good enough to treat as exact. A marker absent from this
# table gets no RCV and its delta is reported without any significance claim, which is
# the correct failure mode: silence rather than a fabricated threshold.
#
# (marker) -> (CV_intra_individual_%, CV_analytical_%)
BIOLOGICAL_CV = {
    # liver
    "alt": (19.4, 5.0), "ast": (12.3, 5.0), "alp": (6.4, 4.0), "ggt": (13.4, 5.0),
    "bilirubin_total": (21.8, 5.0), "albumin": (3.1, 2.5), "total_protein": (2.7, 2.0),
    # lipids
    "total_cholesterol": (5.9, 2.6), "ldl_c": (8.2, 4.0), "hdl_c": (7.1, 3.4),
    "triglycerides": (19.9, 5.0), "vldl_c": (20.0, 5.0), "apob": (6.9, 3.0),
    "lp_a": (8.0, 5.0),
    # metabolic
    "fasting_glucose": (5.7, 2.2), "hba1c": (1.9, 2.0), "fasting_insulin": (21.1, 5.0),
    # renal / electrolytes
    "creatinine": (5.3, 2.7), "bun": (12.3, 4.0), "egfr": (5.3, 2.7),
    "uric_acid": (8.6, 3.0),
    "sodium": (0.6, 0.9), "potassium": (4.6, 2.0), "chloride": (1.2, 1.5),
    "co2": (4.8, 2.0), "calcium": (1.9, 1.7), "magnesium": (3.6, 2.5),
    # CBC
    "hemoglobin": (2.8, 1.5), "hematocrit": (2.8, 1.5), "rbc": (3.2, 1.5),
    "wbc": (10.9, 3.0), "platelets": (9.1, 4.0),
    "mcv": (1.3, 1.0), "mch": (1.4, 1.0), "mchc": (1.7, 1.2), "rdw": (3.5, 2.0),
    "neutrophils": (16.0, 5.0), "lymphocytes": (10.9, 5.0), "monocytes": (17.8, 5.0),
    "eosinophils": (21.0, 6.0), "basophils": (24.0, 8.0),
    # Absolute counts take the same estimates (approximate; the published figures are
    # for counts, so they fit these at least as well as the % rows above).
    "neutrophils_abs": (16.0, 5.0), "lymphocytes_abs": (10.9, 5.0),
    "monocytes_abs": (17.8, 5.0), "eosinophils_abs": (21.0, 6.0),
    "basophils_abs": (24.0, 8.0),
    # endocrine / inflammation / stores
    "tsh": (19.3, 5.0), "hs_crp": (42.2, 5.0), "ferritin": (14.2, 5.0),
    "vitamin_d": (12.1, 5.0), "testosterone_total": (9.3, 5.0),
}
_Z_SQRT2 = 2.77          # 1.96 * sqrt(2), two-sided, p < 0.05


def rcv_pct(marker: Optional[str]) -> Optional[float]:
    """Percent change a repeat draw must exceed to be distinguishable from noise."""
    cv = BIOLOGICAL_CV.get((marker or "").strip().lower())
    if cv is None:
        return None
    cv_i, cv_a = cv
    return round(_Z_SQRT2 * ((cv_i ** 2 + cv_a ** 2) ** 0.5), 1)


def classify_delta(marker: Optional[str], old: Optional[float],
                   new: Optional[float]) -> dict:
    """Is the change between two draws real, and is it good?

    `significant` is None — not False — when the marker has no published CV. An
    unknown threshold is not the same as a delta that failed to clear one, and the
    UI must be able to tell those apart.
    """
    out = {"pct": None, "rcv_pct": rcv_pct(marker), "significant": None,
           "trend": None}
    if old in (None, 0) or new is None:
        return out
    pct = (new - old) / abs(old) * 100.0
    out["pct"] = round(pct, 1)
    if out["rcv_pct"] is None:
        return out

    out["significant"] = abs(pct) > out["rcv_pct"]
    if out["significant"]:
        d = direction(marker)
        if d == HIGHER_BETTER:
            out["trend"] = "better" if pct > 0 else "worse"
        elif d == LOWER_BETTER:
            out["trend"] = "worse" if pct > 0 else "better"
        # two_sided: a real move, but "better"/"worse" isn't well defined
    return out


def assess(marker: Optional[str], value: Optional[float],
           ref_low: Optional[float], ref_high: Optional[float]) -> dict:
    """Where a value sits against its range, and whether that warrants attention.

    `position` is the plain fact (in / above / below). `concern` applies direction:
    it is True only when the value is outside the range on the side that actually
    matters. `favorable` marks the opposite case — outside the range in the good
    direction — so the UI can say "above range (good)" instead of staying silent
    and looking like it missed something.
    """
    d = direction(marker)
    out = {"direction": d, "position": "unknown", "concern": False, "favorable": False,
           "severity": None, "excess_pct": None}
    if value is None:
        return out

    above = ref_high is not None and value > ref_high
    below = ref_low is not None and value < ref_low
    if not above and not below:
        out["position"] = "in"
        return out

    out["position"] = "above" if above else "below"
    if d == HIGHER_BETTER:
        out["concern"], out["favorable"] = below, above
    elif d == LOWER_BETTER:
        out["concern"], out["favorable"] = above, below
    else:
        out["concern"] = True

    # HOW FAR outside, not merely whether. Crossing a reference limit by 12% was
    # rendering identically to a value three times the limit — the same red ▲ and
    # the same word "high" — which reads as an emergency for what is usually a
    # nothing. Clinicians triage transaminases by multiples of the upper limit
    # (<2x mild, 2-5x moderate, >5x urgent); expressing the overage as a percentage
    # of the bound generalises that to any analyte.
    bound = ref_high if above else ref_low
    if out["concern"] and bound:
        out["excess_pct"] = round(abs(value - bound) / abs(bound) * 100.0, 1)

        # Band on the FOLD difference from the bound, not the percentage. A
        # percentage is asymmetric: below the bound it can never exceed 100% (the
        # value would have to go negative), so "marked" was unreachable on the low
        # side — WBC 1.0 against a floor of 5.0 is severe neutropenia and scored
        # "moderate". A fold ratio is symmetric and is also how clinicians already
        # think: <2x, 2-5x, >5x the limit.
        if above:
            fold = value / bound if bound else None
        else:
            fold = bound / value if value else float("inf")
        if fold is not None:
            out["severity"] = ("mild" if fold < 1.25
                               else "moderate" if fold < 2.0
                               else "marked")
    return out


# ── White-cell differential: absolute counts (2026-10-28) ─────────────────────
#
# A differential reported as PERCENTAGES is a composition: the five must sum to ~100,
# so one cell type falling pushes another up. On one real panel "neutrophils low" and
# "lymphocytes high" were largely the same fact counted twice, and both flagged "needs
# attention" while the absolute counts (WBC × %) sat inside every common adult interval. Clinicians read a differential
# by its ABSOLUTE counts. So when the absolute count is available — reported by the lab,
# or derived as WBC × % from the SAME draw — it carries the range judgement, and the
# percentage is shown but never flagged (`read_as` names the row to read instead).
#
# The lab's panels here print no absolute ranges, so derived rows carry the WIDEST of
# the common adult intervals (Labcorp / Quest, from memory — approximate, deliberately
# lenient). That is the app's number, not the lab's: `derived` says so, and a wide
# interval keeps it from manufacturing flags the lab's own report would not raise.
#
# pct key -> (absolute key, display label, (ref_low, ref_high) in 10³/µL)
ABS_DIFFERENTIAL = {
    "neutrophils": ("neutrophils_abs", "Neut (abs)", (1.4, 7.8)),
    "lymphocytes": ("lymphocytes_abs", "Lymph (abs)", (0.7, 3.9)),
    "monocytes": ("monocytes_abs", "Mono (abs)", (0.1, 0.95)),
    "eosinophils": ("eosinophils_abs", "Eos (abs)", (0.0, 0.5)),
    "basophils": ("basophils_abs", "Baso (abs)", (0.0, 0.2)),
}


def _is_pct(unit: Optional[str]) -> bool:
    return (unit or "").strip() == "%"


def _wbc_thousands(value: Optional[float]) -> Optional[float]:
    """WBC in 10³/µL. Every panel here prints it that way (4.3); a lab printing cells/µL
    (4300) is rescaled rather than producing a 1,000x absolute count."""
    if value is None or value <= 0:
        return None
    return value / 1000.0 if value > 200 else value


def with_absolute_counts(rows: list[dict]) -> list[dict]:
    """Rows (dicts with date, marker, label, value, unit, ref_low, ref_high) plus a
    derived absolute-count row per % differential with a same-date WBC and no
    lab-reported absolute. Derived rows have `derived: True` and id None."""
    by_date: dict = {}
    for r in rows:
        by_date.setdefault(r["date"], {})[r["marker"]] = r
    out = list(rows)
    for d, panel in by_date.items():
        wbc = _wbc_thousands((panel.get("wbc") or {}).get("value"))
        if wbc is None:
            continue
        for pct_key, (abs_key, label, (lo, hi)) in ABS_DIFFERENTIAL.items():
            p = panel.get(pct_key)
            if p is None or not _is_pct(p.get("unit")) or p.get("value") is None \
                    or abs_key in panel:
                continue
            out.append({"id": None, "date": d, "marker": abs_key, "label": label,
                        "value": round(wbc * p["value"] / 100.0, 2), "unit": "10³/µL",
                        "ref_low": lo, "ref_high": hi, "derived": True})
    return out


def read_as(marker: Optional[str], unit: Optional[str], panel_markers) -> Optional[str]:
    """For a % differential row whose absolute count exists in the same draw (lab or
    derived), the absolute row's key — the row that carries the judgement. Else None."""
    key = (marker or "").strip().lower()
    spec = ABS_DIFFERENTIAL.get(key)
    if spec is None or not _is_pct(unit):
        return None
    return spec[0] if spec[0] in panel_markers else None


def assess_in_panel(marker: Optional[str], value: Optional[float], unit: Optional[str],
                    ref_low: Optional[float], ref_high: Optional[float],
                    panel_markers) -> dict:
    """`assess()` plus the differential rule: a % row whose absolute count is in the
    panel keeps its `position` (the plain fact) but never `concern`."""
    a = assess(marker, value, ref_low, ref_high)
    ra = read_as(marker, unit, panel_markers)
    a["read_as"] = ra
    if ra:
        a.update(concern=False, favorable=False, severity=None, excess_pct=None)
    return a
