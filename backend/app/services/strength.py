"""Strength series derived from Hevy set-level data.

Extracted from the `/api/hevy/strength/e1rm` route body 2026-08-03 so the
gamification layer can reuse it (the scalar `epley_e1rm` was already a service
function in `hevy_sync.py`; the per-session aggregation was not). The router now
delegates here; behaviour is unchanged.

NOTE for the gamification layer: e1RM is the *detail* view, not Strength's spine.
This user's program is essentially all dumbbell accessory work, so e1RM series are
thin (low single-digit sessions for most lifts), isolation work frequently exceeds
Epley's useful rep range, and band-assisted movements produce nonsense
(`Pull Up (Band)` computes to ~90 kg; excluded since 2026-10-31). Per-session /
per-week tonnage is the robust signal — see docs/GAMIFICATION.md.
"""
import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import HevyExerciseTemplate, HevySet, HevyWorkout
from app.services.hevy_sync import epley_e1rm
from app.timeutils import to_local_date, local_today


# Hevy can't tell a set that builds muscle from conditioning: a burpee and a push-up are both
# reps_only with no weight. A loaded set counts; an unloaded one only if it's a bodyweight
# strength move. About half his class sets are conditioning or core circuits (2026-11-01), so
# counting them read a muscle as dosed when it wasn't. No reps = carries, holds and sprints.
BUILDING_MAX_REPS = 30
_BODYWEIGHT_STRENGTH = re.compile(
    r"push ?ups?\b|pull ?ups?\b|chin ?ups?\b|suspension|nordic|\bdips?\b|inverted row|"
    r"hamstring curl|lunge|skullcrusher", re.I)
_NOT_STRENGTH = re.compile(r"plank|inchworm|plyo|burpee|hip dip", re.I)


def counts_as_building(title: Optional[str], weight_kg: Optional[float], reps: Optional[int]) -> bool:
    if not reps or reps > BUILDING_MAX_REPS:
        return False
    if weight_kg:
        return True
    title = title or ""
    return bool(_BODYWEIGHT_STRENGTH.search(title)) and not _NOT_STRENGTH.search(title)


async def hard_sets_by_day(
    db: AsyncSession, start: date, end: date, building_only: bool = False,
) -> dict[date, dict[str, float]]:
    """{local date: {muscle group: hard sets}} over [start, end] inclusive.

    ONE definition for every consumer — the Training tab's weekly bars and the coach's
    pull-block line (added 2026-09-12; the rule used to live inline in the router). A
    hard set is any non-warmup set; primary muscle 1.0, each secondary 0.5; a set whose
    template has no primary group counts 1.0 to "other".
    """
    rows = (await db.execute(
        select(HevySet.exercise_template_id, HevySet.exercise_title, HevySet.weight_kg,
               HevySet.reps, HevyWorkout.start_time)
        .join(HevyWorkout, HevySet.hevy_workout_id == HevyWorkout.id)
        .where(HevySet.set_type != "warmup")
        # 1-day buffers: start/end are local dates, start_time is naive UTC
        .where(HevyWorkout.start_time >= start - timedelta(days=1))
        .where(HevyWorkout.start_time < end + timedelta(days=2))
    )).all()
    templates = {
        t.id: t for t in (await db.execute(select(HevyExerciseTemplate))).scalars().all()
    }
    out: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        d = to_local_date(r.start_time)
        if d < start or d > end:
            continue
        if building_only and not counts_as_building(r.exercise_title, r.weight_kg, r.reps):
            continue
        t = templates.get(r.exercise_template_id)
        if t is None or not t.primary_muscle_group:
            out[d]["other"] += 1.0
            continue
        out[d][t.primary_muscle_group] += 1.0
        for sec in t.secondary_muscle_groups or []:
            out[d][sec] += 0.5
    return {d: dict(g) for d, g in out.items()}


