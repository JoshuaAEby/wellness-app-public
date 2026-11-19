"""Anchor lifts — deliberate progression on a few chosen lifts (note 56, 2026-09-25).

Why: the six-month review found most loads flat (dumbbell curls identical for six months,
band pull-ups stuck at 5 reps on the same bands since June). Classes rotate exercises and
nothing asks for more load, so strength did not move while aerobic markers did. The user
chooses his own weights, so a simple rule applied to a few recurring lifts can fix that.

Chosen from the data (sessions since 2026-04), one per movement pattern — recurring,
loadable, compound. Romanian deadlift (5 sessions) is the natural fifth later.

Rule: DOUBLE PROGRESSION.
  weighted — work in [lo, hi] reps at one load. When EVERY working set reaches hi, add
             `step_lb` (combined; 5 lb per hand = the steps already used) and restart at lo.
             Otherwise keep the load and add a rep to the weakest set.
  assisted — the logged weight is the ASSISTANCE. Work in [lo, hi] reps with the lightest
             band used for ≥2 sets; when every set on it reaches hi, move to the next
             lighter assistance level he has used (or unassisted).
Dumbbells are logged as COMBINED weight with reps PER SIDE (2026-09-25).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

# The day the anchor program began (note 56). Progression is judged only from here: the
# earlier history mixes logging conventions (single vs combined dumbbell weight) and
# un-progressed class weights, so "first ever load → last" read floor press as 10 → 80.
ANCHORS_STARTED = date(2026, 9, 25)
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import HevySet, HevyWorkout
from app.timeutils import local_today, to_local_date

KG_TO_LB = 1 / 0.4536
# Progression is judged on the FIRST this-many sets at the working load; later sets are
# extra volume, not the test. (Judging the weakest set turned a row of 12/12/12 + 8/6
# drop-off sets into "consolidate at 8", 2026-09-25.)
WORKING_SETS = 3
RECENT_BEST_DAYS = 60

ANCHORS = [
    {"exercise": "Floor Press (Dumbbell)",   "short": "Floor press",  "pattern": "push",          "lo": 8, "hi": 12, "step_lb": 10},
    {"exercise": "Bent Over Row (Dumbbell)", "short": "DB row",       "pattern": "pull",          "lo": 8, "hi": 12, "step_lb": 10},
    {"exercise": "Reverse Lunge (Dumbbell)", "short": "Reverse lunge", "pattern": "legs",         "lo": 8, "hi": 12, "step_lb": 10},
    {"exercise": "Pull Up (Band)",           "short": "Band pull-up", "pattern": "vertical pull", "lo": 5, "hi": 8, "assisted": True},
    # Its OWN anchor, not an alias of the pull-up (2026-10-04): underhand grip and
    # usually easier, so merging the two would blur both progressions. Hevy logs it as a
    # separate exercise.
    {"exercise": "Chin Up (Assisted)",       "short": "Assisted chin-up", "pattern": "vertical pull (chin)", "lo": 5, "hi": 8, "assisted": True},
]


def _working(sets: list[tuple[float, int]], assisted: bool) -> tuple[Optional[float], list[int]]:
    """The load this session was 'about', and the reps done at it. Weighted: the
    heaviest load used for >=2 sets (else the heaviest). Assisted: the LIGHTEST
    assistance used for >=2 sets (else the lightest)."""
    by_load: dict[float, list[int]] = defaultdict(list)
    for lb, reps in sets:
        by_load[lb].append(reps)
    multi = [l for l, r in by_load.items() if len(r) >= 2] or list(by_load)
    load = (min(multi) if assisted else max(multi)) if multi else None
    return load, by_load.get(load, [])[:WORKING_SETS]


def _target(a: dict, load: float, reps: list[int], levels: list[float]) -> dict:
    lo, hi = a["lo"], a["hi"]
    if reps and min(reps) >= hi:
        if a.get("assisted"):
            lighter = [l for l in levels if l < load]
            nxt = max(lighter) if lighter else 0.0
            return {"action": "less_assist", "lb": nxt, "reps": lo,
                    "text": (f"{'unassisted' if nxt == 0 else f'{nxt:.0f} lb assist'} × {lo}"
                             f" — all sets hit {hi} on {load:.0f}")}
        return {"action": "add_weight", "lb": load + a["step_lb"], "reps": lo,
                "text": f"{load + a['step_lb']:.0f} lb × {lo} — all sets hit {hi} at {load:.0f}"}
    if reps and min(reps) >= lo:
        goal = min(hi, min(reps) + 1)
        unit = "lb assist" if a.get("assisted") else "lb"
        return {"action": "add_rep", "lb": load, "reps": goal,
                "text": f"{load:.0f} {unit} × {goal} on every set (last: {'/'.join(map(str, reps))})"}
    unit = "lb assist" if a.get("assisted") else "lb"
    return {"action": "consolidate", "lb": load, "reps": lo,
            "text": f"{load:.0f} {unit} × {lo} on every set (last: {'/'.join(map(str, reps)) or '—'})"}


async def anchor_status(db: AsyncSession) -> dict:
    names = [a["exercise"] for a in ANCHORS]
    rows = (await db.execute(
        select(HevyWorkout.start_time, HevySet.exercise_title, HevySet.weight_kg, HevySet.reps)
        .join(HevyWorkout, HevyWorkout.id == HevySet.hevy_workout_id)
        .where(HevySet.exercise_title.in_(names))
        .where((HevySet.set_type.is_(None)) | (HevySet.set_type == "normal"))
        .where(HevySet.reps > 0)
        .order_by(HevyWorkout.start_time)
    )).all()
    per: dict[str, dict[date, list[tuple[float, int]]]] = defaultdict(lambda: defaultdict(list))
    for ts, ex, kg, reps in rows:
        per[ex][to_local_date(ts)].append((round((kg or 0) * KG_TO_LB), int(reps)))

    today = local_today()
    out = []
    for a in ANCHORS:
        sessions = per.get(a["exercise"], {})
        if not sessions:
            out.append({**a, "sessions": 0, "target": None})
            continue
        days = sorted(sessions)
        assisted = bool(a.get("assisted"))
        levels = sorted({lb for d in days for lb, _ in sessions[d]})
        history = []
        for d in days:
            load, reps = _working(sessions[d], assisted)
            history.append({"date": d.isoformat(), "load": load, "reps": reps,
                            "best_reps": max(reps) if reps else None})
        last = history[-1]
        # Baseline = the last session ON OR BEFORE the program began (the load you started
        # from); progress = later sessions against it. Counting only post-start sessions
        # threw the starting point away and left every lift "too early".
        loaded = [h for h in history if h["load"] is not None]
        pre = [h for h in loaded if date.fromisoformat(h["date"]) <= ANCHORS_STARTED]
        post = [h for h in loaded if date.fromisoformat(h["date"]) > ANCHORS_STARTED]
        # Heaviest (or least-assisted) working load in the last RECENT_BEST_DAYS where every
        # judged set reached the bottom of the range — context beside the rule's target,
        # since he picks his own weights (a lift's last session can sit well under an earlier, heavier one).
        recent_ok = [h for h in history
                     if (today - date.fromisoformat(h["date"])).days <= RECENT_BEST_DAYS
                     and h["reps"] and min(h["reps"]) >= a["lo"] and h["load"] is not None]
        best = (min(recent_ok, key=lambda h: h["load"]) if assisted
                else max(recent_ok, key=lambda h: h["load"])) if recent_ok else None
        out.append({
            **a,
            "sessions": len(days),
            "last_date": last["date"],
            "days_since": (today - days[-1]).days,
            "last_sets": [{"lb": lb, "reps": r} for lb, r in sessions[days[-1]]],
            "working_load": last["load"],
            "target": _target(a, last["load"], last["reps"], levels) if last["load"] is not None else None,
            "history": history[-12:],
            # Scorecard input (note 67): first vs latest working load SINCE the program began.
            "since_start": {"sessions": len(post),
                            "baseline_load": pre[-1]["load"] if pre else None,
                            "baseline_date": pre[-1]["date"] if pre else None,
                            "last_load": post[-1]["load"] if post else None,
                            "last_date": post[-1]["date"] if post else None,
                            "loads": [{"date": h["date"], "load": h["load"]}
                                      for h in (pre[-1:] + post)]},
            "recent_best": ({"date": best["date"], "load": best["load"], "reps": best["reps"]}
                            if best and best["date"] != last["date"] else None),
        })
    return {"anchors": out}
