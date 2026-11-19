"""Class board → weight targets (note 91, 2026-10-29).

The user's gym writes the day's exercises on a whiteboard; he sees it ~5 minutes before the
class starts and picks his own weights. A photo of the board goes to a vision model that
reads each line AND maps it to the names he already logs in Hevy (the catalog is in the
prompt). The targets are then computed HERE, deterministically, from his own sets — the
model never invents a weight.

Target rule (per matched exercise, from the 2026-04-02 logging switch on):
  1. Anchor = the most recent session whose working reps sit within ±REP_ZONE of today's
     rep target. From it, DOUBLE PROGRESSION — the same rule as services/anchor_lifts:
     every judged set reached the target → one step up; otherwise same load.
  2. No session at a similar rep count → Epley from the last ESTIMATE_DAYS of sets,
     solved for target + 1 reps (one in reserve), rounded DOWN to the step.
  3. Assisted (band) movements: the logged weight is the ASSISTANCE — every judged set
     reached the target (or 8 for MAX) → the next lighter band he has used.
  4. Carries / holds (Hevy distance or time types): progress on load — see carry_target.
  Every target also carries `easy_lb` = the weight WITHOUT this session's step up, for the
  Easy day switch (the user's call when he's up for class but not for pushing).
  Medicine-ball lines step through the balls that exist (MEDICINE_BALL_LB), not +5.
Readiness is shown beside the targets but never moves them: Δ est. 1RM vs the previous
session showed ρ −0.13 against that morning's readiness (172 pairs, 2026-10-29).
"""
from __future__ import annotations

import base64
import json
import logging
import time
from collections import defaultdict
from datetime import date, timedelta
from statistics import median
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DailyMetric, HevyExerciseTemplate, HevySet, HevyWorkout
from app.services.anchor_lifts import KG_TO_LB, WORKING_SETS, _working
from app.timeutils import local_today, to_local_date

logger = logging.getLogger(__name__)

# Default model: Opus 5.5 at low effort. Chosen 2026-10-29 on three sample boards
# over faster models: in a latency/accuracy comparison it gave identical matches every run.
BOARD_MODEL = "claude-opus-5-5"
BOARD_EFFORT = "low"

# Dumbbell weights were logged PER HAND until this date and COMBINED from it (step
# change found 2026-10-29: 161 exercise-days, single best split 04-02). Older sets are
# ignored rather than doubled — the user hasn't decided whether to rewrite them.
HISTORY_START = date(2026, 4, 2)
CATALOG_SINCE = date(2026, 3, 15)
REP_ZONE = 3            # a past session "matches" today's rep target within ± this
ANCHOR_DAYS = 120       # how far back an anchor session may be
ESTIMATE_DAYS = 60      # window for the Epley fallback
MAX_EPLEY_REPS = 20     # beyond this Epley is too loose even for a rough guide
ASSISTED_MAX_TARGET = 8  # "x MAX" on an assisted movement: progress once every set hits this

# Exercises that use ONE implement although the title says Dumbbell; "combined" doesn't
# apply, so the step is one dumbbell size, not a pair.
_SINGLE_IMPLEMENT = ("single", "one arm", "1-arm", "goblet", "pullover", "snatch",
                     "woodchop", "side bend", "triceps extension", "mummy", "swing",
                     "kickback")       # one arm at a time (2026-11-16)


# Medicine balls at his gym, lb: every size seen on his medicine-ball exercises since April
# (15–45), and the user confirmed 40s exist (2026-10-29). Only used when the board says MB.
MEDICINE_BALL_LB = (15.0, 20.0, 25.0, 30.0, 35.0, 40.0, 45.0)


def step_lb(title: str) -> float:
    """Smallest sensible load change for this exercise, in lb (combined for pairs)."""
    t = title.lower()
    if "dumbbell" in t or "dumbell" in t:
        return 5.0 if any(k in t for k in _SINGLE_IMPLEMENT) else 10.0
    if "farmer" in t and "single" not in t:     # two implements, logged combined
        return 10.0
    return 5.0


