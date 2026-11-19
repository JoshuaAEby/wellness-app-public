"""The synthetic person behind the demo database.

One year of a made-up user ("Alex"), generated from a small causal model whose effects are
written down in GROUND_TRUTH. The seeder feeds the output through the app's real ingestion
and recompute pipeline, and `demo.report` then scores what the correlation engine found
against this table.

Design rule: every behaviour is drawn independently of every other behaviour and of every
outcome, EXCEPT where an edge is listed in GROUND_TRUTH. That is what makes the scoring
honest. Any surviving finding is then one of three things: a planted effect (or a measure
that sits on the same causal path), one of the two deliberate traps (a hidden confounder and
a reverse-causation pair), or a false positive.

Nothing here is calibrated on, or copied from, a real person's data. Magnitudes are chosen to
be plausible and to sit near the edge of what a year of daily data can detect, so the demo
shows the engine finding some things and missing others rather than a clean sweep.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np

HOME_TZ = "America/New_York"
TRIP_TZ = "Europe/Berlin"
DAYS = 365
SEED = 20261119

# ── Ground truth ──────────────────────────────────────────────────────────────
# Each entry: the cause, the outcome it moves, the direction, and the engine input keys that
# measure the same cause (a survivor on any of them, with this outcome and sign, counts as
# recovering the effect). `size` is what the generator actually does, in the outcome's units.
GROUND_TRUTH = [
    {"id": "alcohol_rhr", "kind": "planted", "outcome": "rhr_z_score", "sign": +1,
     "inputs": {"alcohol_sdu": +1, "alcohol_onboard_bed": +1},
     "what": "Drinks raise overnight heart rate the next night",
     "size": "+1.0 bpm per standard drink"},
    {"id": "alcohol_resp", "kind": "planted", "outcome": "respiratory_rate", "sign": +1,
     "inputs": {"alcohol_sdu": +1, "alcohol_onboard_bed": +1},
     "what": "Drinks raise sleeping breathing rate the next night",
     "size": "+0.08 breaths/min per standard drink"},
    {"id": "alcohol_hrv", "kind": "planted", "outcome": "hrv_z_score", "sign": -1,
     "inputs": {"alcohol_sdu": -1, "alcohol_onboard_bed": -1},
     "what": "Drinks lower overnight HRV the next night",
     "size": "−4% per standard drink"},
    {"id": "training_hrv", "kind": "planted", "outcome": "hrv_z_score", "sign": -1,
     "inputs": {"hard_minutes": -1, "daily_load": -1, "active_calories": -1,
                "exercise_minutes": -1, "workout_hiit": -1, "workout_strength": -1, "acwr": -1},
     "what": "A hard class lowers HRV the next morning",
     "size": "about −7% after a typical class"},
    {"id": "training_rhr", "kind": "planted", "outcome": "rhr_z_score", "sign": +1,
     "inputs": {"hard_minutes": +1, "daily_load": +1, "active_calories": +1,
                "exercise_minutes": +1, "workout_hiit": +1, "workout_strength": +1, "acwr": +1},
     "what": "A hard class raises overnight heart rate slightly (deliberately small)",
     "size": "+0.3 bpm after a typical class"},
    {"id": "sleep_energy", "kind": "planted", "outcome": "subj_energy", "sign": +1,
     "inputs": {"sleep_total_h": +1, "bedtime_hour": -1},
     "what": "More sleep, more energy the same morning (later bedtime = less sleep)",
     "size": "+0.30 points (1–5) per hour of sleep"},
    {"id": "sleep_subjsleep", "kind": "planted", "outcome": "subj_sleep", "sign": +1,
     "inputs": {"sleep_total_h": +1, "bedtime_hour": -1},
     "what": "More sleep, better rated sleep",
     "size": "+0.40 points per hour"},
    {"id": "caffeine_subjsleep", "kind": "planted", "outcome": "subj_sleep", "sign": -1,
     "inputs": {"caffeine_onboard_bed": -1, "caffeine_last_hour": -1,
                "caffeine_total_mg": -1, "habit_caffeine": +1},
     "what": "Caffeine still on board at bedtime worsens rated sleep",
     "size": "−0.3 points per 50 mg on board"},
    # Traps. Neither input causes the outcome. A hidden "stressful week" raises caffeine
    # and lowers mood (and raises stress); morning energy drives that day's steps, and
    # energy carries over from day to day. The reverse-lag test should label both
    # CONFOUNDED rather than let them through as levers.
    {"id": "trap_stress_caffeine", "kind": "confounded", "outcome": "subj_mood", "sign": -1,
     "inputs": {"caffeine_total_mg": -1, "caffeine_onboard_bed": -1, "caffeine_last_hour": -1},
     "what": "Hidden stress weeks drive both caffeine and low mood",
     "size": "no causal effect"},
    {"id": "trap_stress_caffeine_stress", "kind": "confounded", "outcome": "subj_stress", "sign": +1,
     "inputs": {"caffeine_total_mg": +1, "caffeine_onboard_bed": +1, "caffeine_last_hour": +1},
     "what": "Hidden stress weeks drive both caffeine and rated stress",
     "size": "no causal effect"},
    {"id": "trap_energy_steps", "kind": "reverse", "outcome": "subj_energy", "sign": +1,
     "inputs": {"steps": +1},
     "what": "Energy drives steps (reverse causation), and energy persists day to day",
     "size": "no causal effect of steps on energy"},
    # Not designed as a trap, but true of this world and worth showing: drinks are logged
    # as food too, so on drinking days calories, carbs and energy balance rise with the
    # alcohol. Food has no effect on any outcome here, so a calories finding on an
    # alcohol outcome is confounding by same-day co-occurrence. The reverse-lag test
    # cannot catch it: drinking does not persist from day to day.
    *[{"id": f"cooccur_{inp}_{out}", "kind": "co-occurrence", "outcome": out, "sign": s,
       "inputs": {inp: s}, "what": "Drink calories ride along with alcohol (same-day co-occurrence)",
       "size": "no causal effect"}
      for inp in ("calories", "energy_balance", "carbs_g")
      for out, s in (("rhr_z_score", +1), ("respiratory_rate", +1), ("hrv_z_score", -1))],
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def hae(dt_local: datetime) -> str:
    """Aware local datetime → the Health Auto Export string format."""
    return dt_local.strftime("%Y-%m-%d %H:%M:%S %z")


def clip(v, lo, hi):
    return max(lo, min(hi, v))


@dataclass
class World:
    start: date
    end: date
    metrics: dict = field(default_factory=dict)       # HAE name -> [ {date, qty} ]
    sleep: list = field(default_factory=list)          # HAE sleep_analysis samples
    workouts: list = field(default_factory=list)       # HAE workouts
    checkins: list = field(default_factory=list)       # subjective_log rows (dicts)
    habits: list = field(default_factory=list)         # habit_logs rows
    food: list = field(default_factory=list)           # (local naive dt, meal, name, kcal, p, c, f, fiber)
    caffeine: list = field(default_factory=list)       # (food day, aware local dt, mg)
    alcohol: list = field(default_factory=list)        # (food day, "HH:MM", sdu, label, kcal)
    water: list = field(default_factory=list)          # (food day, "HH:MM", oz)
    flags: list = field(default_factory=list)          # (date, reason, source)
    tz_periods: list = field(default_factory=list)     # (start, end, tz)
    latent: dict = field(default_factory=dict)         # per-day hidden state, for the report

    def add(self, name: str, dt_local: datetime, qty: float):
        self.metrics.setdefault(name, []).append({"date": hae(dt_local), "qty": round(float(qty), 3)})


# Synthetic menu: (name, kcal, protein, carbs, fat, fiber). Plausible label-style numbers.
MENU = {
    "breakfast": [("Oatmeal with berries and walnuts", 380, 11, 58, 13, 8),
                  ("Two eggs, toast and avocado", 430, 19, 30, 26, 7),
                  ("Greek yogurt, granola and honey", 340, 22, 44, 9, 3)],
    "lunch": [("Chicken and hummus wrap", 610, 38, 58, 24, 7),
              ("Lentil soup with bread", 520, 24, 78, 12, 15),
              ("Tuna salad sandwich and an apple", 590, 32, 66, 21, 7),
              ("Grain bowl with tofu", 640, 28, 80, 22, 11)],
    "dinner": [("Salmon, rice and broccoli", 720, 42, 70, 27, 6),
               ("Pasta with turkey meatballs", 780, 40, 92, 26, 7),
               ("Vegetable stir fry with chicken", 650, 41, 60, 24, 8),
               ("Bean and cheese burrito", 820, 33, 102, 30, 16),
               ("Homemade pizza, two slices", 690, 28, 78, 28, 4)],
    "snack": [("Apple with peanut butter", 270, 7, 28, 16, 5),
              ("Protein bar", 210, 20, 23, 7, 3),
              ("Trail mix", 240, 7, 20, 16, 3)],
}
MEAL_TIMES = {"breakfast": 7.5, "lunch": 12.5, "snack": 15.5, "dinner": 19.0}
DRINKS = [("Beer", 1.0, 150), ("Glass of wine", 1.2, 125), ("IPA", 1.6, 200), ("Cocktail", 1.5, 180)]


def _local(day: date, hour: float, tz: ZoneInfo) -> datetime:
    """`hour` may exceed 24 (after midnight) or be negative (the evening before)."""
    base = datetime.combine(day, time(0, 0))
    return (base + timedelta(minutes=round(hour * 60))).replace(tzinfo=tz)


def generate(end: date, days: int = DAYS, seed: int = SEED) -> World:
    rng = np.random.default_rng(seed)
    home, trip = ZoneInfo(HOME_TZ), ZoneInfo(TRIP_TZ)
    start = end - timedelta(days=days - 1)
    w = World(start=start, end=end)
    dates = [start + timedelta(days=i) for i in range(days)]

    # ── Events: one trip (8 days) and one illness (4 days) ────────────────────
    trip_start = start + timedelta(days=int(days * 0.38))
    trip_days = {trip_start + timedelta(days=i) for i in range(8)}
    sick_start = start + timedelta(days=int(days * 0.70))
    sick_days = {sick_start + timedelta(days=i) for i in range(4)}
    w.tz_periods.append((min(trip_days) + timedelta(days=1), max(trip_days) - timedelta(days=1), TRIP_TZ))
    for d in sorted(trip_days):
        w.flags.append((d, "travel", "manual"))
    for d in sorted(sick_days):
        w.flags.append((d, "sick", "manual"))

    def zone(d: date) -> ZoneInfo:
        a, b, _ = w.tz_periods[0]
        return trip if a <= d <= b else home

    # ── Hidden state ──────────────────────────────────────────────────────────
    # Stressful weeks (the confounder), a slow fitness trend, and AR(1) noise terms.
    weeks = (days + 6) // 7
    stress_week = rng.random(weeks) < 0.22
    stress = {d: float(stress_week[(d - start).days // 7]) for d in dates}
    frac = {d: (d - start).days / max(1, days - 1) for d in dates}
    vit = 0.0          # day-to-day carry-over in energy (makes the reverse trap work)
    hrv_ar, rhr_ar = 0.0, 0.0

    # Bed and wake times are drawn up front: tonight's caffeine-on-board needs the bedtime
    # that actually follows (bed_h[i] is the night that ENDS on dates[i]).
    bed_hours = rng.normal(22.9, 0.6, size=days + 1)
    wake_hours = rng.normal(6.6, 0.3, size=days + 1)

    prev = {"sdu": 0.0, "hard": 0.0, "onboard": 0.0}
    for i, d in enumerate(dates):
        tz = zone(d)
        is_trip, is_sick = d in trip_days, d in sick_days
        S = stress[d]
        f = frac[d]

        # ── Last night's sleep (ends this morning; keyed by wake date d) ───────
        bed_h = float(bed_hours[i])                      # clock hour the evening before
        wake_h = float(wake_hours[i]) + (0.5 if is_sick else 0.0)
        awake_min = clip(rng.normal(25, 8), 5, 60)
        total_h = (24 - bed_h + wake_h) - awake_min / 60
        deep_h = clip(total_h * rng.normal(0.16, 0.02), 0.4, 2.0)
        rem_h = clip(total_h * rng.normal(0.22, 0.03), 0.6, 2.4)
        core_h = total_h - deep_h - rem_h
        bed_dt = _local(d, bed_h - 24, tz)
        wake_dt = _local(d, wake_h, tz)
        w.sleep.append({"date": hae(_local(d, 0, tz)), "sleepStart": hae(bed_dt), "sleepEnd": hae(wake_dt),
                        "inBedStart": hae(bed_dt - timedelta(minutes=10)), "inBedEnd": hae(wake_dt + timedelta(minutes=5)),
                        "totalSleep": round(total_h, 3), "deep": round(deep_h, 3), "core": round(core_h, 3),
                        "rem": round(rem_h, 3), "awake": round(awake_min / 60, 3)})

        # Overnight physiology, driven by YESTERDAY's drinks and training.
        hrv_ar = 0.5 * hrv_ar + rng.normal(0, 0.10)
        rhr_ar = 0.5 * rhr_ar + rng.normal(0, 1.8)
        hrv_true = (45 + 7 * f) * math.exp(-0.04 * prev["sdu"] - 0.07 * prev["hard"]
                                           - (0.25 if is_sick else 0.0) + hrv_ar)
        rhr_true = (58 - 3 * f) + 1.0 * prev["sdu"] + 0.3 * prev["hard"] + (5 if is_sick else 0) + rhr_ar
        resp = 14.6 + 0.08 * prev["sdu"] + (1.8 if is_sick else 0.0) + rng.normal(0, 0.25)
        for k in range(int(rng.integers(3, 6))):           # 3–5 spot readings, noisy like Apple's
            t = bed_dt + timedelta(minutes=float(rng.uniform(40, (wake_dt - bed_dt).total_seconds() / 60 - 30)))
            w.add("heart_rate_variability", t, hrv_true * math.exp(rng.normal(0, 0.22)))
        t = bed_dt + timedelta(minutes=2)
        while t < wake_dt:
            mins = (t - bed_dt).total_seconds() / 60
            early = 7 * math.exp(-mins / 70)
            w.add("heart_rate", t, rhr_true + 2.5 + early + rng.normal(0, 1.8))
            t += timedelta(minutes=5)
        t = bed_dt + timedelta(minutes=15)
        while t < wake_dt:
            w.add("respiratory_rate", t, resp + rng.normal(0, 0.4))
            t += timedelta(minutes=30)
        w.add("apple_sleeping_wrist_temperature", wake_dt - timedelta(minutes=20),
              rng.normal(0, 0.15) + (0.7 if is_sick else 0.0))

        # ── Morning check-in ───────────────────────────────────────────────────
        vit = 0.6 * vit + rng.normal(0, 0.30)
        energy = 3.3 + 0.30 * (total_h - 7.3) + vit - (1.0 if is_sick else 0.0) + rng.normal(0, 0.25)
        mood = 3.7 - 0.6 * S - (0.7 if is_sick else 0.0) + rng.normal(0, 0.40)
        stress_score = 2.2 + 0.9 * S + rng.normal(0, 0.45)

        # ── Today's behaviour ──────────────────────────────────────────────────
        dow = d.weekday()
        class_p = {0: 0.85, 2: 0.85, 4: 0.85, 1: 0.15, 3: 0.15, 5: 0.15, 6: 0.0}[dow]
        has_class = (not is_trip) and (not is_sick) and rng.random() < class_p
        has_walk = (not is_sick) and rng.random() < (0.75 if is_trip else 0.5)
        sdu = 0.0
        if not is_sick and rng.random() < 0.28:
            sdu = float(rng.choice([1.0, 1.0, 1.5, 2.0, 2.0, 3.0, 4.0]))

        hard = 0.0
        if has_class:
            hard = _class(w, d, tz, rng, wake_h, f)
        walk_steps = _walk(w, d, tz, rng) if has_walk else 0
        steps = int(clip(5200 + 2200 * (energy - 3.3) + walk_steps + rng.normal(0, 1200), 800, 30000))
        _day_activity(w, d, tz, rng, wake_h, steps, has_class)

        # Caffeine: a morning coffee, sometimes a second, plus one more in stress weeks.
        caf = []
        if rng.random() < 0.93:
            caf.append((wake_h + 0.7, float(rng.choice([95, 125, 150]))))
        if rng.random() < 0.45:
            caf.append((rng.uniform(12.5, 17.5), 95.0))
        if S and rng.random() < 0.85:
            caf.append((rng.uniform(10.0, 15.0), float(rng.choice([95, 125]))))
        for h, mg in caf:
            w.caffeine.append((d, _local(d, h, tz), mg))

        # Alcohol: drinks spaced through the evening.
        drinks = []
        if sdu:
            left, h = sdu, rng.uniform(18.0, 21.0)
            while left > 0:
                label, unit_sdu, kcal = DRINKS[int(rng.integers(0, len(DRINKS)))]
                amt = min(unit_sdu, left)
                drinks.append((h, amt, label, round(kcal * amt / unit_sdu)))
                left -= amt
                h += rng.uniform(0.6, 1.1)
            for h, amt, label, kcal in drinks:
                hh, mm = divmod(int(round(h * 60)), 60)
                w.alcohol.append((d, f"{hh % 24:02d}:{mm:02d}", round(amt, 2), label, kcal))

        # Food (incomplete logging on some days, flagged as such) and water.
        incomplete = rng.random() < (0.5 if is_trip else 0.05)
        meals = ["breakfast"] if incomplete else ["breakfast", "lunch", "dinner"] + (["snack"] if rng.random() < 0.6 else [])
        for meal in meals:
            name, kcal, p, c, fat, fib = MENU[meal][int(rng.integers(0, len(MENU[meal])))]
            scale = clip(rng.normal(1.0, 0.12), 0.7, 1.4)
            when = _local(d, MEAL_TIMES[meal] + rng.normal(0, 0.4), tz).replace(tzinfo=None)
            w.food.append((when, meal, name, round(kcal * scale), round(p * scale, 1), round(c * scale, 1),
                           round(fat * scale, 1), round(fib * scale, 1)))
        if incomplete:
            w.flags.append((d, "incomplete_data", "away" if is_trip else "manual"))
        if rng.random() < 0.8:
            for h in sorted(rng.uniform(8, 21, size=int(rng.integers(3, 6)))):
                hh, mm = divmod(int(h * 60), 60)
                w.water.append((d, f"{hh:02d}:{mm:02d}", float(rng.choice([12, 16, 20, 24]))))

        # Habits: independent of everything else.
        w.habits.append({"date": d, "creatine": bool(rng.random() < 0.85),
                         "supplements": bool(rng.random() < 0.7),
                         "meditation": bool(rng.random() < 0.2),
                         "outdoor_light_am": bool(rng.random() < 0.5)})

        # Weight, VO2, walking HR: slow trends.
        if rng.random() < 0.85:
            w.add("weight_body_mass", _local(d, wake_h + 0.3, tz), 141.5 - 3 * f + rng.normal(0, 0.7))
        if dow == 6:
            w.add("vo2_max", _local(d, 18, tz), 38.5 + 2.0 * f + rng.normal(0, 0.4))
        w.add("walking_heart_rate_average", _local(d, 21, tz), 104 - 4 * f + rng.normal(0, 2.0))
        w.add("basal_energy_burned", _local(d, 23.9, tz), 1400 + rng.normal(0, 20))

        # Caffeine on board at bedtime TONIGHT drives tomorrow's rated sleep; computed with
        # the app's own half-life so the planted effect sits on the engine's measure.
        next_bed = float(bed_hours[i + 1])
        onboard = sum(mg * 0.5 ** ((next_bed - h) / 5.0) for h, mg in caf if h < next_bed)

        sleep_q = 3.3 + 0.40 * (total_h - 7.3) - 0.006 * prev.get("onboard", 0.0) + rng.normal(0, 0.40)
        if rng.random() < 0.92:
            w.checkins.append({"date": d, "energy": round(clip(energy, 1, 5), 1), "mood": round(clip(mood, 1, 5), 1),
                               "stress": round(clip(stress_score, 1, 5), 1), "sleep_quality": round(clip(sleep_q, 1, 5), 1),
                               "sick": is_sick, "day_score": round(clip(rng.normal(6.8 - 0.8 * S, 1.0), 1, 10), 1)
                               if rng.random() < 0.75 else None})

        w.latent[d] = {"stress": S, "energy": energy, "sdu": sdu, "hard": hard, "sleep_h": total_h,
                       "onboard": onboard, "trip": is_trip, "sick": is_sick}
        prev = {"sdu": sdu, "hard": hard, "onboard": onboard}

    # Today: only the night and the morning have happened.
    return w


def _class(w: World, d: date, tz: ZoneInfo, rng, wake_h: float, f: float) -> float:
    """A 55-minute gym class at 06:00 (or 17:30 on Saturday). Returns its 'hardness' (≈1)."""
    start_h = 17.5 if d.weekday() == 5 else max(6.0, wake_h + 0.4)
    st = _local(d, start_h, tz)
    dur = 55 * 60
    kind = "High Intensity Interval Training" if rng.random() < 0.6 else "Traditional Strength Training"
    intensity = clip(rng.normal(1.0, 0.18), 0.6, 1.4)
    peak = 150 + 14 * intensity
    hrs = []
    t = st
    while t < st + timedelta(seconds=dur):
        m = (t - st).total_seconds() / 60
        if m < 8:
            base = 100 + (peak - 30 - 100) * m / 8
        elif m < 48:
            base = peak - 18 + 16 * math.sin(m / 2.2)
        elif m < 51:
            base = peak - 5                                  # the finish
        else:
            base = peak - 5 - 18 * (m - 51)                 # stretching, then stop
        hr = clip(base + rng.normal(0, 3), 70, 195)
        hrs.append(hr)
        w.add("heart_rate", t, hr)
        t += timedelta(seconds=10)
    kcal = 420 * intensity + rng.normal(0, 30)
    w.workouts.append({"id": f"demo-{d.isoformat()}-class", "name": kind, "start": hae(st),
                       "end": hae(st + timedelta(seconds=dur)), "duration": dur,
                       "activeEnergyBurned": {"qty": round(kcal, 1), "units": "kcal"},
                       "avgHeartRate": {"qty": round(sum(hrs) / len(hrs), 1)},
                       "maxHeartRate": {"qty": round(max(hrs), 1)},
                       "intensity": {"qty": round(6 + 3 * intensity, 1), "units": "kcal/hr·kg"}})
    w.add("apple_exercise_time", st + timedelta(minutes=30), 55)
    w.add("active_energy", st + timedelta(minutes=17), kcal)      # Apple counts workout kcal here too
    return intensity


def _walk(w: World, d: date, tz: ZoneInfo, rng) -> int:
    st = _local(d, rng.uniform(17.0, 18.5), tz)
    mins = int(rng.integers(30, 55))
    t, hrs = st, []
    while t < st + timedelta(minutes=mins):
        hr = rng.normal(104, 4)
        hrs.append(hr)
        w.add("heart_rate", t, hr)
        t += timedelta(seconds=30)
    kcal = 4.2 * mins + rng.normal(0, 15)
    w.workouts.append({"id": f"demo-{d.isoformat()}-walk", "name": "Outdoor Walk", "start": hae(st),
                       "end": hae(st + timedelta(minutes=mins)), "duration": mins * 60,
                       "activeEnergyBurned": {"qty": round(kcal, 1), "units": "kcal"},
                       "avgHeartRate": {"qty": round(sum(hrs) / len(hrs), 1)},
                       "maxHeartRate": {"qty": round(max(hrs), 1)}})
    w.add("apple_exercise_time", st + timedelta(minutes=mins / 2), mins * 0.6)
    w.add("active_energy", st + timedelta(minutes=11), kcal)
    w.add("time_in_daylight", st, mins * 0.9)
    w.add("walking_running_distance", st + timedelta(minutes=mins), mins * 0.055)
    return int(mins * 105)


def _day_activity(w: World, d: date, tz: ZoneInfo, rng, wake_h: float, steps: int, has_class: bool):
    """Hourly steps, active energy, stand hours and sparse daytime HR across the waking day."""
    hours = list(range(int(wake_h) + 1, 22))
    weights = rng.dirichlet(np.ones(len(hours)) * 3)
    for h, wt in zip(hours, weights):
        t = _local(d, h + 0.5, tz)
        s = steps * wt
        w.add("step_count", t, s)
        w.add("active_energy", t, 8 + s * 0.035 + rng.normal(0, 2))
        if rng.random() < 0.8:
            w.add("apple_stand_hour", t, 1)
        for q in (0.1, 0.35, 0.6, 0.85):
            w.add("heart_rate", _local(d, h + q, tz), rng.normal(76 + s / 400, 5))
    if rng.random() < 0.5:
        w.add("time_in_daylight", _local(d, wake_h + 1.2, tz), rng.uniform(8, 30))
