"""Charts inside the weekly / monthly summaries (note 66, 2026-10-04).

The model may illustrate a point by writing a marker on its own line —
`[chart: protein_daily]` — chosen from the fixed CATALOG below. It never supplies a number:
every value is read from the database here, for the summary's own period, and the caption
is written from the data too. So a chart cannot be fabricated, and when the prose misquotes
a number the real series sits directly under it.

Granularity follows the summary: weekly → DAILY points over the last 4 weeks with the
summary's week shaded as NOW; monthly → WEEKLY means over ~4 months with the month shaded.
Payloads are shaped for the frontend's ScoreChartSheet `Plot` (points / unit / target /
targetBand / domain / bars / bands).
"""
from __future__ import annotations

import re
import statistics as st
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.acwr import ACWR_HIGH, ACWR_LOW
from app.models import (CaffeineLog, DailyMetric, FoodLog, HealthMetric, ScreentimeEvent,
                        SleepSession, UserSettings)
from app.timeutils import local_date_sql, local_day_bounds_utc, local_food_date_sql
from app.foodday import food_date_sql, food_day_start, food_day_end

MAX_CHARTS = {"weekly": 3, "monthly": 4}
MARKER_RE = re.compile(r"^\s*\[chart:\s*([a-z_]+)\s*\]\s*$", re.I)

# id → (label, unit, options). Kept in this order in the prompt menu.
CATALOG: dict[str, dict] = {
    "protein_daily":  {"label": "Protein", "unit": "g", "area": "nutrition"},
    "fiber_daily":    {"label": "Fiber", "unit": "g", "area": "nutrition"},
    "balance_daily":  {"label": "Vs break-even", "unit": "kcal", "area": "nutrition"},
    "alcohol_daily":  {"label": "Alcohol", "unit": "drinks", "area": "nutrition", "bars": True},
    "after_dinner":   {"label": "Eating after dinner", "unit": "% of days", "area": "nutrition"},
    "caffeine_daily": {"label": "Caffeine", "unit": "mg", "area": "nutrition", "bars": True},
    "sleep_nightly":  {"label": "Sleep duration", "unit": "h", "area": "recovery"},
    "hrv_nightly":    {"label": "HRV (overnight)", "unit": "ms", "area": "recovery"},
    "rhr_nightly":    {"label": "Resting heart rate", "unit": "bpm", "area": "recovery"},
    "load_daily":     {"label": "Training load (TRIMP)", "unit": "TRIMP", "area": "training", "bars": True},
    "acwr_trend":     {"label": "Acute : chronic (ACWR)", "unit": "ACWR", "area": "training"},
    "steps_daily":    {"label": "Steps", "unit": "steps", "area": "activity", "bars": True},
    "screen_daily":   {"label": "Phone screen time", "unit": "min", "area": "behaviour"},
    "weight_trend":   {"label": "Weight", "unit": "lb", "area": "body"},
}

MENU_TEXT = "\n".join(f"  {k} — {v['label']} ({v['unit']})" for k, v in CATALOG.items())


def prompt_block(kind: str) -> str:
    """The instruction appended to the weekly / monthly system prompt."""
    n = MAX_CHARTS[kind]
    gran = "daily points over the last 4 weeks" if kind == "weekly" else "weekly points over ~4 months"
    return (
        "\n\nCHARTS. You may illustrate up to " + str(n) + " points with a chart from this "
        "fixed menu. Write the marker ALONE on its own line, directly after the paragraph it "
        "supports, exactly as `[chart: <id>]`. The app draws it from the real data (" + gran +
        ", this period shaded), so never describe what the chart will look like and never put "
        "numbers in the marker. Use a chart only when it SHOWS the point you just made — a "
        "trend turning, a gap to target, an outlier day — never as decoration, and never two "
        "charts for one point. Zero charts is fine.\nMenu:\n" + MENU_TEXT
    )


def extract_ids(text: str) -> list[str]:
    return [m.group(1).lower() for line in (text or "").splitlines()
            if (m := MARKER_RE.match(line))]