def epley(lb: float, reps: int) -> float:
    return lb * (1 + reps / 30.0)


def load_for_reps(e1rm: float, reps: int) -> float:
    """Inverse Epley: the load that leaves `reps` as the last rep possible."""
    return e1rm / (1 + reps / 30.0)


# ── Catalog: the names he already logs ────────────────────────────────────────

async def exercise_catalog(db: AsyncSession) -> list[dict]:
    """Every exercise logged since CATALOG_SINCE: title, Hevy type, sessions, last date."""
    rows = (await db.execute(
        select(HevySet.exercise_title, HevyExerciseTemplate.exercise_type,
               func.count(func.distinct(HevySet.hevy_workout_id)),
               func.max(HevyWorkout.start_time))
        .join(HevyWorkout, HevyWorkout.id == HevySet.hevy_workout_id)
        .outerjoin(HevyExerciseTemplate,
                   HevyExerciseTemplate.id == HevySet.exercise_template_id)
        .where(HevyWorkout.start_time >= CATALOG_SINCE)
        .group_by(HevySet.exercise_title)
    )).all()
    out = [{"title": t, "type": typ or "unknown", "sessions": n,
            "last": to_local_date(last).isoformat() if last else None}
           for t, typ, n, last in rows if t]
    out.sort(key=lambda r: (-r["sessions"], r["title"]))
    return out


# ── Parsing the board ─────────────────────────────────────────────────────────

BOARD_SYSTEM = """You read a photo of a group-fitness class whiteboard for one athlete and \
turn it into a list of exercises, each mapped onto the name the athlete uses in his \
workout log. Output JSON only, no prose."""

BOARD_INSTRUCTION = """Read every exercise on this whiteboard, in board order.

How this gym writes boards:
- Lines are numbered; boards may be split into circuits (e.g. "Q1:", "Q2:"). Keep the label.
- This gym's abbreviations (from the athlete, 2026-10-29): DB = dumbbell, KB = \
kettlebell, MB = medicine ball, RB = resistance band, BB = burn bar (a standing bar he \
holds on to or jumps over — NOT barbell; his log calls it "Bar-based ..."), HB = heavy \
bag (punching bag he lies on, flips or jumps over), BK = back, FRT = front, DK = donkey \
(as in donkey kick), DL = deadlift, RDL = Romanian deadlift. Also seen: SB = stability \
ball, BJ = broad jump, HRB = heavy resistance band, Wt. = weighted, TC = a long \
half-cylinder pad (domed rubber on one side, flat on the other — like a long BOSU) he \
jumps over or lies on; his log keeps the "TC" prefix. "Ledge" is a raised platform. \
Abbreviations you can't resolve: keep them literally, don't guess.
- Rep notation: "x15" = 15 reps per set. "x 5-5-5" under a "Lft - Rt - Bl" header = 5 \
left, 5 right, 5 both in one set. "x MAX" = as many as possible. "x3 laps" / distances / \
times are not reps. Ratios like "(2:1)" are notes.
- reps_target = the reps EACH working limb does in one set: x15 → 15; Lft-Rt-Bl 5-5-5 → \
10 (5 one side + 5 both); MAX, laps, times, or no number → null.

The athlete's log names (title | type | sessions since March), most used first:
{catalog}

For each exercise give "match": the EXACT title from that list that is the same movement \
with the same implement, or null when none is. Prefer a title he has logged more often \
when two are equally good. "alternatives": up to 3 other exact titles that are close \
(same movement, other implement, or a near variant). "loadable": true when the athlete \
picks a weight or band for it (dumbbell, kettlebell, bar, med ball, band, weighted), \
false for pure bodyweight / cardio / agility.

Return exactly:
{{"exercises": [{{"n": 1, "group": "Q1" or null, "board_text": "the line as written", \
"name": "plain-English name", "implement": "dumbbell|kettlebell|barbell|body_bar|\
medicine_ball|stability_ball|band|bosu|bodyweight|other|unknown", "reps_target": 15 or \
null, "rep_scheme": "as written, e.g. x15 or 5-5-5 L/R/both or MAX", "match": "exact \
title" or null, "match_confidence": "high|medium|low", "alternatives": [], "loadable": \
true}}], "unreadable": ["any text you could not read"]}}"""


