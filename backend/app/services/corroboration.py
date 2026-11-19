"""Cross-family corroboration of correlation findings.

The app runs several *independent statistical families*, each with its own
within-family multiple-comparisons control (BH FDR is applied per family, not
globally): correlations (day-to-day covariation at fixed lag), temporal trends
(metric vs time), and changepoints (steps in detrended residuals). A finding
that survives its own family's FDR can still be a leak; agreement across
independent families is far less likely under the global null. This module
cross-references them, per confirmed/suggestive correlation pair:

  1. TREND AGREEMENT — if both the input and the outcome are trending, does the
     outcome move the way the correlation's sign predicts? WEAK evidence on its
     own (any co-trending pair "agrees"; seasonality confounds it) — supporting
     context, not proof. Disagreement, however, is genuinely informative.
  2. CHANGEPOINT CONCORDANCE — both series have a detected step within
     CP_MAX_GAP_DAYS of each other, in sign-consistent directions. Temporal
     specificity is hard to get by co-trending alone — but see the split test
     below, because concordance ALONE does not mean what it looks like.
  3. INTERVENTION CONTEXT — a user-flagged intervention near either changepoint
     is surfaced as an annotation (no new statistics; the ITS endpoint owns the
     rigorous before/after test).

## The split test, and why concordance needed one (2026-08-25)

Co-timed steps in the input and the outcome are EQUALLY consistent with two
very different worlds:

    (a) the input drives the outcome, and both stepped because the input did;
    (b) some third thing stepped both, and the input did nothing.

This module used to score (a) and (b) identically as `corroborated`, which
meant a confounded pair was *upgraded* by the very check meant to scrutinise
it. That is backwards, and it fires precisely when it matters most: a change
in life circumstances shifts several series at once, and every pair among them
gets promoted together.

What separates them is whether the association survives WITHIN each era. If
the input really drives the outcome, day-to-day covariation should still be
there on either side of the step. If the pooled rho was manufactured by two
series stepping at the same moment, splitting at the changepoint collapses it.
So on a concordant pair we recompute Spearman rho in the pre- and post-step
segments and classify:

    survives    — a segment with enough n keeps the sign and most of |rho|.
                  The step is not doing the work. Genuine corroboration.
    step_driven — every segment with enough n loses most of |rho|. The pooled
                  coefficient IS the step. Verdict becomes "confounded".
    ambiguous   — no segment has enough n to judge. Contributes NOTHING, in
                  either direction: it is neither evidence for nor against.

Deliberately NOT dependent on the user declaring anything. An explicit era
marker would make this easier, but the regime changes worth catching are
often the ones nobody wants to type into a health app, and a check that only
works when it is told about the confound would not have caught the case that
prompted it.

Verdict: "confounded" (a concordance is step-driven — read with suspicion),
"mixed" (some family disagrees), "corroborated" (>=1 family agrees, none
disagrees), "correlation-only" (no other family has data/signal).
"""
import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Intervention
from app.services.correlations import read_correlation_cache
from app.services.trends import compute_trends
from app.timeutils import local_today

logger = logging.getLogger(__name__)

# Max days between input and outcome changepoints to call them concordant
CP_MAX_GAP_DAYS = 14
# Max days between an intervention and a changepoint to annotate it
IV_MAX_GAP_DAYS = 10

# ── Split-test thresholds (see the module docstring) ──────────────────────────
# Aligned observations a segment needs before its rho is allowed to speak. Below
# this the segment is silent rather than counted as a collapse — a short segment
# has a small rho by construction, and reading that as "confounded" would be the
# same class of error this test exists to fix.
SEGMENT_MIN_N = 15
# Fraction of the pooled |rho| a segment must retain to count as surviving. A
# heuristic, not a test: the honest statement is "the association is still
# visibly there inside an era", and 0.5 is where that stops being arguable.
# Deliberately not a p-value — segments are short, so a significance test would
# report "not significant" for almost everything and read as confounding.
SEGMENT_RETAIN_FRAC = 0.5