def sanitize(text: str, kind: str) -> str:
    """Drop unknown ids, duplicates and anything past the per-kind limit (in that order)."""
    seen: list[str] = []
    out = []
    for line in (text or "").splitlines():
        m = MARKER_RE.match(line)
        if not m:
            out.append(line)
            continue
        cid = m.group(1).lower()
        if cid in CATALOG and cid not in seen and len(seen) < MAX_CHARTS[kind]:
            seen.append(cid)
            out.append(f"[chart: {cid}]")
    return "\n".join(out)


def strip_markers(text: str) -> str:
    """Plain-text copy for the phone push — markers mean nothing there."""
    return "\n".join(l for l in (text or "").splitlines() if not MARKER_RE.match(l))


# ── Daily series ──────────────────────────────────────────────────────────────

async def _food_daily(db, start, end, attr) -> dict[date, float]:
    from app.services.day_flags import nutrition_excluded_dates
    excluded = await nutrition_excluded_dates(db, start, end)
    rows = (await db.execute(
        select(food_date_sql(FoodLog.timestamp).label("d"), func.sum(getattr(FoodLog, attr)))
        .where(FoodLog.timestamp >= food_day_start(start))
        .where(FoodLog.timestamp < food_day_end(end))
        .where((FoodLog.skipped == False) | (FoodLog.skipped.is_(None)))  # noqa: E712
        .group_by(food_date_sql(FoodLog.timestamp)))).all()
    return {date.fromisoformat(str(d)): float(v or 0) for d, v in rows
            if date.fromisoformat(str(d)) not in excluded}


async def _hm_daily(db, start, end, metric, agg=func.sum) -> dict[date, float]:
    # Alcohol follows the 04:00 food day; everything else the calendar day.
    lday = (local_food_date_sql if metric == "alcohol" else local_date_sql)(HealthMetric.timestamp)
    rows = (await db.execute(
        select(lday, agg(HealthMetric.value)).where(HealthMetric.metric_type == metric)
        .where(HealthMetric.timestamp >= local_day_bounds_utc(start)[0])
        .where(HealthMetric.timestamp < local_day_bounds_utc(end)[1]).group_by(lday))).all()
    return {date.fromisoformat(str(d)): float(v) for d, v in rows if v is not None}


async def _dm_daily(db, start, end, col) -> dict[date, float]:
    rows = (await db.execute(select(DailyMetric.date, getattr(DailyMetric, col))
                             .where(DailyMetric.date >= start).where(DailyMetric.date <= end))).all()
    return {d: float(v) for d, v in rows if v is not None}


