"""Workouts, RPE, HR zones, workout-type contrast, HR recovery, anchor lifts, ACWR forecast.

Part of the /api/metrics package (split out of routers/metrics.py 2026-10-21)."""
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import HealthMetric, UserSettings, Workout
from app.services.hr_zones import zone_bounds, zone_index
from app.timeutils import local_date_sql
from app.routers.metrics._common import _DOW_OUTCOME_LABELS, _group_stats, _outcome_points, local_date_sql_value

router = APIRouter()


@router.get("/workouts")
async def get_workouts(
    limit: int = Query(default=20, le=1000),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    from app.workout_categories import classify_gym_session, gym_session_agreement

    result = await db.execute(
        select(Workout).order_by(Workout.start_time.desc()).limit(limit)
    )
    rows = result.scalars().all()
    # The gym's own class name, from Hevy (note 31, 2026-09-14). Validated as the best
    # label available: across 58 clearly-titled sessions it agrees with the HR split 86%
    # of the time, and it is where the disagreements get explained ("Core and
    # conditioning" barely raises HR). Display only — the correlation inputs are
    # continuous (hard_minutes, tonnage), not labels.
    from app.models import HevyWorkout
    uuids = [w.apple_uuid for w in rows if w.apple_uuid]
    hevy_titles = dict((await db.execute(
        select(HevyWorkout.apple_uuid, HevyWorkout.title).where(HevyWorkout.apple_uuid.in_(uuids))
    )).all()) if uuids else {}
    return [
        {
            "id": w.id,
            "apple_uuid": w.apple_uuid,
            "activity_type": w.activity_type,
            "start_time": w.start_time.isoformat(),
            "end_time": w.end_time.isoformat() if w.end_time else None,
            "duration_sec": w.duration_sec,
            "active_calories": w.active_calories,
            "avg_hr": w.avg_hr,
            "max_hr": w.max_hr,
            "rpe": w.rpe,
            "trimp": w.trimp,
            # The gym labels every class "HIIT"; `session_kind` is what it actually was,
            # derived from heart rate. None = unclassifiable (no HR), never a default
            # bucket. `high_intensity_min` rides along so the UI can show the evidence
            # rather than asking the reader to trust a label.
            "session_kind": classify_gym_session(w.activity_type, w.high_intensity_min),
            "high_intensity_min": w.high_intensity_min,
            "label_agrees_with_hr": gym_session_agreement(w.activity_type, w.high_intensity_min),
            "hevy_title": hevy_titles.get(w.apple_uuid),
        }
        for w in rows
    ]


class RpeIn(BaseModel):
    rpe: Optional[float] = Field(default=None, ge=1, le=10)


@router.patch("/workouts/{workout_id}/rpe")
async def set_workout_rpe(
    workout_id: int,
    body: RpeIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    result = await db.execute(select(Workout).where(Workout.id == workout_id))
    w = result.scalar_one_or_none()
    if w is None:
        raise HTTPException(status_code=404, detail="Workout not found")
    w.rpe = body.rpe
    await db.commit()
    return {
        "id": w.id,
        "activity_type": w.activity_type,
        "start_time": w.start_time.isoformat(),
        "rpe": w.rpe,
    }


@router.get("/workouts/{workout_id}/detail")
async def get_workout_detail(
    workout_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Return workout details plus HR time series (30s buckets) and zone breakdown."""
    from app.models import HealthMetric

    result = await db.execute(select(Workout).where(Workout.id == workout_id))
    w = result.scalar_one_or_none()
    if w is None:
        raise HTTPException(status_code=404, detail="Workout not found")

    hr_series = []
    hr_zones = {"z1_min": 0, "z2_min": 0, "z3_min": 0, "z4_min": 0, "z5_min": 0}

    # Shared %HRR bounds (services/hr_zones) — same source metrics_engine and the
    # training-zones endpoint use.
    _zs = (await db.execute(select(UserSettings).limit(1))).scalar_one_or_none()
    _zb = zone_bounds(_zs.max_hr if _zs else None, _zs.hrr_rest_ref if _zs else None)

    if w.start_time and w.end_time:
        hr_rows = (await db.execute(
            select(HealthMetric)
            .where(HealthMetric.metric_type == "heart_rate")
            .where(HealthMetric.timestamp >= w.start_time)
            .where(HealthMetric.timestamp <= w.end_time)
            .order_by(HealthMetric.timestamp)
        )).scalars().all()

        if hr_rows:
            # Bucket into 30-second intervals, express time as minutes from start
            buckets: dict = {}
            for row in hr_rows:
                elapsed = (row.timestamp - w.start_time).total_seconds()
                bucket = int(elapsed // 30) * 30
                buckets.setdefault(bucket, []).append(row.value)

            hr_series = [
                {"t": round(k / 60, 2), "bpm": round(sum(v) / len(v))}
                for k, v in sorted(buckets.items())
            ]

            # Zone distribution: prefer the values metrics_engine already stored on the
            # workout. They are computed per-SAMPLE with the shared %HRR bounds; this
            # endpoint used to re-derive them from the 30 s averaged chart series with
            # its own hardcoded bands (a THIRD copy of the constants), which both
            # disagreed with the rest of the app and lost resolution — averaging bpm
            # into 30 s buckets before bucketing biases samples toward the middle zone.
            if w.zone1_min is not None:
                hr_zones = {
                    "z1_min": round(w.zone1_min or 0.0, 1),
                    "z2_min": round(w.zone2_min or 0.0, 1),
                    "z3_min": round(w.zone3_min or 0.0, 1),
                    "z4_min": round(w.zone4_min or 0.0, 1),
                    "z5_min": round(w.zone5_min or 0.0, 1),
                }
            else:
                # Not yet recomputed — derive from the RAW samples (not the chart
                # series) using the same shared bounds.
                zone_seconds = [0.0] * 5
                for i in range(len(hr_rows) - 1):
                    dt = min((hr_rows[i + 1].timestamp - hr_rows[i].timestamp).total_seconds(), 30.0)
                    zone_seconds[zone_index(hr_rows[i].value, _zb)] += dt
                hr_zones = {f"z{i+1}_min": round(sec / 60, 1)
                            for i, sec in enumerate(zone_seconds)}

    return {
        # Bounds travel with the payload so the UI can LABEL the ranges instead of
        # hardcoding them (WorkoutDetailModal used to print "< 115", "115-135", ...).
        "zone_bounds": {"z2": round(_zb[0]), "z3": round(_zb[1]),
                        "z4": round(_zb[2]), "z5": round(_zb[3])},
        "workout": {
            "id": w.id,
            "activity_type": w.activity_type,
            "start_time": w.start_time.isoformat(),
            "end_time": w.end_time.isoformat() if w.end_time else None,
            "duration_sec": w.duration_sec,
            "active_calories": w.active_calories,
            "avg_hr": w.avg_hr,
            "max_hr": w.max_hr,
            "rpe": w.rpe,
            "trimp": w.trimp,
        },
        "hr_series": hr_series,
        "hr_zones": hr_zones,
    }


@router.get("/training-zones")
async def get_training_zones(
    days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """
    Aggregate HR zone time across all workouts in the past `days` days.
    Returns Low Aerobic / High Aerobic / Anaerobic breakdowns with minutes and percentages.
    Uses two DB queries (workouts + HR data) joined in Python.
    """
    import bisect

    end_dt = datetime.utcnow()
    start_dt = end_dt - timedelta(days=days)

    wo_result = await db.execute(
        select(Workout)
        .where(Workout.start_time >= start_dt)
        .where(Workout.start_time <= end_dt)
        .where(Workout.duration_sec.isnot(None))
        .order_by(Workout.start_time)
    )
    workouts_list = wo_result.scalars().all()

    if not workouts_list:
        return {"days": days, "total_min": 0, "zones": [], "focus": None, "focus_description": None}

    # Expand workout windows slightly (+60s buffer for late HR samples)
    windows = sorted([
        (w.start_time, w.start_time + timedelta(seconds=w.duration_sec + 60))
        for w in workouts_list
    ])
    window_starts = [w[0] for w in windows]

    hr_result = await db.execute(
        select(HealthMetric)
        .where(HealthMetric.metric_type == "heart_rate")
        .where(HealthMetric.timestamp >= start_dt)
        .where(HealthMetric.timestamp <= end_dt)
        .order_by(HealthMetric.timestamp)
    )
    all_hr = hr_result.scalars().all()

    # Boundaries from services/hr_zones — the SAME source metrics_engine uses. These
    # were a second hardcoded copy (115/135/155/170) until 2026-08-08; two copies of
    # the same constants is how they silently diverge.
    _zs = (await db.execute(select(UserSettings).limit(1))).scalar_one_or_none()
    _zb = zone_bounds(_zs.max_hr if _zs else None, _zs.hrr_rest_ref if _zs else None)
    zone_secs = [0.0] * 5  # Z1..Z5 per _zb

    for i in range(len(all_hr) - 1):
        ts = all_hr[i].timestamp
        # Binary search: find the latest window starting at or before ts
        idx = bisect.bisect_right(window_starts, ts) - 1
        if idx < 0:
            continue
        _, w_end = windows[idx]
        if ts > w_end:
            continue  # not inside this workout window

        bpm = all_hr[i].value
        dt = min((all_hr[i + 1].timestamp - ts).total_seconds(), 30)
        zone_secs[zone_index(bpm, _zb)] += dt

    total = sum(zone_secs)
    if total == 0:
        return {"days": days, "total_min": 0, "zones": [], "focus": None, "focus_description": None}

    low_s  = zone_secs[0] + zone_secs[1]
    high_s = zone_secs[2]
    ana_s  = zone_secs[3] + zone_secs[4]

    FOCUS_DESC = {
        "Low Aerobic":  "Building aerobic base — good foundation for long-term fitness.",
        "High Aerobic": "Developing aerobic capacity and lactate threshold.",
        "Anaerobic":    "High intensity focus — ensure adequate recovery between sessions.",
    }

    zones = [
        {"label": "Anaerobic",    "color": "#ef4444", "min": round(ana_s  / 60), "pct": round(ana_s  / total * 100)},
        {"label": "High Aerobic", "color": "#f59e0b", "min": round(high_s / 60), "pct": round(high_s / total * 100)},
        {"label": "Low Aerobic",  "color": "#22c55e", "min": round(low_s  / 60), "pct": round(low_s  / total * 100)},
    ]
    dominant = max(zones, key=lambda z: z["pct"])

    return {
        "days": days,
        "total_min": round(total / 60),
        "zones": zones,
        "focus": dominant["label"] + " Focus",
        "focus_description": FOCUS_DESC.get(dominant["label"], ""),
    }


@router.get("/anchors")
async def get_anchor_lifts(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Anchor lifts: last session and the next double-progression target (note 56)."""
    from app.services.anchor_lifts import anchor_status
    return await anchor_status(db)


@router.get("/acwr-forecast")
async def get_acwr_forecast(
    horizon: int = Query(default=7, ge=7, le=28),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """ACWR now, plus a `horizon`-day projection under rest / maintain / build.

    Deterministic, not predictive: ATL and CTL are EWMAs, so the ratio's future is a
    pure function of today's pair and the loads you choose. Fails soft with
    `available: false` rather than erroring — the Training tab must still render.
    """
    import logging

    from app.services.load_forecast import acwr_forecast
    try:
        return await acwr_forecast(db, horizon=horizon)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("acwr forecast failed: %s", exc)
        return {"available": False, "reason": "forecast unavailable"}


# ── Workout-type contrast ─────────────────────────────────────────────────────

_WORKOUT_TYPE_LABELS = {
    "strength":  "Strength",
    "hiit":      "HIIT",
    "cardio":    "Cardio",
    "walk_only": "Walk only",
    "rest":      "Rest day",
}


@router.get("/workout-type-contrast")
async def get_workout_type_contrast(
    outcome: str = Query(default="subj_energy"),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """
    Descriptive next-morning contrast by workout type: for each category of
    workout done on day D, the outcome measured on day D+1 (matching the
    correlation engine's lag-1 convention for daytime behaviours).

    Groups OVERLAP: a day with both strength and HIIT counts in both groups
    (each group is "mornings after a day containing ≥1 workout of that type").
    walk_only = walk/low-intensity day with NO training workout; rest = no
    workout record at all (counted only from the first tracked workout onward,
    so pre-tracking history can't masquerade as rest days).

    FRAMING: exploratory descriptive means + naive 95% CIs, same policy as
    /day-of-week — NOT significance tests, and not confound-controlled beyond
    the fixed 1-day offset.
    """
    if outcome not in _DOW_OUTCOME_LABELS:
        raise HTTPException(
            status_code=400,
            detail=f"outcome must be one of: {sorted(_DOW_OUTCOME_LABELS)}",
        )

    points = await _outcome_points(db, outcome)

    # Morning outcomes are measured the day AFTER the workout (lag 1). Day quality
    # (subj_day) is rated the EVENING of the workout day itself → same-day (lag 0).
    lag = 0 if outcome == "subj_day" else 1

    from app.workout_categories import categorize_activity
    wo_rows = (await db.execute(
        select(local_date_sql(Workout.start_time).label("day"), Workout.activity_type)
    )).all()
    cats_by_day: dict[date, set[str]] = {}
    for r in wo_rows:
        d = date.fromisoformat(str(r.day))
        cat = categorize_activity(r.activity_type)
        if cat:
            cats_by_day.setdefault(d, set()).add(cat)
    if not cats_by_day:
        return {"outcome": outcome, "outcome_label": _DOW_OUTCOME_LABELS[outcome],
                "available": False, "groups": []}
    first_wo_day = min(cats_by_day)

    groups: dict[str, list[float]] = {k: [] for k in _WORKOUT_TYPE_LABELS}
    for morning, val in points.items():
        prev = morning - timedelta(days=lag)
        if prev < first_wo_day:
            continue  # pre-tracking era: can't distinguish rest from unrecorded
        cats = cats_by_day.get(prev)
        if not cats:
            groups["rest"].append(val)
            continue
        training = cats - {"walk"}
        for c in training:
            groups[c].append(val)
        if not training:
            groups["walk_only"].append(val)

    stats_by_group = [
        {"key": k, "label": _WORKOUT_TYPE_LABELS[k], **_group_stats(v)}
        for k, v in groups.items()
    ]
    shown = [g for g in stats_by_group if g["n"] > 0]
    return {
        "outcome":       outcome,
        "outcome_label": _DOW_OUTCOME_LABELS[outcome],
        "available":     sum(1 for g in shown if g["n"] >= 8) >= 2,
        "lag_note":      ("Rated the evening of the workout day itself." if lag == 0
                          else "Outcome measured the morning after the workout day."),
        "groups":        shown,
    }


@router.get("/hr-recovery")
async def get_hr_recovery(
    days: int = Query(default=365, ge=30, le=1095),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Per-session HR recovery, newest first, with the peak it recovered FROM.

    Peak travels with every point deliberately: HRR is not comparable across sessions
    without it — recovering from 180 bpm and from 130 bpm are different problems, and
    walks read low mostly because their peak is low, not because recovery is poor.
    """
    from statistics import median, quantiles

    from app.workout_categories import categorize_activity

    since = datetime.utcnow() - timedelta(days=days)
    rows = (await db.execute(
        select(Workout)
        .where(Workout.start_time >= since)
        .where(Workout.hr_recovery_60.isnot(None))
        .where(Workout.suspect.isnot(True))
        .order_by(Workout.start_time)
    )).scalars().all()

    out = [{
        "date": local_date_sql_value(w.start_time),
        "activity": w.activity_type,
        "category": categorize_activity(w.activity_type),
        "hr_peak": w.hr_peak,
        "hrr60": w.hr_recovery_60,
        "high_intensity_min": w.high_intensity_min,
    } for w in rows]

    # Grouped by category, because a cross-type median is the exact mistake the peak
    # column exists to prevent.
    by_cat: dict[str, list[float]] = {}
    for r in out:
        by_cat.setdefault(r["category"] or "other", []).append(r["hrr60"])
    summary = {k: {"n": len(v), "median": round(median(v), 1),
                   "iqr_low": round(quantiles(v, n=4)[0], 1) if len(v) >= 4 else None,
                   "iqr_high": round(quantiles(v, n=4)[2], 1) if len(v) >= 4 else None}
               for k, v in by_cat.items() if len(v) >= 3}

    fits, trends = _hrr_fits_and_trends(out)
    return {"sessions": out, "by_category": summary, "n_total": len(out),
            "fits": fits, "trends": trends}


# Minimum sessions in a category before a fit is drawn. Below this the slope is
# hostage to two or three points and a confident-looking line would be a lie.
HRR_MIN_FIT_N = 10


def _hrr_fits_and_trends(sessions: list[dict]) -> tuple[dict, dict]:
    """Per-category HRR~peak fit, and the time trend of the PEAK-ADJUSTED residual.

    Two separate questions, which the raw scatter conflates:

      1. "How does my recovery scale with how hard I went?"  → HRR vs peak HR.
         Theil-Sen rather than least squares: this series has genuine outliers
         (a handful of negative HRRs where HR rose after the watch stopped, and
         one-off runs far outside the usual peak range), and a median-of-slopes
         fit does not let three points define the line.

      2. "Am I getting fitter?"                              → residual vs TIME.
         Raw HRR over time is confounded by peak: an easier month reads as worse
         recovery. The residual from (1) is HRR with the peak effect removed, so
         its trend is the closest thing this data offers to a fitness signal.

    Both carry a 95% slope CI. Where the CI spans zero the answer is "no
    detectable trend", which is reported as such rather than dressed as a
    direction — with this much session-to-session spread that is the honest
    answer most of the time, and hiding it behind a line was the original
    complaint about this card.
    """
    from statistics import median

    from scipy.stats import theilslopes

    fits: dict[str, dict] = {}
    trends: dict[str, dict] = {}

    by_cat: dict[str, list[dict]] = {}
    for s in sessions:
        if s["hr_peak"] is not None and s["hrr60"] is not None:
            by_cat.setdefault(s["category"] or "other", []).append(s)

    for cat, rows in by_cat.items():
        if len(rows) < HRR_MIN_FIT_N:
            continue
        peaks = [r["hr_peak"] for r in rows]
        hrrs = [r["hrr60"] for r in rows]
        if len(set(peaks)) < 3:
            continue

        slope, intercept, lo, hi = theilslopes(hrrs, peaks, alpha=0.95)
        x0, x1 = min(peaks), max(peaks)
        # The CI envelope pivots about the fit's centroid, so the band is
        # narrowest where the data is and widens toward the extremes — which is
        # where a slope estimate is least trustworthy.
        xm = median(peaks)
        ym = slope * xm + intercept
        fits[cat] = {
            "n": len(rows),
            "slope": round(slope, 4),
            "slope_lo": round(lo, 4), "slope_hi": round(hi, 4),
            "flat": bool(lo <= 0 <= hi),
            "x0": x0, "x1": x1,
            "y0": round(slope * x0 + intercept, 1),
            "y1": round(slope * x1 + intercept, 1),
            # Keyed on `hr_peak` so it shares the chart's x-axis dataKey, with the
            # two bounds as separate scalars. A [lo, hi] range pair was tried first
            # and rendered as three empty <path d=""> elements: Recharts will not
            # compute range-area geometry from a per-series `data` prop. Two bound
            # lines are drawn instead — less pretty than a filled band, but they
            # actually appear.
            # min/max, not lo-slope/hi-slope: left of the pivot (x - xm) is negative,
            # so the shallow slope produces the HIGHER bound there and the envelope
            # crosses. Naming them by slope gave lo=8.7 above hi=1.2 at x0.
            "band": [
                {"hr_peak": x, "lo": round(min(a, b), 1), "hi": round(max(a, b), 1)}
                for x, a, b in (
                    (x0, ym + lo * (x0 - xm), ym + hi * (x0 - xm)),
                    (x1, ym + lo * (x1 - xm), ym + hi * (x1 - xm)),
                )
            ],
        }

        # Residual = HRR with the peak effect removed. Attached to each session so
        # the client can plot it without re-deriving the fit.
        pts = []
        for r in rows:
            resid = r["hrr60"] - (slope * r["hr_peak"] + intercept)
            r["hrr_adj"] = round(resid, 1)
            pts.append({"date": r["date"], "resid": round(resid, 1)})

        ords = [date.fromisoformat(p["date"]).toordinal() for p in pts]
        if len(set(ords)) >= 3:
            t_slope, t_int, t_lo, t_hi = theilslopes(
                [p["resid"] for p in pts], ords, alpha=0.95)
            flat = bool(t_lo <= 0 <= t_hi)
            trends[cat] = {
                "n": len(pts),
                # Per 90 days: a per-day slope on this scale rounds to zero and
                # reads as "no data" rather than "small".
                "per_90d": round(t_slope * 90, 2),
                "per_90d_lo": round(t_lo * 90, 2),
                "per_90d_hi": round(t_hi * 90, 2),
                "flat": flat,
                "direction": ("no detectable trend" if flat
                              else "improving" if t_slope > 0 else "declining"),
                "span_days": max(ords) - min(ords),
                "points": pts,
            }

    return fits, trends