# The card's cap (note 92, 2026-10-31): his class sets are mostly 12–15 reps, so at the
# PR push's ≤ 10 a session like 10-29 (floor press 90 × 15) left the card unchanged.
# Rougher at 15 reps than at 5; the window delta matters more than the absolute number.
E1RM_CARD_MAX_REPS = 15


def rank_e1rm_lifts(best: dict[str, dict[str, dict]], top: int) -> list[str]:
    """Pure. Template ids, most recently done first (note 92: ranking by session count
    pinned the card to old staples). A lift needs 2 sessions to draw a line, so lifts
    with only one go last; session count breaks ties."""
    def key(tid: str) -> tuple:
        points = best[tid].values()
        return (len(points) >= 2, max(p["date"] for p in points), len(points))
    return sorted(best, key=key, reverse=True)[:top]


async def e1rm_by_exercise(
    db: AsyncSession,
    days: int = 365,
    top: int = 5,
) -> list[dict]:
    """Per-session best e1RM (Epley, reps ≤ E1RM_CARD_MAX_REPS) for the N lifts done most
    recently.

    Bodyweight/duration exercises never qualify (no weight). Assisted movements are
    excluded: their logged weight is the ASSISTANCE, so an e1RM of it is meaningless.
    """
    cutoff = local_today() - timedelta(days=days)
    rows = (await db.execute(
        select(
            HevySet.exercise_template_id,
            HevySet.exercise_title,
            HevySet.weight_kg,
            HevySet.reps,
            HevyWorkout.id.label("workout_id"),
            HevyWorkout.start_time,
        )
        .join(HevyWorkout, HevySet.hevy_workout_id == HevyWorkout.id)
        .outerjoin(HevyExerciseTemplate, HevySet.exercise_template_id == HevyExerciseTemplate.id)
        .where(HevySet.set_type != "warmup")
        .where(HevyWorkout.start_time >= cutoff)
        .where(func.coalesce(HevyExerciseTemplate.exercise_type, "") != "bodyweight_assisted")
    )).all()

    # best e1RM per (exercise, session)
    best: dict[str, dict[str, dict]] = defaultdict(dict)   # template_id → workout_id → point
    titles: dict[str, str] = {}
    for r in rows:
        e1rm = epley_e1rm(r.weight_kg, r.reps, max_reps=E1RM_CARD_MAX_REPS)
        if e1rm is None or not r.exercise_template_id:
            continue
        titles[r.exercise_template_id] = r.exercise_title or r.exercise_template_id
        cur = best[r.exercise_template_id].get(r.workout_id)
        if cur is None or e1rm > cur["e1rm"]:
            best[r.exercise_template_id][r.workout_id] = {
                "date": str(to_local_date(r.start_time)),
                "e1rm": round(e1rm, 1),
                "weight_kg": r.weight_kg,
                "reps": r.reps,
            }

    return [
        {
            "exercise_template_id": tid,
            "title": titles.get(tid, tid),
            "sessions": sorted(best[tid].values(), key=lambda p: p["date"]),
        }
        for tid in rank_e1rm_lifts(best, top)
    ]


# ── Rep-range bests (PR tracker, 2026-10-29) ──────────────────────────────────
#
# The user's sets are mostly class sets at fixed reps, warm-ups and time-capped rounds, so
# averages and per-session e1RM wobble with the class format. A best only moves when he
# actually lifts more: the heaviest load done for AT LEAST R reps, per bracket. One class made
# the case — the board's Epley target for 15 reps sat a third below what he then lifted.
# History starts at class_board.HISTORY_START (dumbbells logged per hand before 04-02).
REP_BRACKETS = (8, 10, 12, 15)
REP_BESTS_TOP = 10


