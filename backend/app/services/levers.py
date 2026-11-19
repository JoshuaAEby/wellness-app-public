"""Ranked levers: the few things to change, from findings that have held up.

What to Act On lists every surviving pair. This answers a narrower question: of the things
you control, which are worth acting on, what does each cost per unit, and where are you now?
A lever's effects come only from the action lists, so confounded and trade-off rows are
already out; this also drops rows whose own holdout weakened or that a single 10% of days
could flip (`unstable`).

The list is fixed BY MEANING, never by the data. A finding can be statistically fine and
still not be a lever: "more messaging lowers early-night HR" and "fewer WFH days" both survive,
and neither is advice. An input earns a place here only if changing it on purpose makes sense.
Everything stays visible in the full findings list under the card.
"""
import re
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

EXPOSURE_DAYS = 28
MIN_EXPOSURE_DAYS = 7
MAX_LEVERS = 5

HELD_UP = {"replicating", "consistent"}

# `per` scales the stored per-unit slope to a step you could actually take.
LEVERS: dict[str, dict] = {
    "alcohol_sdu":   {"label": "Alcohol", "direction": "less", "verb": "LESS", "per": 1,
                      "per_label": "per SDU", "fmt": "{:.1f} SDU/wk"},
    # Building at maintenance (2026-11-01): the aim is fewer big days, not a lower average.
    "calories":      {"label": "Calories", "direction": "less", "verb": "NO SPIKES", "per": 500,
                      "per_label": "per 500 kcal", "fmt": "{:,.0f} kcal/day"},
    "sleep_total_h": {"label": "Sleep duration", "direction": "more", "verb": "MORE", "per": 1,
                      "per_label": "per extra hour", "fmt": "{:.1f} h/night"},
    "bedtime_hour":  {"label": "Bedtime", "direction": "less", "verb": "EARLIER", "per": 1,
                      "per_label": "per hour later", "fmt": "clock"},
    "water_oz":      {"label": "Water", "direction": "more", "verb": "MORE", "per": 20,
                      "per_label": "per 20 oz", "fmt": "{:.0f} oz/day"},
    "screen_social": {"label": "Social media", "direction": "less", "verb": "LESS", "per": 30,
                      "per_label": "per 30 min", "fmt": "{:.0f} min/day"},
}

# True, and no use as a reason to change anything: hours asleep predicts how well you say
# you slept.
CIRCULAR = {("sleep_total_h", "subj_sleep")}


def _clock(hours: float) -> str:
    """Bedtimes run past 24 (25.5 = 01:30)."""
    h = int(hours) % 24
    m = int(round((hours - int(hours)) * 60))
    if m == 60:
        h, m = (h + 1) % 24, 0
    return f"{(h % 12) or 12}:{m:02d} {'am' if h < 12 else 'pm'}"


def _short(label: str) -> str:
    return re.sub(r"\s*(z-score|\(subjective\)|\(first 2 h\))", "", label).strip().replace(
        "Respiratory rate", "Resp rate")


def _exposure(spec: dict, values: list) -> tuple[Optional[str], int]:
    """(text, days it rests on). The frame drops travel and sick days, so right after a trip
    this can be few days; the count goes on the card."""
    vals = [v for v in values if v is not None]
    if len(vals) < MIN_EXPOSURE_DAYS:
        return None, len(vals)
    mean = sum(vals) / len(vals)
    return (_clock(mean) if spec["fmt"] == "clock" else spec["fmt"].format(mean)), len(vals)


def _alcohol_exposure(spec: dict, by_day: dict, days: list) -> tuple[str, int]:
    """Every day counts, unlike the frame: alcohol is logged completely, travel included, so
    an absent day is a real zero. Once, only a third of the last 28 days were in the frame, and
    its rate read more than double the true weekly figure."""
    # Trip days with alcohol not logged (note 102) are None in by_day and left out.
    days = [d for d in days if by_day.get(d, 0.0) is not None]
    if not days:
        return None, 0
    return spec["fmt"].format(sum(by_day.get(d, 0.0) for d in days) / len(days) * 7), len(days)


def _effect(row: dict, cached: dict, verdicts: dict, spec: dict) -> tuple[Optional[dict], Optional[str]]:
    """(effect, None), or (None, why it was left out)."""
    key = (row["input_key"], row["outcome_key"], row["lag_days"])
    c = cached.get(key)
    d = (c or {}).get("survivor_details")
    if not d or d.get("slope") is None:
        return None, "no_detail"
    if (key[0], key[1]) in CIRCULAR:
        return None, "circular"
    if d.get("unstable"):
        return None, "unstable"
    verdict = verdicts.get(key)
    if verdict == "weakening":
        return None, "weakening"
    per, unit = spec["per"], {"z": "σ"}.get(d.get("y_unit"), d.get("y_unit") or "")
    return {
        "outcome": _short(c.get("metric_b_label") or key[1]),
        "next_day": key[2] == 1,
        "effect": round(d["slope"] * per, 2),
        "low": None if d.get("slope_lo") is None else round(d["slope_lo"] * per, 2),
        "high": None if d.get("slope_hi") is None else round(d["slope_hi"] * per, 2),
        "unit": unit,
        "rho": round(c["spearman_r"], 2),
        "n": c.get("n"),
        "tier": row.get("tier"),
        "status": verdict if verdict in HELD_UP else "too_early",
        "span_sd": abs(d.get("span_effect_sd") or 0),
    }, None