def _catalog_lines(catalog: list[dict]) -> str:
    return "\n".join(f"{c['title']} | {c['type']} | {c['sessions']}" for c in catalog)


async def parse_board(catalog: list[dict], image: Optional[bytes] = None,
                      media_type: Optional[str] = None, text: Optional[str] = None,
                      model: str = BOARD_MODEL,
                      effort: Optional[str] = BOARD_EFFORT) -> Optional[dict]:
    """One call: board photo (or the board typed out) → exercises mapped to catalog
    titles. No DB write.

    Titles the model returns that are NOT in the catalog are dropped to null (and kept in
    `match_raw`), so a hallucinated name can never reach the target lookup.
    """
    from app.config import settings as app_settings
    from app.services import ai_health
    from app.services.ai_summaries import (_create_complete, _log_call,
                                           _strip_json_fence, _text_of)

    if not app_settings.anthropic_api_key:
        ai_health.record_disabled("No ANTHROPIC_API_KEY configured.")
        return None

    import anthropic
    client = anthropic.AsyncAnthropic(api_key=app_settings.anthropic_api_key)
    instruction = BOARD_INSTRUCTION.format(catalog=_catalog_lines(catalog))
    if image is not None:
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                         "data": base64.standard_b64encode(image).decode()}},
            {"type": "text", "text": instruction},
        ]
    else:
        content = [{"type": "text", "text": "The board, typed out by the athlete:\n"
                                            f"{text}\n\n"
                                            + instruction.replace("on this whiteboard",
                                                                  "on this board")}]
    kwargs = {"model": model, "max_tokens": 8000,
              "system": [{"type": "text", "text": BOARD_SYSTEM}],
              "messages": [{"role": "user", "content": content}]}
    if effort:
        kwargs["output_config"] = {"effort": effort}
    t0 = time.monotonic()
    try:
        response = await _create_complete(client, kwargs, "class_board")
        data = json.loads(_strip_json_fence(_text_of(response)))
        await _log_call("class_board", response.usage.input_tokens,
                        response.usage.output_tokens, model=model)
    except Exception as exc:
        ai_health.record_failure("class_board", exc)
        logger.warning("class board parse failed: %s", exc)
        return None

    titles = {c["title"] for c in catalog}
    for ex in data.get("exercises", []):
        m = ex.get("match")
        if m and m not in titles:
            ex["match_raw"], ex["match"] = m, None
        ex["alternatives"] = [a for a in (ex.get("alternatives") or []) if a in titles][:3]
        rt = ex.get("reps_target")
        ex["reps_target"] = int(rt) if isinstance(rt, (int, float)) and rt > 0 else None
    data["model"] = model
    data["ms"] = round((time.monotonic() - t0) * 1000)
    data["usage"] = {"input": response.usage.input_tokens,
                     "output": response.usage.output_tokens}
    return data


# ── Targets ───────────────────────────────────────────────────────────────────

async def _sessions(db: AsyncSession, title: str) -> dict[date, list[tuple[float, int]]]:
    """{local date: [(lb, reps), ...]} for this exercise since HISTORY_START, in set order."""
    rows = (await db.execute(
        select(HevyWorkout.start_time, HevySet.weight_kg, HevySet.reps)
        .join(HevyWorkout, HevyWorkout.id == HevySet.hevy_workout_id)
        .where(HevySet.exercise_title == title)
        .where((HevySet.set_type.is_(None)) | (HevySet.set_type != "warmup"))
        .where(HevyWorkout.start_time >= HISTORY_START)
        .order_by(HevyWorkout.start_time, HevySet.exercise_index, HevySet.set_index)
    )).all()
    out: dict[date, list[tuple[float, int]]] = defaultdict(list)
    for ts, kg, reps in rows:
        out[to_local_date(ts)].append((round((kg or 0) * KG_TO_LB), int(reps or 0)))
    return out