# correlation metric key → trends registry key. Only metrics present in both
# families can be cross-referenced; z-score outcomes map to their raw-value
# trend (sign-compatible: z is monotone in the raw value against a slowly
# moving baseline, so *direction* agreement is meaningful).
_TREND_KEY = {
    "hrv_z_score":            "hrv",
    "rhr_z_score":            "rhr",
    "respiratory_rate":       "resp_rate",
    "walking_hr":             "walking_hr",
    "breathing_disturbances": "breathing",
    "sleep_total_h":          "sleep_hours",
    "steps":                  "steps",
    "exercise_minutes":       "exercise_minutes",
    "time_in_daylight":       "time_in_daylight",
}


def _segment_rho(series: dict, metric_a: str, metric_b: str, lag: int,
                 lo: date | None, hi: date | None) -> tuple[float | None, int]:
    """Spearman rho for one pair restricted to days in [lo, hi). (rho, n)."""
    from scipy import stats

    from app.services.correlations import _build_pairs

    sub = {d: v for d, v in series.items()
           if (lo is None or d >= lo) and (hi is None or d < hi)}
    xs, ys = _build_pairs(sub, metric_a, metric_b, lag)
    if len(xs) < SEGMENT_MIN_N or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None, len(xs)
    r, _p = stats.spearmanr(xs, ys)
    return (None if r != r else float(r)), len(xs)   # r != r catches NaN


def _split_test(series: dict, c: dict, cut: date) -> dict:
    """Does the pair's association survive inside each era, or IS it the step?

    See the module docstring. Returns the segment coefficients plus a verdict of
    survives / step_driven / ambiguous.
    """
    pooled = abs(c["spearman_r"])
    keep = SEGMENT_RETAIN_FRAC * pooled
    rho_pre, n_pre = _segment_rho(series, c["metric_a"], c["metric_b"],
                                  c["lag_days"], None, cut)
    rho_post, n_post = _segment_rho(series, c["metric_a"], c["metric_b"],
                                    c["lag_days"], cut, None)

    judged = [r for r in (rho_pre, rho_post) if r is not None]
    if not judged:
        verdict = "ambiguous"
    elif any(abs(r) >= keep and (r > 0) == (c["spearman_r"] > 0) for r in judged):
        verdict = "survives"
    else:
        verdict = "step_driven"

    return {
        "verdict":  verdict,
        "cut":      cut.isoformat(),
        "rho_pre":  round(rho_pre, 3) if rho_pre is not None else None,
        "n_pre":    n_pre,
        "rho_post": round(rho_post, 3) if rho_post is not None else None,
        "n_post":   n_post,
        "pooled_rho": c["spearman_r"],
        "retain_threshold": round(keep, 3),
    }


def _expected_outcome_direction(input_direction: str, rho: float) -> str:
    if rho > 0:
        return input_direction
    return "falling" if input_direction == "rising" else "rising"