def rep_bests_from_sets(sets_by_title: dict[str, list[tuple]], window_start: date,
                        top: int = REP_BESTS_TOP) -> dict:
    """Pure. sets_by_title: {title: [(date, lb, reps), ...]} (warm-ups already excluded).

    Lifts DONE in the window, most recent first (10-29: a new lift and today's lifts
    must show — the first cut ranked by session count with a ≥3-session floor and hid a
    first-ever Kettlebell Sumo Row). Per bracket: the best (load, date), when it was last
    MATCHED (a tie is real work even though it isn't a PR), and the best as of
    window_start. A PR beats the previous best on/after window_start; a bracket's first
    appearance is not a PR. `last` = the most recent session's sets.
    """
    lifts, prs = [], {}
    for title, sets in sets_by_title.items():
        days = {d for d, _, _ in sets}
        last_day = max(days)
        if last_day < window_start:
            continue
        best: dict[int, tuple] = {}
        matched: dict[int, date] = {}
        at_start: dict[int, float] = {}
        snap_done = False
        for d, lb, reps in sorted(sets, key=lambda s: (s[0], -s[1])):
            if not snap_done and d >= window_start:
                at_start = {r: v[0] for r, v in best.items()}
                snap_done = True
            if not lb or not reps:
                continue
            for r in REP_BRACKETS:
                if reps < r:
                    continue
                prev = best.get(r)
                if prev is None:
                    best[r] = (lb, d)
                elif lb > prev[0]:
                    if d >= window_start:
                        ev = prs.setdefault((title, d), {"title": title, "date": d.isoformat(),
                                                         "lb": lb, "reps": reps, "brackets": [],
                                                         "prev": {}})
                        if lb >= ev["lb"]:
                            ev["lb"], ev["reps"] = lb, reps
                        ev["brackets"].append(r)
                        ev["prev"][r] = prev[0]
                    best[r] = (lb, d)
                    matched.pop(r, None)
                elif lb == prev[0] and d > prev[1]:
                    matched[r] = d
        if not snap_done:
            at_start = {r: v[0] for r, v in best.items()}
        if not best:            # never done for 8+ reps (e.g. Bus Driver 15 × 6): no row to show
            continue
        last_sets = sorted([(lb, reps) for d, lb, reps in sets if d == last_day and lb],
                           key=lambda x: (-x[0], -x[1]))
        lifts.append({
            "title": title, "sessions": len(days), "last_date": last_day.isoformat(),
            "last_sets": [{"lb": lb, "reps": reps} for lb, reps in last_sets],
            "brackets": [{"reps": r, "best_lb": best[r][0], "best_date": best[r][1].isoformat(),
                          "matched_date": matched[r].isoformat() if r in matched else None,
                          "start_lb": at_start.get(r)}
                         for r in REP_BRACKETS if r in best],
        })
    lifts.sort(key=lambda l: (l["last_date"], l["sessions"]), reverse=True)
    lifts = lifts[:top]
    shown = {l["title"] for l in lifts}
    events = sorted((e for e in prs.values() if e["title"] in shown),
                    key=lambda e: e["date"], reverse=True)
    for e in events:
        e["brackets"] = sorted(set(e["brackets"]))
    return {"window_start": window_start.isoformat(), "lifts": lifts, "prs": events}


async def rep_bests(db: AsyncSession, days: int = 90, top: int = REP_BESTS_TOP) -> dict:
    from app.services.class_board import HISTORY_START
    from app.models import HevyExerciseTemplate
    rows = (await db.execute(
        select(HevySet.exercise_title, HevySet.weight_kg, HevySet.reps, HevyWorkout.start_time)
        .join(HevyWorkout, HevySet.hevy_workout_id == HevyWorkout.id)
        .join(HevyExerciseTemplate, HevyExerciseTemplate.id == HevySet.exercise_template_id)
        .where(HevyExerciseTemplate.exercise_type.in_(("weight_reps", "bodyweight_weighted")))
        .where((HevySet.set_type.is_(None)) | (HevySet.set_type != "warmup"))
        .where(HevyWorkout.start_time >= HISTORY_START)
    )).all()
    by_title: dict[str, list[tuple]] = defaultdict(list)
    for title, kg, reps, ts in rows:
        if title and kg:
            by_title[title].append((to_local_date(ts), round(kg * 2.20462), int(reps or 0)))
    return rep_bests_from_sets(by_title, local_today() - timedelta(days=days), top)