def _fmt_sets(sets: list[tuple[float, int]], unit: str = "") -> str:
    parts = []
    for lb, reps in sets:
        r = str(reps) if reps else "—"
        parts.append(f"{lb:g}{unit}×{r}" if lb else f"×{r}")
    return ", ".join(parts)


def _next_up(load: float, step: float, sizes: Optional[tuple]) -> float:
    """One step heavier: the next implement that exists, else load + step."""
    if sizes:
        up = [s for s in sizes if s > load]
        return up[0] if up else load
    return load + step


def _round_down(raw: float, step: float, sizes: Optional[tuple]) -> float:
    if sizes:
        fit = [s for s in sizes if s <= raw]
        return fit[-1] if fit else sizes[0]
    return max(step, (raw // step) * step)


# Targets start from his BEST, not his typical (2026-10-29: "fix on my PRs rather than
# managing the average"). Many logged sets are warm-ups, time-capped or equipment-limited
# class sets, so the latest similar session and Epley from it understate him — in one class the
# rule said a weight well under what he then lifted for the same reps. Back-test over 125 class sessions since
# 06-01 (target from history before each day vs what he chose): within one step 55% → 59%,
# more than a step too LOW 20% → 13%; the gain is mostly in the estimate case (too low
# 40% → 10%).
BEST_DAYS = 60


def recommend_from_sessions(title: str, ex_type: str, sessions: dict[date, list],
                            reps_target: Optional[int], today: date,
                            sizes: Optional[tuple] = None) -> dict:
    """The base rule (_base_target), then never below his recent best: the heaviest load
    done for at least the target reps in the last BEST_DAYS, and — when the target is an
    Epley estimate — never below his last working load."""
    rec = _base_target(title, ex_type, sessions, reps_target, today, sizes)
    t = rec.get("target_lb")
    if (t is None or rec.get("unit") != "lb"
            or ex_type not in ("weight_reps", "bodyweight_weighted")):
        return rec
    goal = reps_target or (median(r for _, r in sessions[max(sessions)]) if sessions else None)
    floor, why = t, None
    for d in sorted(sessions):
        if (today - d).days > BEST_DAYS or not goal:
            continue
        for lb, r in sessions[d]:
            if lb and r >= goal and lb > floor:
                floor, why = lb, f"your best for {goal}+ reps in {BEST_DAYS} days ({d.strftime('%-m/%-d')})"
    if rec.get("status") == "estimate":
        last = _working(sessions[max(sessions)], assisted=False)[0]
        if last and last > floor:
            floor, why = last, "your last working weight"
    if why:
        rec = {**rec, "status": "best", "basis": "recent_best", "target_lb": floor,
               "easy_lb": min(rec.get("easy_lb", floor), floor),
               "text": f"{floor:g} lb × {goal} — {why}; rule said {t:g}"}
    return rec


def _base_target(title: str, ex_type: str, sessions: dict[date, list],
                 reps_target: Optional[int], today: date,
                 sizes: Optional[tuple] = None) -> dict:
    """The target for one exercise from its session history. See module docstring.

    `sizes`: the loads that physically exist for this implement (medicine balls), so a
    step up lands on a real ball — "45 lb" for a sit-up when the rack stops at 40 was
    the first mistake seen against a real class (10-27).
    """
    days = sorted(sessions)
    if not days:
        return {"status": "no_history",
                "text": "No history since April — pick a weight that leaves 1–2 reps."}
    last_day = days[-1]
    last = {"date": last_day.isoformat(), "sets": [{"lb": lb, "reps": r}
                                                   for lb, r in sessions[last_day]]}
    base = {"last": last, "sessions": len(days)}

    if ex_type == "bodyweight_assisted":
        levels = sorted({lb for d in days for lb, _ in sessions[d]})
        load, reps = _working(sessions[last_day], assisted=True)
        goal = reps_target or ASSISTED_MAX_TARGET
        if reps and min(reps) >= goal:
            lighter = [l for l in levels if l < (load or 0)]
            nxt = max(lighter) if lighter else 0.0
            txt = "unassisted" if nxt == 0 else f"{nxt:g} lb band"
            return {**base, "status": "progress", "target_lb": nxt, "unit": "lb assist",
                    "easy_lb": load,
                    "text": f"{txt} — every set reached {goal} on {load:g} last time"}
        return {**base, "status": "hold", "target_lb": load, "unit": "lb assist",
                "easy_lb": load,
                "text": f"{load:g} lb band, aim for {goal} per set "
                        f"(last: {'/'.join(map(str, reps)) or '—'})"}

    if ex_type not in ("weight_reps", "bodyweight_weighted"):
        txt = f"Last {last_day.strftime('%-m/%-d')}: {_fmt_sets(sessions[last_day])}"
        return {**base, "status": "info", "text": txt}

    step = step_lb(title)
    # 1. Anchor: the most recent session at a similar rep count.
    anchor = None
    for d in reversed(days):
        if (today - d).days > ANCHOR_DAYS:
            break
        load, reps = _working(sessions[d], assisted=False)
        if not load or not reps:
            continue
        if reps_target is None or abs(median(reps) - reps_target) <= REP_ZONE:
            anchor = (d, load, reps)
            break
    goal = reps_target
    if anchor:
        d, load, reps = anchor
        goal = goal or int(round(median(reps)))
        judged = reps[:WORKING_SETS]
        when = d.strftime("%-m/%-d")
        up = _next_up(load, step, sizes)
        if min(judged) >= goal and up > load:
            return {**base, "status": "progress", "target_lb": up, "unit": "lb",
                    "easy_lb": load, "basis": "anchor", "anchor_date": d.isoformat(),
                    "text": f"{up:g} lb × {goal} — all sets hit {goal} at "
                            f"{load:g} on {when} ({'/'.join(map(str, judged))})"}
        if min(judged) >= goal:      # already the heaviest that exists: add reps instead
            return {**base, "status": "hold", "target_lb": load, "unit": "lb",
                    "easy_lb": load, "basis": "anchor", "anchor_date": d.isoformat(),
                    "text": f"{load:g} lb (the heaviest) — beat {'/'.join(map(str, judged))} "
                            f"from {when}"}
        return {**base, "status": "hold", "target_lb": load, "unit": "lb",
                "easy_lb": load, "basis": "anchor", "anchor_date": d.isoformat(),
                "text": f"{load:g} lb × {goal} — {when} was {'/'.join(map(str, judged))} "
                        f"at {load:g}"}

    # 2. Different rep range: estimate from recent sets.
    recent = [(lb, r) for d in days if (today - d).days <= ESTIMATE_DAYS
              for lb, r in sessions[d] if lb and 0 < r <= MAX_EPLEY_REPS]
    if recent and goal:
        e1 = max(epley(lb, r) for lb, r in recent)
        raw = load_for_reps(e1, goal + 1)
        target = _round_down(raw, step, sizes)
        return {**base, "status": "estimate", "target_lb": target, "unit": "lb",
                "easy_lb": target,
                "basis": "epley", "e1rm_lb": round(e1),
                "text": f"{target:g} lb × {goal} — estimated from recent sets "
                        f"(none at ~{goal} reps); last {last_day.strftime('%-m/%-d')}: "
                        f"{_fmt_sets(sessions[last_day])}"}
    return {**base, "status": "info",
            "text": f"Last {last_day.strftime('%-m/%-d')}: {_fmt_sets(sessions[last_day])}"}


CARRY_TYPES = ("short_distance_weight", "weight_duration")


async def _carry_sessions(db: AsyncSession, title: str) -> dict[date, list[tuple]]:
    """{local date: [(lb, distance m, seconds), ...]} — carries, holds, loaded walks."""
    rows = (await db.execute(
        select(HevyWorkout.start_time, HevySet.weight_kg, HevySet.distance_meters,
               HevySet.duration_seconds)
        .join(HevyWorkout, HevyWorkout.id == HevySet.hevy_workout_id)
        .where(HevySet.exercise_title == title)
        .where((HevySet.set_type.is_(None)) | (HevySet.set_type != "warmup"))
        .where(HevyWorkout.start_time >= HISTORY_START)
        .order_by(HevyWorkout.start_time, HevySet.exercise_index, HevySet.set_index)
    )).all()
    out: dict[date, list[tuple]] = defaultdict(list)
    for ts, kg, dist, secs in rows:
        if kg:
            out[to_local_date(ts)].append((round(kg * KG_TO_LB), dist, secs))
    return out


def carry_target(title: str, sessions: dict[date, list[tuple]],
                 sizes: Optional[tuple] = None) -> dict:
    """Pure: carries/holds progress on LOAD — the class fixes the distance or time.

    Same idea as double progression: own the top load first. If last session's heaviest
    load was carried on ≥2 sets, go one step up; otherwise do every set at that load
    (laps at three rising loads → "the top load on every lap", not one step higher).
    """
    days = sorted(sessions)
    if not days:
        return {"status": "no_history",
                "text": "No history since April — pick a load you can hold to the end."}
    sets = sessions[days[-1]]
    when = days[-1].strftime("%-m/%-d")
    shown = ", ".join(f"{lb:g}" + (f"×{d:g} m" if d else f"×{s}s" if s else "")
                      for lb, d, s in sets)
    top = max(lb for lb, _, _ in sets)
    n_top = sum(1 for lb, _, _ in sets if lb == top)
    base = {"last": {"date": days[-1].isoformat(),
                     "sets": [{"lb": lb, "m": d, "s": s} for lb, d, s in sets]},
            "sessions": len(days), "unit": "lb"}
    if n_top >= 2:
        up = _next_up(top, step_lb(title), sizes)
        return {**base, "status": "progress", "target_lb": up, "easy_lb": top,
                "text": f"{up:g} lb — {n_top} sets at {top:g} on {when} ({shown})"}
    return {**base, "status": "hold", "target_lb": top, "easy_lb": top,
            "text": f"{top:g} lb on every set — {when} reached it once ({shown})"}


async def recommend(db: AsyncSession, title: Optional[str], ex_type: Optional[str],
                    reps_target: Optional[int], implement: Optional[str] = None) -> dict:
    if not title:
        return {"status": "no_match", "text": "Not in your log — choose a match or go by feel."}
    sizes = MEDICINE_BALL_LB if implement == "medicine_ball" else None
    if ex_type in CARRY_TYPES:
        rec = carry_target(title, await _carry_sessions(db, title), sizes)
    else:
        sessions = await _sessions(db, title)
        rec = recommend_from_sessions(title, ex_type or "unknown", sessions, reps_target,
                                      local_today(), sizes)
    # Pairs are logged COMBINED; at the rack he picks two dumbbells, so say which.
    if rec.get("unit") == "lb" and rec.get("target_lb") and step_lb(title) == 10:
        rec["per_hand"] = rec["target_lb"] / 2
        if rec.get("easy_lb"):
            rec["easy_per_hand"] = rec["easy_lb"] / 2
    return rec


async def readiness_today(db: AsyncSession) -> Optional[dict]:
    row = (await db.execute(
        select(DailyMetric.readiness_score, DailyMetric.readiness_label)
        .where(DailyMetric.date == local_today())
    )).first()
    if not row or row[0] is None:
        return None
    return {"score": round(row[0]), "label": row[1]}


async def board_with_targets(db: AsyncSession, parsed: dict, catalog: list[dict]) -> list[dict]:
    types = {c["title"]: c["type"] for c in catalog}
    out = []
    for ex in parsed.get("exercises", []):
        # A learned name (his pick, or what he logged after an earlier board) beats the
        # model's guess for the same wording.
        learned = await learned_title(db, ex)
        if learned and learned in types and learned != ex.get("match"):
            ex = {**ex, "match": learned, "match_confidence": "learned",
                  "match_model": ex.get("match")}
        rec = await recommend(db, ex.get("match"), types.get(ex.get("match") or ""),
                              ex.get("reps_target"), ex.get("implement"))
        out.append({**ex, "type": types.get(ex.get("match") or ""), "rec": rec})
    return out


# ── Closing the loop: targets vs what he logged, and learned names (0085) ─────
#
# After each Hevy sync (scheduler._hevy_sync) every board from the last CHECK_DAYS is
# compared with that day's logged sets. Board lines whose match he didn't log are paired
# with an unclaimed logged exercise by shared words — that pairing, and every "change"
# pick, becomes an alias so the same wording matches directly next time (10-27: "KB Sumo
# Squat High Pull" was matched to Kettlebell Sumo Deadlift High Pull; he logs Kettlebell
# High Pull).
import re as _re

CHECK_DAYS = 3
MIN_SHARED_WORDS = 2
MIN_OVERLAP = 0.6       # shared words / words in the shorter name


def alias_key(text: Optional[str]) -> str:
    """Board wording → a stable key: no numbering, no rep scheme, no punctuation, singular."""
    t = (text or "").lower()
    t = _re.sub(r"^\W*\d+\s*[\.\)]?", " ", t)                       # "4." / "1)"
    t = _re.sub(r"\s[x×]\s*(\d|max).*$", " ", t)                     # "x15", "x 5-5-5", "x MAX"
    words = [w[:-1] if len(w) > 2 and w.endswith("s") and not w.endswith(("ss", "us")) else w
             for w in _re.findall(r"[a-z0-9]+", t)]
    return " ".join(words)


def _words(title: str) -> set:
    return set(alias_key(_re.sub(r"\(.*?\)", " ", title)).split()) | (
        {w for w in alias_key(title).split()})


def pair_unclaimed(items: list[dict], logged: list[str]) -> dict[int, str]:
    """Pure: board lines (index → name) → logged titles nobody claimed, by shared words.
    Greedy on the best overlap; each logged title used once."""
    cands = []
    for i, it in enumerate(items):
        a = set(alias_key(it.get("name") or it.get("board_text")).split())
        for t in logged:
            b = _words(t)
            shared = len(a & b)
            if shared >= MIN_SHARED_WORDS and shared / max(1, min(len(a), len(b))) >= MIN_OVERLAP:
                cands.append((shared / min(len(a), len(b)), shared, i, t))
    out, used = {}, set()
    for _, _, i, t in sorted(cands, reverse=True):
        if i not in out and t not in used:
            out[i], used = t, used | {t}
    return out


def verdict(rec: dict, sets: list[tuple[float, int]]) -> dict:
    """Pure: what he did vs the target. Assisted = the band (lower is better)."""
    if not sets:
        return {"verdict": "not_logged"}
    assisted = rec.get("unit") == "lb assist"
    load, reps = _working(sets, assisted=assisted)
    out = {"actual_lb": load, "actual_reps": reps}
    t = rec.get("target_lb")
    if t is None or load is None:
        return {**out, "verdict": "logged"}
    if load == t:
        return {**out, "verdict": "hit"}
    better = load < t if assisted else load > t
    return {**out, "verdict": "beat" if better else "under"}


async def remember_alias(db: AsyncSession, texts: list[Optional[str]], title: str,
                         source: str) -> None:
    from app.models import ClassAlias
    from datetime import datetime as _dt
    for k in {alias_key(t) for t in texts if t}:
        if not k:
            continue
        row = (await db.execute(select(ClassAlias).where(ClassAlias.key == k))).scalar_one_or_none()
        if row is None:
            db.add(ClassAlias(key=k, title=title, source=source, seen=1, updated_at=_dt.utcnow()))
        elif row.title != title or source == "pick":
            # A pick always wins; a log only replaces a log.
            if source == "pick" or row.source != "pick":
                row.title, row.source, row.seen, row.updated_at = title, source, 1, _dt.utcnow()
        else:
            row.seen = (row.seen or 0) + 1
            row.updated_at = _dt.utcnow()


async def learned_title(db: AsyncSession, item: dict) -> Optional[str]:
    from app.models import ClassAlias
    keys = [alias_key(item.get("board_text")), alias_key(item.get("name"))]
    rows = (await db.execute(select(ClassAlias).where(ClassAlias.key.in_([k for k in keys if k]))
                             )).scalars().all()
    rows.sort(key=lambda r: (r.source != "pick", -(r.seen or 0)))
    return rows[0].title if rows else None


async def check_boards(db: AsyncSession) -> int:
    """Compare boards from the last CHECK_DAYS with that day's Hevy log. Returns boards checked."""
    from app.models import ClassBoard
    from datetime import datetime as _dt
    from app.timeutils import local_day_bounds_utc
    boards = (await db.execute(select(ClassBoard).where(
        ClassBoard.date >= local_today() - timedelta(days=CHECK_DAYS)))).scalars().all()
    done = 0
    for b in boards:
        lo, hi = local_day_bounds_utc(b.date)
        rows = (await db.execute(
            select(HevySet.exercise_title, HevySet.weight_kg, HevySet.reps)
            .join(HevyWorkout, HevyWorkout.id == HevySet.hevy_workout_id)
            .where(HevyWorkout.start_time >= lo).where(HevyWorkout.start_time < hi)
            .where((HevySet.set_type.is_(None)) | (HevySet.set_type != "warmup"))
            .order_by(HevyWorkout.start_time, HevySet.exercise_index, HevySet.set_index)
        )).all()
        if not rows:
            continue
        logged: dict[str, list] = defaultdict(list)
        for t, kg, reps in rows:
            logged[t].append((round((kg or 0) * KG_TO_LB), int(reps or 0)))
        items = b.items or []
        picks = b.picks or {}
        titles: dict[int, tuple[str, str]] = {}
        for i, it in enumerate(items):
            t = picks.get(str(i)) or it.get("match")
            if t and t in logged:
                titles[i] = (t, "pick" if str(i) in picks else "match")
        claimed = {t for t, _ in titles.values()}
        free_items = [i for i in range(len(items)) if i not in titles]
        paired = pair_unclaimed([items[i] for i in free_items],
                                [t for t in logged if t not in claimed])
        for k, t in paired.items():
            i = free_items[k]
            titles[i] = (t, "log")
            await remember_alias(db, [items[i].get("board_text"), items[i].get("name")], t, "log")
        outcome = []
        for i, it in enumerate(items):
            t, src = titles.get(i, (None, None))
            rec = it.get("rec") or {}
            if t and src == "log":                      # target for what he actually did
                rec = {}
            res = verdict(rec, logged.get(t, [])) if t else {"verdict": "not_logged"}
            outcome.append({"idx": i, "board_text": it.get("board_text"), "title": t,
                            "title_source": src, "target_lb": rec.get("target_lb"),
                            "unit": rec.get("unit"), **res})
        b.outcome, b.checked_at = outcome, _dt.utcnow()
        done += 1
    await db.commit()
    return done


def summarize_outcome(outcome: list[dict]) -> dict:
    judged = [o for o in outcome if o.get("verdict") in ("beat", "hit", "under")]
    return {"judged": len(judged),
            "at_or_above": sum(o["verdict"] in ("beat", "hit") for o in judged),
            "beat": [o for o in judged if o["verdict"] == "beat"],
            "under": [o for o in judged if o["verdict"] == "under"]}
