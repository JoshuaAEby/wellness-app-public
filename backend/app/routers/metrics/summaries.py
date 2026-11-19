"""AI summary refreshes, manual push triggers, weekly digest, API usage, progress, summary charts.

Part of the /api/metrics package (split out of routers/metrics.py 2026-10-21)."""
from datetime import date, timedelta
from statistics import mean
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import ApiCallLog, DailyMetric, FoodLog, HabitLog, SleepSession, SubjectiveLog, UserSettings, Workout
from app.timeutils import local_date_sql, to_local_date, local_today
from app.foodday import food_date_sql, food_day_start, food_day_end

router = APIRouter()


@router.post("/today/refresh-summary")
async def refresh_daily_summary(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Regenerate today's AI daily summary on demand."""
    from app.services.ai_summaries import generate_daily_summary
    text = await generate_daily_summary()
    if text is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="AI summary unavailable — check API key or AI enabled setting")
    return {"summary": text}


@router.post("/weekly/refresh-summary")
async def refresh_weekly_summary(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Regenerate this week's AI summary on demand."""
    from app.services.ai_summaries import generate_weekly_summary
    text = await generate_weekly_summary()
    if text is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="Weekly summary unavailable")
    return {"summary": text}


@router.post("/monthly/refresh-summary")
async def refresh_monthly_summary(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Regenerate last month's AI summary on demand."""
    from app.services.ai_summaries import generate_monthly_summary
    text = await generate_monthly_summary()
    if text is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="Monthly summary unavailable — check API key or not enough data")
    return {"summary": text}


@router.post("/notifications/afternoon")
async def trigger_afternoon_nudge(
    _: str = Depends(get_current_user),
):
    """Manually trigger the afternoon steps nudge (for testing)."""
    from app.services.scheduler import _afternoon_nudge
    await _afternoon_nudge()
    return {"status": "sent"}


@router.post("/notifications/presleep")
async def trigger_presleep_nudge(
    _: str = Depends(get_current_user),
):
    """Evaluate the 9 PM recovery check now (for testing). It no longer pushes on its own —
    since 2026-10-21 it rides in the "Coach tomorrow" notification — so this returns what
    that push would include, or null."""
    from app.services.scheduler import _presleep_alert
    alert = await _presleep_alert()
    return {"alert": alert[0] if alert else None, "priority": alert[1] if alert else None}


@router.post("/notifications/illness")
async def trigger_illness_check(
    _: str = Depends(get_current_user),
):
    """Manually trigger the illness/overtraining early-warning check (for testing)."""
    from app.services.scheduler import _illness_check
    await _illness_check()
    return {"status": "checked"}


