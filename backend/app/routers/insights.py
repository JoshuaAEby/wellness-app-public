"""Wellness insights: on-demand and cached correlation analysis."""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.timeutils import local_today

router = APIRouter(prefix="/api/insights", tags=["insights"])


@router.get("/families")
async def get_input_families(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Input families Findings groups as one finding, plus the weekly data check:
    members that stopped tracking their family, and ungrouped inputs that move together."""
    from app.services.correlations import family_check
    return await family_check(db)


@router.get("/trends")
async def get_trends(
    days: int = Query(default=365, ge=21, le=365),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """
    Temporal trend detection per metric ("what's moving") — Theil–Sen slope +
    autocorrelation-corrected Mann–Kendall + BH FDR. Distinct from correlations
    (metric-vs-time, not metric-vs-metric). Ranked movers first.
    """
    from app.services.trends import compute_trends
    results = await compute_trends(db, days=days)
    return {"days": days, "results": results}


@router.get("/typical")
async def get_typical(
    start: date | None = None,
    end: date | None = None,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Typical values (note 86): mean ± SD, median, IQR per daily variable over the
    window, on the correlation frame's normal days. `end` defaults to and is capped at
    yesterday — today is still accumulating."""
    from app.services.typical_values import typical_values
    yesterday = local_today() - timedelta(days=1)
    end = min(end or yesterday, yesterday)
    start = start or end - timedelta(days=89)
    if (end - start).days < 6:
        start = end - timedelta(days=6)
    return await typical_values(db, start, end)


@router.get("/validation")
async def get_validation(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """
    Prospective (holdout) validation + negative-control status.

    Each ledgered finding is re-tested on ONLY the data accumulated after it was
    first selected — a genuine holdout untouched by the discovery process. The
    negative controls are null-by-construction inputs run through the full
    pipeline; any control reaching suggestive/confirmed means the FDR machinery
    is leaking.
    """
    from app.services.correlations import build_validation
    return await build_validation(db)


@router.get("/corroboration")
async def get_corroboration(
    days: int = Query(default=365, ge=21, le=365),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """
    Cross-family corroboration: for each confirmed/suggestive correlation pair,
    check whether the independent trend / changepoint / intervention analyses
    point the same way. BH is applied per family, so multi-family agreement is
    much stronger evidence than any single family's FDR-passing finding —
    while a "mixed" verdict flags findings that deserve suspicion.
    """
    from app.services.corroboration import build_corroboration
    return await build_corroboration(db, days=days)


@router.get("/correlations")
async def get_correlations(
    days: int = Query(default=365, ge=14, le=365),
    use_cache: bool = Query(default=True, description="Return cached results if available; set false to force live recompute"),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """
    Return cross-metric Pearson correlations.

    - `use_cache=true` (default): returns the weekly-refreshed batch cache (instant).
      Falls back to live computation if cache is empty.
    - `use_cache=false`: always recomputes live over the requested `days` window.
    """
    from app.services.correlations import compute_correlations, read_correlation_cache

    if use_cache:
        cached = await read_correlation_cache()
        if cached:
            return {"source": "cache", "results": cached}

    # Live computation (or cache miss)
    results = await compute_correlations(db, days=days)
    return {"source": "live", "days_window": days, "results": results}


@router.post("/correlations/refresh")
async def refresh_correlations(
    days: int = Query(default=365, ge=14, le=365),
    _: str = Depends(get_current_user),
):
    """Manually trigger correlation cache refresh."""
    from app.services.correlations import refresh_correlation_cache
    await refresh_correlation_cache(days=days)
    return {"ok": True, "days_window": days}


@router.get("/threshold-bands")
async def get_threshold_bands(
    days: int = Query(default=365, ge=14, le=365),
    min_n: int = Query(default=30, ge=10),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """
    Return threshold bands for all currently confirmed correlation pairs.
    Each band splits the input into Low/Mid/High quantile thirds and reports
    the average outcome value per bin. Returns sufficient_data=False for pairs
    with fewer than min_n aligned observations.
    """
    from app.services.correlations import (
        SIGNIFICANCE_THRESHOLD, _SUBJECTIVE_OUTCOME_KEYS,
        compute_all_threshold_bands, read_correlation_cache,
    )
    cached = await read_correlation_cache()
    # Only subjective-outcome pairs — threshold bands are a dose-response framing
    # that makes sense for things you control (inputs) → how you feel (subjective
    # outcomes). Physiological outcomes (HRV, RHR, resp rate) live in Diagnostic
    # Signals and don't benefit from this presentation.
    confirmed_pairs = [
        (r["metric_a"], r["metric_b"], r["lag_days"])
        for r in cached
        if (r.get("significant") or r.get("suggestive"))
        and abs(r["spearman_r"] or 0) >= SIGNIFICANCE_THRESHOLD
        and r["n"] >= 30
        and r["metric_b"] in _SUBJECTIVE_OUTCOME_KEYS
    ]
    bands = await compute_all_threshold_bands(db, confirmed_pairs, days=days, min_n=min_n)
    return {"bands": bands}


@router.get("/stratified")
async def get_stratified_insights(
    stratum: str = Query(..., pattern="^(low|high)$",
                         description="'low' = readiness < 50, 'high' = readiness ≥ 75"),
    days: int = Query(default=365, ge=14, le=365),
    _: str = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Actionable insights filtered to days in a specific readiness stratum.
    Returns sufficient_data=False with empty lists when fewer than 30 stratum days exist.
    """
    from app.services.correlations import compute_stratified_actionable
    return await compute_stratified_actionable(db, stratum=stratum, days=days)


@router.get("/actionable")
async def get_actionable_insights(
    days: int = Query(default=365, ge=14, le=365),
    use_cache: bool = Query(default=True),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """
    Return correlations reframed as actionable do-more / do-less lists.
    Only input→outcome pairs with |r| ≥ ACTION_THRESHOLD (0.2) and n ≥ 30 are included,
    and routing is on whether the INPUT can be changed — not on whether the outcome is
    subjective. Inputs classified `none` in INPUT_CONTROL (weather, gait readouts) are
    the only ones that go to `diagnostic`.
    Uses cached correlation data when available.
    """
    from app.services.correlations import (
        ACTION_THRESHOLD, build_actionable_insights, compute_correlations,
        read_correlation_cache,
    )

    # n >= 30 still applies (it was added because low-n noise produced confusing
    # double-negative habit insights at n≈24). The |r| bar is ACTION_THRESHOLD, not
    # SIGNIFICANCE_THRESHOLD — see the note on those constants: this list is allowed to
    # carry small-but-reliable effects on cheap levers, the alerting path is not.
    # max_per_section=None (note 36): the lists must add up to the counts shown beside
    # them, and a silent cut at 12 made that impossible.
    kwargs = {"min_abs_r": ACTION_THRESHOLD, "min_n": 30, "max_per_section": None}

    if use_cache:
        cached = await read_correlation_cache()
        if cached:
            return build_actionable_insights(cached, **kwargs)

    results = await compute_correlations(db, days=days)
    return build_actionable_insights(results, **kwargs)


@router.get("/levers")
async def get_levers(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """The few inputs worth changing, ranked, with exposure and effect per unit
    (services/levers.py). The full lists stay in /actionable."""
    from app.services.levers import compute_levers
    return await compute_levers(db)


@router.get("/limitations")
async def get_limitations(
    weeks: int = Query(default=12, ge=4, le=52),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """
    Data-limitation context for the correlation engine.

    Returns:
      - checkin_compliance: per-week check-in rate for the last N weeks (sparkline data)
      - sub_threshold_pairs: pairs in insight_cache with n < 30 (actionable threshold),
        sorted by n desc (closest to crossing), up to 5. Reports current n vs required 30.
        No timeline projection — only the hard gap is stated.
      - limiter: identifies the main bottleneck ("morning check-in consistency" when most
        sub-threshold pairs have subjective outcomes; "time and data accumulation" otherwise)
    """
    from app.models import InsightCache, SubjectiveLog
    from app.services.correlations import METRIC_LABELS

    today = local_today()
    _SUBJECTIVE_KEYS = {"subj_energy", "subj_mood", "subj_sleep"}

    # ── Check-in compliance: per-week rate over last N weeks ─────────────────
    window_start = today - timedelta(weeks=weeks)
    subj_rows = (await db.execute(
        select(SubjectiveLog.date)
        .where(SubjectiveLog.date >= window_start)
        .where(SubjectiveLog.date <= today)
        .where(
            (SubjectiveLog.energy.isnot(None)) |
            (SubjectiveLog.mood.isnot(None)) |
            (SubjectiveLog.sleep_quality.isnot(None))
        )
    )).scalars().all()
    checkin_dates = set(subj_rows)

    # Build week-by-week compliance from oldest to newest
    first_monday = today - timedelta(days=today.weekday()) - timedelta(weeks=weeks - 1)
    compliance = []
    for i in range(weeks):
        w_start = first_monday + timedelta(weeks=i)
        w_end = min(w_start + timedelta(days=6), today)
        n_days = (w_end - w_start).days + 1
        n_logged = sum(1 for j in range(n_days) if (w_start + timedelta(days=j)) in checkin_dates)
        compliance.append({
            "week_start": w_start.isoformat(),
            "days_logged": n_logged,
            "days_total": n_days,
            "pct": round(n_logged / n_days * 100) if n_days > 0 else 0,
        })

    # ── Sub-threshold pairs from insight_cache ────────────────────────────────
    cache_rows = (await db.execute(select(InsightCache))).scalars().all()
    sub = [r for r in cache_rows if r.n is not None and r.n < 30]
    sub.sort(key=lambda r: -(r.n or 0))  # highest n first (closest to threshold)

    sub_threshold_pairs = [
        {
            "label_a":    METRIC_LABELS.get(r.metric_a, r.metric_a),
            "label_b":    METRIC_LABELS.get(r.metric_b, r.metric_b),
            "lag_days":   r.lag_days,
            "n_current":  r.n,
            "n_required": 30,
        }
        for r in sub[:5]
    ]

    # ── Limiter: dominant bottleneck ─────────────────────────────────────────
    subj_count = sum(1 for r in sub if r.metric_b in _SUBJECTIVE_KEYS)
    limiter = (
        "morning check-in consistency"
        if len(sub) > 0 and subj_count / len(sub) > 0.5
        else "time and data accumulation"
    )

    return {
        "checkin_compliance": compliance,
        "sub_threshold_pairs": sub_threshold_pairs,
        "n_sub_threshold": len(sub),
        "limiter": limiter,
    }


@router.get("/metric-coverage")
async def get_metric_coverage(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """When each recently-added input started, so low n can be read correctly.

    n=9 on an eight-day-old metric is "not old enough to ask yet"; n=9 after a year is
    "you rarely do this". Findings cannot distinguish them without this.
    """
    from app.services.correlations import metric_coverage
    return await metric_coverage(db)