async def _daily(db: AsyncSession, cid: str, start: date, end: date) -> tuple[dict[date, float], dict]:
    """(series, extras) for one chart id. extras may carry target / targetBand / domain."""
    settings = (await db.execute(select(UserSettings).limit(1))).scalar_one_or_none()
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    if cid == "protein_daily":
        from app.services.nutrition_goals import resolve_protein_goal
        return await _food_daily(db, start, end, "protein_g"), {"target": await resolve_protein_goal(db, settings)}
    if cid == "fiber_daily":
        from app.services.nutrition_goals import fiber_goal_for
        return await _food_daily(db, start, end, "fiber_g"), {"target": fiber_goal_for(settings)}
    if cid == "balance_daily":
        from app.services.energy import current_activity_factor, resolve_rmr
        kcal = await _food_daily(db, start, end, "calories")
        act = await _hm_daily(db, start, end, "active_calories")
        rmr = (await resolve_rmr(db, settings))["rmr"] if settings else None
        k = await current_activity_factor(db)
        return ({d: v - (rmr + k * act[d]) for d, v in kcal.items() if d in act} if rmr else {}), {}
    if cid == "alcohol_daily":
        alc = await _hm_daily(db, start, end, "alcohol")
        return {d: alc.get(d, 0.0) for d in days}, {}                # unlogged = none
    if cid == "after_dinner":
        from app.services.evening_eating import ate_after_dinner
        return {d: 100.0 * v for d, v in (await ate_after_dinner(db, start, end)).items()
                if v is not None}, {"domain": [0, 100]}
    if cid == "caffeine_daily":
        lday = local_date_sql(CaffeineLog.timestamp)
        rows = (await db.execute(
            select(lday, func.sum(CaffeineLog.mg))
            .where(CaffeineLog.timestamp >= local_day_bounds_utc(start)[0])
            .where(CaffeineLog.timestamp < local_day_bounds_utc(end)[1]).group_by(lday))).all()
        got = {date.fromisoformat(str(d)): float(v or 0) for d, v in rows}
        return {d: got.get(d, 0.0) for d in days if d <= end}, {}
    if cid == "sleep_nightly":
        rows = (await db.execute(select(SleepSession.date, SleepSession.total_min)
                                 .where(SleepSession.date >= start).where(SleepSession.date <= end))).all()
        return {d: m / 60 for d, m in rows if m}, {}
    if cid == "hrv_nightly":
        return await _dm_daily(db, start, end, "hrv_value"), {}
    if cid == "rhr_nightly":
        return await _dm_daily(db, start, end, "rhr_value"), {}
    if cid == "load_daily":
        return await _dm_daily(db, start, end, "daily_load"), {}
    if cid == "acwr_trend":
        return await _dm_daily(db, start, end, "acwr"), {"targetBand": [ACWR_LOW, ACWR_HIGH], "targetLabel": "normal range"}
    if cid == "steps_daily":
        return await _hm_daily(db, start, end, "steps"), {}
    if cid == "screen_daily":
        lday = local_date_sql(ScreentimeEvent.start_time)
        rows = (await db.execute(
            select(lday, func.sum(ScreentimeEvent.duration_sec)).where(ScreentimeEvent.device == "iPhone")
            .where(lday >= start.isoformat()).where(lday <= end.isoformat()).group_by(lday))).all()
        return {date.fromisoformat(str(d)): (v or 0) / 60 for d, v in rows}, {}
    if cid == "weight_trend":
        return await _hm_daily(db, start, end, "weight", func.avg), {}
    raise KeyError(cid)


async def build_charts(db: AsyncSession, kind: str, period_start: date, period_end: date,
                       ids: list[str]) -> list[dict]:
    """Chart payloads for the ids referenced by one summary."""
    weekly = kind == "weekly"
    start = period_end - timedelta(days=27) if weekly else period_end - timedelta(weeks=17)
    start = min(start, period_start)
    out = []
    for cid in [i for i in ids if i in CATALOG][:MAX_CHARTS.get(kind, 3)]:
        meta = CATALOG[cid]
        daily, extra = await _daily(db, cid, start, period_end)
        if weekly:
            points = [{"x": d.isoformat(), "y": round(v, 2), "label": d.strftime("%a %b %-d")}
                      for d, v in sorted(daily.items())]
        else:   # weekly means, ISO Monday
            byw: dict[date, list] = defaultdict(list)
            for d, v in daily.items():
                byw[d - timedelta(days=d.weekday())].append(v)
            points = [{"x": w.isoformat(), "y": round(st.mean(v), 2)} for w, v in sorted(byw.items())]
        if meta.get("bars") and not weekly:
            extra.setdefault("bars", False)       # weekly means read better as a line
        target = extra.get("target")
        fmt = lambda d: d.strftime("%b %-d")
        caption = (f"{meta['label']} — {'each day' if weekly else 'weekly average'}, "
                   f"{fmt(start)} to {fmt(period_end)}; shaded = {fmt(period_start)}–{fmt(period_end)}")
        # (The target is named in the plot's own legend — repeating it here was noise.)
        out.append({
            "id": cid, "label": meta["label"], "unit": meta["unit"], "points": points,
            "target": target, "targetLabel": extra.get("targetLabel"),
            "targetBand": extra.get("targetBand"), "domain": extra.get("domain"),
            # Daily yes/no (after_dinner) reads as bars, not a 0/100 zig-zag line.
            "bars": (bool(meta.get("bars")) or cid == "after_dinner") if weekly else extra.get("bars", False),
            "bands": [{"label": "THIS WEEK" if weekly else "THIS MONTH",
                       "from": period_start.isoformat(), "to": period_end.isoformat(), "strong": True}],
            "caption": caption,
        })
    return out