async def build_corroboration(db: AsyncSession, days: int = 180) -> dict:
    """Cross-reference confirmed/suggestive correlation pairs against trends,
    changepoints and flagged interventions. Returns {days, results: [...]}."""
    corr = await read_correlation_cache()
    pairs = [c for c in corr if c.get("significant") or c.get("suggestive")]
    if not pairs:
        return {"days": days, "results": []}

    trends = {t["key"]: t for t in await compute_trends(db, days=days)}
    interventions = (await db.execute(select(Intervention))).scalars().all()

    # Day series for the split test, fetched lazily and at most once: it is the
    # expensive call in the correlation stack (~3 s) and most reads have no
    # concordant changepoint to test.
    _series_cache: dict = {}

    async def _series() -> dict:
        if "s" not in _series_cache:
            from datetime import timedelta

            from app.services.correlations import _fetch_daily_series
            end = local_today()
            s, _raw = await _fetch_daily_series(db, end - timedelta(days=days), end)
            _series_cache["s"] = s
        return _series_cache["s"]

    results = []
    for c in pairs:
        rho = c.get("spearman_r")
        if rho is None:
            continue
        t_in = trends.get(_TREND_KEY.get(c["metric_a"], ""))
        t_out = trends.get(_TREND_KEY.get(c["metric_b"], ""))

        # ── 1. trend agreement ───────────────────────────────────────────────
        trend_ev = None
        if (t_in and t_out
                and t_in["direction"] != "steady" and t_out["direction"] != "steady"):
            expected = _expected_outcome_direction(t_in["direction"], rho)
            trend_ev = {
                "input_direction":    t_in["direction"],
                "outcome_direction":  t_out["direction"],
                "expected_direction": expected,
                "agrees":             t_out["direction"] == expected,
            }

        # ── 2. changepoint concordance ───────────────────────────────────────
        cp_ev = None
        cp_in = t_in.get("changepoint") if t_in else None
        cp_out = t_out.get("changepoint") if t_out else None
        if cp_in and cp_out:
            d_in = date.fromisoformat(cp_in["date"])
            d_out = date.fromisoformat(cp_out["date"])
            gap = abs((d_out - d_in).days)
            if gap <= CP_MAX_GAP_DAYS:
                same_shift = cp_in["shift"] == cp_out["shift"]
                consistent = same_shift if rho > 0 else not same_shift
                cp_ev = {
                    "input_date":   cp_in["date"],
                    "outcome_date": cp_out["date"],
                    "gap_days":     gap,
                    "consistent":   consistent,
                }
                # Concordance alone cannot tell "the input drove the outcome"
                # from "a third thing stepped both". Split at the earlier of the
                # two steps — that is where the regime actually changes — and
                # ask whether the association is still there inside each era.
                if consistent:
                    cp_ev["split"] = _split_test(
                        await _series(), c, min(d_in, d_out))

        # ── 3. intervention annotation (context only, no stats) ─────────────
        iv_ev = None
        for iv in interventions:
            for cp, which in ((cp_in, "input"), (cp_out, "outcome")):
                if cp is None:
                    continue
                if abs((date.fromisoformat(cp["date"]) - iv.date).days) <= IV_MAX_GAP_DAYS:
                    iv_ev = {
                        "label": iv.label,
                        "date":  iv.date.isoformat(),
                        "near":  f"{which} step change",
                    }
                    break
            if iv_ev:
                break

        # A concordance only counts as agreement once the split test says the
        # association survives inside an era. `step_driven` is not neutral — it
        # is positive evidence that the coefficient IS the step — so it takes
        # over the verdict. `ambiguous` contributes nothing either way.
        split = (cp_ev or {}).get("split", {}).get("verdict")
        cp_agrees = None
        if cp_ev is not None:
            if not cp_ev["consistent"]:
                cp_agrees = False           # steps disagree in sign — informative
            elif split == "survives":
                cp_agrees = True
            # step_driven / ambiguous / untested → no vote

        agrees = [ev for ev in (
            trend_ev["agrees"] if trend_ev else None,
            cp_agrees,
        ) if ev is not None]
        n_agree = sum(1 for a in agrees if a)
        n_disagree = sum(1 for a in agrees if not a)
        if split == "step_driven":
            verdict = "confounded"
        elif n_disagree:
            verdict = "mixed"
        elif n_agree:
            verdict = "corroborated"
        else:
            verdict = "correlation-only"

        results.append({
            "input":          c["metric_a"],
            "input_label":    c["metric_a_label"],
            "outcome":        c["metric_b"],
            "outcome_label":  c["metric_b_label"],
            "lag_days":       c["lag_days"],
            "spearman_r":     rho,
            "n":              c["n"],
            "n_eff":          c["n_eff"],
            "corr_tier":      "confirmed" if c.get("significant") else "suggestive",
            "trend":          trend_ev,
            "changepoint":    cp_ev,
            "intervention":   iv_ev,
            "verdict":        verdict,
        })

    # Confounded first — a pair whose coefficient is a step change is the most
    # actionable thing on this page, and it used to be sorted to the TOP as
    # "corroborated" for the same reason it now sorts to the top as a warning.
    # Then corroborated, then mixed, then correlation-only; strongest |ρ| within
    # each group.
    order = {"confounded": 0, "corroborated": 1, "mixed": 2, "correlation-only": 3}
    results.sort(key=lambda r: (order[r["verdict"]], -abs(r["spearman_r"])))
    return {"days": days, "results": results}
