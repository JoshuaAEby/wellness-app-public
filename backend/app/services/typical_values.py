"""
"What is typical for me?" — mean ± SD, median and IQR for every daily variable (note 86).

Built on the correlation engine's daily frame (`correlations._fetch_daily_series`), on
purpose: that frame is the app's vetted definition of a NORMAL day — travel/sick/…
days are dropped whole, and nutrition is nulled on incomplete-log days — so "typical
protein" here is the same quantity every finding and summary reasons about, not a
second, subtly different average. The response says how many days that excluded.

What each variable gets depends on its shape:
  binary   (0/1 day flags, habits)     → % of days
  clock    (bedtime, last caffeine, …) → times, mean ± SD in minutes; bedtimes after
                                          midnight are already 24+ h in the frame, so
                                          plain statistics are correct
  number                                → mean ± SD AND median [IQR]. `prefer_median` is
                                          set when the mean misleads: ≥25% zeros
                                          (alcohol, hard minutes) or |skew| ≥ 1. SD on a
                                          zero-heavy series describes almost nobody.
Fluids (services/fluids) is added from its own source, over the same included days.
"""
from __future__ import annotations

import statistics as st
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

# 5, not 7: with 7 the 1W window needed every single day, so any gap emptied it. Each
# row shows its n, so a thin estimate is visible rather than hidden.
MIN_DAYS = 5

GROUPS: list[tuple[str, list[str]]] = [
    ("Recovery", ["hrv_z_score", "rhr_z_score", "readiness_score", "respiratory_rate",
                  "wrist_temp_deviation", "walking_hr", "sleep_hr_early_fixed",
                  "breathing_disturbances"]),
    ("Sleep", ["sleep_total_h", "sleep_quality_pct", "bedtime_hour", "sleep_debt_hours",
               "bedroom_temp", "bedroom_humidity", "nap_min"]),
    ("How you felt", ["subj_energy", "subj_mood", "subj_sleep", "subj_stress", "subj_day"]),
    ("Activity", ["steps", "active_calories", "exercise_minutes", "stand_hours",
                  "walk_distance", "walking_speed", "time_in_daylight",
                  "morning_light_min", "first_light_hour", "light_centroid_hour"]),
    ("Training", ["daily_load", "hard_minutes", "acwr", "zone2_pct_7d", "zone3_pct_7d",
                  "high_intensity_pct_7d", "tonnage", "hard_sets", "workout_gym_class",
                  "workout_lifting", "workout_conditioning", "workout_strength",
                  "workout_walk"]),
    ("Nutrition", ["calories", "protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g",
                   "sodium_mg", "energy_balance"]),
    ("Drinks", ["fluids_oz", "water_oz", "caffeine_total_mg", "caffeine_last_hour",
                "caffeine_onboard_bed", "alcohol_sdu", "alcohol_onboard_bed"]),
    ("Screens", ["screen_total", "screen_social", "screen_communication",
                 "screen_entertainment", "screen_evening", "tv_prebed_min"]),
    ("Habits & context", ["habit_caffeine", "habit_supplements", "habit_creatine",
                          "habit_meditation", "habit_light", "mindful_min", "wfh_day",
                          "partner_present"]),
    ("Weather", ["weather_temp_mean", "weather_aqi_mean", "weather_uv_max",
                 "weather_pressure_mean", "weather_pressure_delta"]),
]
# Yes/no day flags, listed rather than inferred: guessing from {0, 1} values called an
# all-zero minutes series (nap_min) "0% of days".
BINARY = {"workout_gym_class", "workout_lifting", "workout_conditioning", "workout_strength",
          "workout_walk", "workout_hiit", "habit_caffeine", "habit_supplements",
          "habit_creatine", "habit_meditation", "habit_light", "wfh_day", "partner_present",
          "slept_away"}
CLOCK = {"bedtime_hour", "caffeine_last_hour", "first_light_hour", "light_centroid_hour",
         "meditation_hour"}
# Calibration inputs and duplicates of another row — never "a value of yours".
SKIP_PREFIX = ("ctrl_",)
SKIP = {"workout_hiit", "slept_away"}   # = workout_gym_class; always 0 (away days excluded)


def _skew(xs: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    m = st.fmean(xs)
    sd = st.pstdev(xs)
    return 0.0 if sd == 0 else sum((x - m) ** 3 for x in xs) / n / sd ** 3


def _q(xs: list[float], p: float) -> float:
    s = sorted(xs)
    k = (len(s) - 1) * p
    f = int(k)
    return s[f] + (s[min(f + 1, len(s) - 1)] - s[f]) * (k - f)


def _describe(key: str, xs: list[float]) -> dict:
    n = len(xs)
    if key in BINARY:
        return {"kind": "binary", "n": n, "pct": round(100 * st.fmean(xs), 1)}
    zeros = sum(1 for x in xs if x == 0) / n
    d = {"kind": "clock" if key in CLOCK else "number", "n": n,
         "mean": st.fmean(xs), "sd": st.stdev(xs) if n > 1 else 0.0,
         "median": st.median(xs), "p25": _q(xs, 0.25), "p75": _q(xs, 0.75),
         "min": min(xs), "max": max(xs), "zero_share": round(zeros, 3)}
    d["prefer_median"] = d["kind"] == "number" and (zeros >= 0.25 or abs(_skew(xs)) >= 1)
    return d


async def typical_values(db: AsyncSession, start: date, end: date) -> dict:
    from app.services.correlations import METRIC_LABELS, _fetch_daily_series
    from app.services.fluids import fluid_days

    series, _ = await _fetch_daily_series(db, start, end)
    included = sorted(series)
    cols: dict[str, list[float]] = {}
    for d in included:
        for k, v in series[d].items():
            if v is not None:
                cols.setdefault(k, []).append(float(v))
    # Fluids from all drinks, on included (non-flagged) days WITH WATER LOGGED. "Any drink"
    # let a lone soda define a day — over a year that read 24 oz, mostly months before
    # water logging existed. Water is the bulk of fluids, so it is the completeness signal.
    fl = await fluid_days(db, start, end)
    cols["fluids_oz"] = [fl[d]["total"] for d in included if d in fl and fl[d]["water"] > 0]
    labels = {**METRIC_LABELS, "fluids_oz": "Fluids, all drinks (oz)"}

    placed = {k for _, ks in GROUPS for k in ks}
    extra = sorted(k for k in cols if k not in placed and k not in SKIP
                   and not k.startswith(SKIP_PREFIX))
    groups = []
    for name, keys in GROUPS + ([("Other", extra)] if extra else []):
        items = []
        for k in keys:
            xs = cols.get(k) or []
            if k in SKIP or len(xs) < MIN_DAYS:
                continue
            items.append({"key": k, "label": labels.get(k, k),
                          **_describe(k, xs)})
        if items:
            groups.append({"group": name, "items": items})

    return {"start": start.isoformat(), "end": end.isoformat(),
            "window_days": (end - start).days + 1, "included_days": len(included),
            "min_days": MIN_DAYS, "groups": groups}