@router.get("/weekly-digest")
async def get_weekly_digest(
    baseline_weeks: int = Query(default=12, ge=4, le=52),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """This week against a TYPICAL week — not against last week.

    Week-over-week deltas were the original design and they do not work on these
    series. Nightly sleep has an SD of ~78 min, so a week-over-week move of 10-20
    min is a fraction of ordinary variation reported as though it were news. The
    card always showed a change, none of the changes meant anything, and it
    correctly trained the reader to ignore it.

    A delta is uninterpretable without a spread. So alongside the current week this
    now returns the DISTRIBUTION of the last `baseline_weeks` complete weeks per
    metric (median, p25, p75, n) and lets the client say "outside your usual range"
    only when the current week actually falls outside it. `prev` is retained so the
    payload stays backward-compatible.
    """
    today = local_today()
    this_start = today - timedelta(days=6)   # 7 days including today
    prev_start = today - timedelta(days=13)
    prev_end   = today - timedelta(days=7)

    # ── Settings (goals) ──────────────────────────────────────────────────────
    settings = (await db.execute(select(UserSettings).limit(1))).scalar_one_or_none()
    # ONE derivation, shared — see services/nutrition_goals (fifth inline copy removed).
    from app.services.nutrition_goals import resolve_protein_goal as _resolve_protein_goal
    protein_goal = await _resolve_protein_goal(db, settings)
    # Per-day, not resolve_calorie_goal()["value"] — that is None in balance/pace mode
    # (the goal moves daily with activity), which silently zeroed calorie adherence here.
    from app.services.energy import effective_calorie_goal
    # Resolved across the BASELINE window too, not just this week, so the baseline
    # hit-rate is computed exactly the way the current week's is — like with like. The
    # per-day loop is the sanctioned way to do this: a batched version was written and
    # removed 2026-08-22 because it measured SLOWER (0.42 s vs 0.16 s for 90 days) and
    # disagreed with this function by up to 3 kcal. See the note in services/energy.
    base_start = this_start - timedelta(weeks=baseline_weeks)
    cal_goal_by_day: dict = {}
    if settings:
        _d = base_start
        while _d <= today:
            _g = (await effective_calorie_goal(db, settings, _d))["goal"]
            if _g:
                cal_goal_by_day[_d] = _g
            _d += timedelta(days=1)

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _avg(vals):
        vs = [v for v in vals if v is not None]
        return round(mean(vs), 2) if vs else None

    def _fmt_period(s, e):
        if s.month == e.month:
            return f"{s.strftime('%b')} {s.day}–{e.day}"
        return f"{s.strftime('%b')} {s.day} – {e.strftime('%b')} {e.day}"

    # ── Workout counts ────────────────────────────────────────────────────────
    async def wo_count(s, e):
        wo_day = local_date_sql(Workout.start_time)
        r = await db.execute(
            select(func.count(Workout.id))
            .where(wo_day >= str(s))
            .where(wo_day <= str(e))
        )
        return r.scalar() or 0

    wo_this = await wo_count(this_start, today)
    wo_prev = await wo_count(prev_start, prev_end)

    # ── Daily metrics (readiness, HRV z-score) ────────────────────────────────
    async def dm_avgs(s, e):
        rows = (await db.execute(
            select(DailyMetric).where(DailyMetric.date >= s, DailyMetric.date <= e)
        )).scalars().all()
        return {
            "readiness": _avg([r.readiness_score for r in rows]),
            "hrv_z":     _avg([r.hrv_z_score     for r in rows]),
        }

    dm_this = await dm_avgs(this_start, today)
    dm_prev = await dm_avgs(prev_start, prev_end)

    # ── Sleep ─────────────────────────────────────────────────────────────────
    async def sleep_avg(s, e):
        rows = (await db.execute(
            select(SleepSession.total_min)
            .where(SleepSession.date >= s, SleepSession.date <= e)
            .where(SleepSession.total_min.is_not(None))
        )).all()
        return _avg([r.total_min / 60.0 for r in rows if r.total_min])

    sl_this = await sleep_avg(this_start, today)
    sl_prev = await sleep_avg(prev_start, prev_end)

    # ── Nutrition ─────────────────────────────────────────────────────────────
    nut_rows = (await db.execute(
        select(
            food_date_sql(FoodLog.timestamp).label("d"),
            func.sum(FoodLog.protein_g).label("protein"),
            func.sum(FoodLog.calories).label("calories"),
            func.sum(FoodLog.fiber_g).label("fiber"),
        )
        .where(FoodLog.timestamp >= food_day_start(this_start))
        .where(FoodLog.timestamp < food_day_end(today))
        .group_by(food_date_sql(FoodLog.timestamp))
    )).all()

    nut = {
        "days_logged":  len(nut_rows),
        "protein_hit":  sum(1 for r in nut_rows if protein_goal and (r.protein or 0) >= protein_goal) if protein_goal else None,
        "calorie_hit":  (sum(1 for r in nut_rows
                             if cal_goal_by_day.get(date.fromisoformat(str(r.d)))
                             and (r.calories or 0) <= cal_goal_by_day[date.fromisoformat(str(r.d))])
                         if cal_goal_by_day else None),
        "protein_goal": protein_goal,
        # Dynamic goals have no single value; report the median in force this window.
        "calorie_goal": (sorted(cal_goal_by_day.values())[len(cal_goal_by_day) // 2]
                         if cal_goal_by_day else None),
    }

    # ── Habits ────────────────────────────────────────────────────────────────
    habit_rows = (await db.execute(
        select(HabitLog)
        .where(HabitLog.date >= this_start, HabitLog.date <= today)
    )).scalars().all()

    habits = {}
    for key in ("no_late_caffeine", "outdoor_light_am", "supplements", "meditation"):
        logged = [r for r in habit_rows if getattr(r, key) is not None]
        done   = [r for r in logged   if getattr(r, key) is True]
        habits[key] = {"done": len(done), "logged": len(logged)}

    # ── Morning check-ins ─────────────────────────────────────────────────────
    checkin_count = (await db.execute(
        select(func.count(SubjectiveLog.id))
        .where(SubjectiveLog.date >= this_start, SubjectiveLog.date <= today)
    )).scalar() or 0

    # ── Baseline: the distribution of COMPLETE prior weeks ────────────────────
    #
    # Complete weeks only, and the current partial week is excluded — including it
    # would drag its own reference toward itself. Each metric is aggregated per week
    # exactly as the current week is, so like is compared with like.
    wo_day = local_date_sql(Workout.start_time)
    wo_rows = (await db.execute(
        select(wo_day.label("d"))
        .where(wo_day >= str(base_start)).where(wo_day < str(this_start))
    )).all()
    dm_rows = (await db.execute(
        select(DailyMetric)
        .where(DailyMetric.date >= base_start).where(DailyMetric.date < this_start)
    )).scalars().all()
    sl_rows = (await db.execute(
        select(SleepSession.date, SleepSession.total_min)
        .where(SleepSession.date >= base_start).where(SleepSession.date < this_start)
        .where(SleepSession.total_min.is_not(None))
    )).all()

    def _week_index(d: date) -> int:
        """Weeks back from this_start, so bucket boundaries line up with the
        current week rather than with the calendar."""
        return (this_start - d).days // 7

    wk_workouts: dict[int, int] = {}
    for r in wo_rows:
        di = date.fromisoformat(str(r.d))
        wk_workouts[_week_index(di)] = wk_workouts.get(_week_index(di), 0) + 1

    wk_readiness: dict[int, list] = {}
    wk_hrv: dict[int, list] = {}
    for r in dm_rows:
        i = _week_index(r.date)
        if r.readiness_score is not None:
            wk_readiness.setdefault(i, []).append(r.readiness_score)
        if r.hrv_z_score is not None:
            wk_hrv.setdefault(i, []).append(r.hrv_z_score)

    wk_sleep: dict[int, list] = {}
    for d_, tm in sl_rows:
        wk_sleep.setdefault(_week_index(d_), []).append(tm / 60.0)

    def _dist(per_week: dict, agg=mean):
        """median / p25 / p75 across weekly values. Needs >= 4 weeks to be a
        reference at all — below that the quartiles are the data points."""
        vals = sorted(agg(v) if isinstance(v, list) else v for v in per_week.values())
        n = len(vals)
        if n < 4:
            return {"median": None, "p25": None, "p75": None, "n_weeks": n}
        q = lambda p: vals[min(n - 1, int(p * n))]
        return {"median": round(q(0.5), 2), "p25": round(q(0.25), 2),
                "p75": round(q(0.75), 2), "n_weeks": n}

    # ── Baselines for nutrition / habits / check-ins ──────────────────────────
    #
    # These three used to come back as bare numbers with no reference, so the card
    # showed "3/7 days" with nothing to say whether 3 was good. Every metric here is
    # bucketed by the SAME `_week_index` and aggregated the same way as the current
    # week, so the comparison is like with like.
    base_food = (await db.execute(
        select(
            food_date_sql(FoodLog.timestamp).label("d"),
            func.sum(FoodLog.protein_g).label("protein"),
            func.sum(FoodLog.calories).label("calories"),
            func.sum(FoodLog.fiber_g).label("fiber"),
        )
        .where(FoodLog.timestamp >= food_day_start(base_start))
        .where(FoodLog.timestamp < food_day_start(this_start))
        .where((FoodLog.skipped == False) | (FoodLog.skipped.is_(None)))  # noqa: E712
        .group_by(food_date_sql(FoodLog.timestamp))
    )).all()
    base_habits = (await db.execute(
        select(HabitLog).where(HabitLog.date >= base_start).where(HabitLog.date < this_start)
    )).scalars().all()
    base_checkins = (await db.execute(
        select(SubjectiveLog.date)
        .where(SubjectiveLog.date >= base_start).where(SubjectiveLog.date < this_start)
    )).all()

    wk_days_logged: dict[int, int] = {}
    wk_protein_hit: dict[int, int] = {}
    wk_calorie_hit: dict[int, int] = {}
    wk_protein_g: dict[int, list] = {}
    wk_fiber_g: dict[int, list] = {}
    for r in base_food:
        di = date.fromisoformat(str(r.d))
        i = _week_index(di)
        wk_days_logged[i] = wk_days_logged.get(i, 0) + 1
        wk_protein_g.setdefault(i, []).append(r.protein or 0)
        wk_fiber_g.setdefault(i, []).append(r.fiber or 0)
        # Counts must exist at 0 for weeks that logged food but hit nothing, or the
        # distribution silently becomes "typical among weeks you succeeded".
        wk_protein_hit.setdefault(i, 0)
        wk_calorie_hit.setdefault(i, 0)
        if protein_goal and (r.protein or 0) >= protein_goal:
            wk_protein_hit[i] += 1
        g = cal_goal_by_day.get(di)
        if g and (r.calories or 0) <= g:
            wk_calorie_hit[i] += 1

    wk_checkins: dict[int, int] = {}
    for (d_,) in base_checkins:
        i = _week_index(d_)
        wk_checkins[i] = wk_checkins.get(i, 0) + 1

    habit_baseline = {}
    for key in ("no_late_caffeine", "outdoor_light_am", "supplements", "meditation"):
        per_week: dict[int, int] = {}
        for r in base_habits:
            i = _week_index(r.date)
            per_week.setdefault(i, 0)
            if getattr(r, key) is True:
                per_week[i] += 1
        habit_baseline[key] = _dist(per_week)

    baseline = {
        "workouts":  _dist(wk_workouts),
        "readiness": _dist(wk_readiness),
        "hrv_z":     _dist(wk_hrv),
        "sleep_h":   _dist(wk_sleep),
        "days_logged":  _dist(wk_days_logged),
        "protein_hit":  _dist(wk_protein_hit) if protein_goal else None,
        "calorie_hit":  _dist(wk_calorie_hit) if cal_goal_by_day else None,
        "protein_g":    _dist(wk_protein_g),
        "fiber_g":      _dist(wk_fiber_g),
        "checkins":     _dist(wk_checkins),
        "habits":       habit_baseline,
        "weeks_requested": baseline_weeks,
    }

    return {
        "period":    _fmt_period(this_start, today),
        # `prev` kept for backward compatibility; `baseline` is the one to read.
        "workouts":  {"this": wo_this,              "prev": wo_prev},
        "readiness": {"this": dm_this["readiness"],  "prev": dm_prev["readiness"]},
        "hrv_z":     {"this": dm_this["hrv_z"],      "prev": dm_prev["hrv_z"]},
        "sleep_h":   {"this": sl_this,               "prev": sl_prev},
        "baseline":  baseline,
        "nutrition": nut,
        "habits":    habits,
        "checkins":  checkin_count,
    }


@router.get("/usage")
async def get_ai_usage(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Return AI API call statistics and estimated cost.

    Months are LOCAL calendar months (created_at is naive UTC; a call at 9 pm ET on
    the 31st used to land in the next month). `gaps` lists silences longer than
    GAP_DAYS in the log so the all-time figure can say it is incomplete rather than
    quietly understate — logging stopped 2026-06-07 → 2026-09-02 (note 51). The
    cost is our own per-call estimate from MODEL_PRICING, cross-checked against
    Anthropic's live cost report (which is in CENTS) (note 51)."""
    from app.timeutils import local_today
    today = local_today()
    GAP_DAYS = 14

    all_rows = (await db.execute(select(ApiCallLog).order_by(ApiCallLog.created_at))).scalars().all()
    month_rows = [
        r for r in all_rows
        if r.created_at and (d := to_local_date(r.created_at)).year == today.year and d.month == today.month
    ]
    stamps = [r.created_at for r in all_rows if r.created_at]
    gaps = [{"from": to_local_date(a).isoformat(), "to": to_local_date(b).isoformat(),
             "days": (b - a).days}
            for a, b in zip(stamps, stamps[1:]) if (b - a).days > GAP_DAYS]

    # feature == "backfill" rows are one row per DAY per model, filled from Anthropic's
    # usage report for the 2026-06-07 → 09-02 logging gap: they carry real dollars and
    # tokens but are not calls, so they stay out of the call count.
    def _agg(rows):
        return {
            "calls": sum(1 for r in rows if r.feature != "backfill"),
            "backfilled_usd": round(sum(r.cost_usd or 0 for r in rows if r.feature == "backfill"), 4),
            "input_tokens": sum(r.input_tokens or 0 for r in rows),
            "output_tokens": sum(r.output_tokens or 0 for r in rows),
            "cost_usd": round(sum(r.cost_usd or 0 for r in rows), 4),
        }

    def _group(rows, key):
        out = {}
        for r in rows:
            out.setdefault(key(r) or "unknown", []).append(r)
        return {k: _agg(v) for k, v in out.items()}

    return {
        "all_time": _agg(all_rows),
        "this_month": _agg(month_rows),
        "by_feature": _group(all_rows, lambda r: r.feature),
        "month_by_feature": _group(month_rows, lambda r: r.feature),
        "month_label": today.strftime("%B %Y"),
        "first_call": to_local_date(stamps[0]).isoformat() if stamps else None,
        "gaps": gaps,
    }


@router.get("/progress")
async def get_progress(
    since: Optional[date] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Absolute change since training began (note 56) — before vs now, with trends and
    CIs. The daily z-scores are relative to a rolling baseline and cannot show this."""
    from app.services.progress import build_progress
    return await build_progress(db, since=since)


@router.get("/summary-charts")
async def get_summary_charts(
    kind: str = Query(..., pattern="^(weekly|monthly)$"),
    start: date = Query(...),
    end: date = Query(...),
    ids: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Data for the `[chart: id]` markers in one weekly/monthly summary (note 66). Every
    value comes from the database; the model only chose which charts to show."""
    from app.services.summary_charts import build_charts
    return await build_charts(db, kind, start, end, [i.strip() for i in ids.split(",") if i.strip()])