def build_levers(actionable: dict, cached: list[dict], verdicts: dict, series: dict,
                 alcohol_by_day: dict, today: date) -> dict:
    """Pure: `verdicts` maps (input, outcome, lag) to the holdout verdict, `series` is the
    correlation frame by day, so exposure uses the same definitions as the findings."""
    by_key = {(r["metric_a"], r["metric_b"], r["lag_days"]): r for r in cached}
    lists = {"more": actionable.get("do_more", []) + actionable.get("do_more_suggestive", []),
             "less": actionable.get("do_less", []) + actionable.get("do_less_suggestive", [])}
    end = today - timedelta(days=1)
    window = lambda skip: [end - timedelta(days=skip + i) for i in range(EXPOSURE_DAYS)]

    def exposure(key: str, spec: dict, skip: int) -> tuple[Optional[str], int]:
        if key == "alcohol_sdu":
            return _alcohol_exposure(spec, alcohol_by_day, window(skip))
        return _exposure(spec, [(series.get(d) or {}).get(key) for d in window(skip)])

    levers, left_out = [], {}
    for key, spec in LEVERS.items():
        effects = []
        for row in lists[spec["direction"]]:
            if row["input_key"] != key:
                continue
            effect, why = _effect(row, by_key, verdicts, spec)
            if effect:
                effects.append(effect)
            else:
                left_out[why] = left_out.get(why, 0) + 1
        if not effects:
            continue
        effects.sort(key=lambda e: (e["status"] not in HELD_UP, -e["span_sd"]))
        now, now_days = exposure(key, spec, 0)
        levers.append({
            "key": key, "label": spec["label"], "verb": spec["verb"],
            "per_label": spec["per_label"], "now": now, "now_days": now_days,
            "before": exposure(key, spec, EXPOSURE_DAYS)[0],
            "held_up": any(e["status"] in HELD_UP for e in effects),
            "effects": effects,
        })
    # Held up first, then whichever covers more of your own usual swing.
    levers.sort(key=lambda l: (not l["held_up"], -max(e["span_sd"] for e in l["effects"])))
    n_rows = sum(len(v) for v in lists.values())
    return {"levers": levers[:MAX_LEVERS], "action_rows": n_rows,
            "not_levers": sum(1 for v in lists.values() for r in v if r["input_key"] not in LEVERS),
            "left_out": left_out, "exposure_days": EXPOSURE_DAYS}


async def compute_levers(db: AsyncSession) -> dict:
    from app.services.correlations import (
        ACTION_THRESHOLD, _fetch_daily_series, build_actionable_insights, build_validation,
        read_correlation_cache,
    )
    from app.foodday import food_day_of_utc, on_food_days_utc
    from app.models import HealthMetric
    from app.timeutils import local_today

    cached = await read_correlation_cache()
    if not cached:
        return {"levers": [], "action_rows": 0, "not_levers": 0, "left_out": {},
                "exposure_days": EXPOSURE_DAYS}
    actionable = build_actionable_insights(cached, min_abs_r=ACTION_THRESHOLD, min_n=30,
                                           max_per_section=None)
    validation = await build_validation(db)
    verdicts = {(v["metric_a"], v["metric_b"], v["lag_days"]): v["verdict"]
                for v in validation["results"]}
    today = local_today()
    start, end = today - timedelta(days=2 * EXPOSURE_DAYS), today - timedelta(days=1)
    series, _ = await _fetch_daily_series(db, start, end)
    rows = await db.execute(select(HealthMetric.timestamp, HealthMetric.value)
                            .where(HealthMetric.metric_type == "alcohol")
                            .where(on_food_days_utc(HealthMetric.timestamp, start, end)))
    alcohol_by_day: dict = {}
    for ts, value in rows.all():
        day = food_day_of_utc(ts)
        alcohol_by_day[day] = alcohol_by_day.get(day, 0.0) + value
    from app.services.trip_gaps import gap_dates
    for d in await gap_dates(db, "alcohol", start, end):
        alcohol_by_day.setdefault(d, None)
    return build_levers(actionable, cached, verdicts, series, alcohol_by_day, today)
