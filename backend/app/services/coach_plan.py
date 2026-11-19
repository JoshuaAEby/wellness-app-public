"""Weekly-plan coach — the coach card on every day no training experiment is running.

Replaced `_training_rec` as the card's source on 2026-09-12 (note 29). The design and
the evidence behind each threshold are in docs/COACH.md; in short:

- The old rule forced EASY whenever any one of four signals crossed a soft threshold
  (52% of days), never read the readiness score, and the user went to a class on 52% of
  the days it said REST. It answered a question he is not actually asking.
- His real decision is WHICH ~3 of the 6 open days to take a class: median 3 classes a
  week, exactly 3 in 12 of 17 full weeks, never Sunday (gym closed).
- Observationally (2026-04-25..09-11) a class day was followed by BETTER next-morning
  energy at every readiness level, so the default is "go" and only real red flags stop
  it. Readiness itself did not predict next-morning energy, so it is not an input.

Classes are pre-set by the gym, so this never picks exercises. The only lifting it
controls is the HOME pull block — logged in Hevy only, title starting "HOME", so it
never counts as a class, never adds to ACWR, and never trips the training-streak stop.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.acwr import ACWR_HIGH, ACWR_VERY_HIGH
from app.models import DailyMetric, HevyWorkout, SleepSession, UserSettings
from app.timeutils import local_day_bounds_utc, to_local_date
from app.foodday import on_food_days_utc

logger = logging.getLogger(__name__)

WEEKLY_CLASS_TARGET = 3          # The user's baseline; a 4th is earned, never assumed
PULL_BLOCKS_PER_WEEK = 2
PULL_MIN_GAP_DAYS = 2            # ≥48 h between pull blocks
HOME_TITLE_PREFIX = "HOME"       # Hevy title prefix that marks a home pull block
BACK_GROUPS = ("lats", "upper_back", "traps")
BACK_SETS_TARGET = 10            # bottom of the 10–20 hard-set band (routers/hevy.py)
# The pull line used to fire EVERY day it was theoretically due — 14 days running in
# Sept 2026, while zero blocks were ever logged (note 44: "it just says class every
# day... I feel like it should surprise me sometimes"). It now speaks only on days the
# block actually fits, and after this many blockless days it asks once instead of nagging.
PULL_DORMANT_DAYS = 14
# Leg block (2026-11-01: build muscle, little kit at home). Class legs are mostly
# conditioning: 5.7 building sets a week for quads against 16.2 of all sets. Only what home
# allows (doorway bar, bands, a few dumbbells, one kettlebell), so single-leg work makes a
# light weight enough. One a week to begin with: no HOME block had ever been logged.
LEG_BLOCKS_PER_WEEK = 1
LEG_START = date(2026, 11, 1)
LEG_GROUPS = ("quadriceps", "glutes", "hamstrings")
LEG_SETS_TARGET = 30             # 10 a muscle, the bottom of the guideline
LEG_DORMANT_DAYS = 14
LEG_BLOCK = ("Bulgarian split squat, single-leg RDL and single-leg calf raise, 3 sets each "
             "with your heaviest dumbbell or the kettlebell")
FRESH_ACWR = 0.85                # below this, load is unusually low — a cheap day to push
SLEEP_DEBT_NUDGE_H = 1.0
STALE_RUN_DAYS = 28
_RUN_TYPES = ("Outdoor Run", "Indoor Run")

# Soft signals: move a class later in the week when there is room, never cancel it.
SOFT_HRV_Z = -1.5
SOFT_RHR_Z = 1.5
SHORT_SLEEP_H = 6.0

# ACWR only softens effort — the user: fine a bit above 1.5, soften when "super high".
# His last 150 days: median 1.18, 11% of days > 2.0, 5% > 2.67.
# The same edges label ACWR everywhere else (app/acwr.py, 2026-11-04).
ACWR_LIGHTER = ACWR_HIGH
ACWR_EASY_ONLY = ACWR_VERY_HIGH

# A 4th class is offered only when the week is done and every signal is clean.
FOURTH_MAX_DEBT_H = 1.0
FOURTH_MAX_ACWR = 2.0
FOURTH_MIN_HRV_DAYS = 4          # need this many of the last 7 HRV z-scores

_COLOR = {"CLASS": "#818cf8", "+ 4TH": "#818cf8", "TARGET MET": "#10b981",
          "REST": "#ef4444", "REST_SOFT": "#f59e0b"}


def _open_days(day: date, closed) -> list[date]:
    """`day` through Sunday of its Monday-start week, minus days the gym is closed."""
    return [day + timedelta(days=i) for i in range(7 - day.weekday())
            if (day + timedelta(days=i)).weekday() not in closed]


def is_home_block(title: Optional[str], legs: bool = False) -> bool:
    """"HOME – Legs" is the leg block; any other HOME… title is a pull block."""
    t = (title or "").strip().upper()
    return t.startswith(HOME_TITLE_PREFIX) and ("LEG" in t) == legs


async def home_block_days(db: AsyncSession, start: date, end: date,
                          legs: bool = False) -> list[date]:
    """Local dates in [start, end] with a Hevy workout titled HOME… (pull blocks, or the
    leg block with `legs`)."""
    lo, _ = local_day_bounds_utc(start)
    _, hi = local_day_bounds_utc(end)
    rows = (await db.execute(
        select(HevyWorkout.start_time, HevyWorkout.title)
        .where(HevyWorkout.start_time >= lo).where(HevyWorkout.start_time < hi)
    )).all()
    return sorted({to_local_date(r.start_time) for r in rows if is_home_block(r.title, legs)})


def leg_line(day: date, blocks: list, week_blocks: int, leg_sets: float, fits: bool,
             unwell: bool, done_today: bool) -> Optional[dict]:
    """The leg block's line on the coach card, or None. `blocks` = dates in the last
    LEG_DORMANT_DAYS. Same stance as the pull block (note 44): only on days it fits, and once
    ignored for two weeks it asks once a week instead of repeating."""
    count = f"{leg_sets:.0f} building sets for quads, glutes and hamstrings this week"
    if done_today:
        return {"due": False, "done": True,
                "detail": f"Leg block done — {count} (aim {LEG_SETS_TARGET}+)."}
    if week_blocks >= LEG_BLOCKS_PER_WEEK:
        return None
    if not blocks and (day - LEG_START).days >= LEG_DORMANT_DAYS:
        if day.weekday() != 0:
            return None
        return {"due": False, "done": False, "dormant": True, "detail": (
            f"No leg block in {LEG_DORMANT_DAYS} days. Keep it, change the moves, or drop it? "
            "Class gives your legs plenty of work but little loading.")}
    if unwell or not fits:
        return None
    return {"due": True, "done": False, "detail": (
        f"Leg block today, about 20 min: {LEG_BLOCK}. Title it HOME – Legs in Hevy. "
        f"You're at {count} (aim {LEG_SETS_TARGET}+).")}


def due_home_blocks(rec: Optional[dict]) -> list[str]:
    """Home blocks the card offers today, for the briefing. Without this the briefing read a
    bare REST and told him to rest while the card offered the leg block (2026-11-02)."""
    return [name for key, name in (("legs", "leg block"), ("pull", "pull block"))
            if ((rec or {}).get(key) or {}).get("due")]


# The alcohol line speaks only when this morning's HRV is outside its normal range.
ALCOHOL_LINE_MAX_HRV_Z = -1.0


# Findings the extra lines cite are read from the LIVE correlation cache (2026-10-28). They
# were hard-coded (ρ and n typed into the strings) and had
# drifted — Zone 2 → mood no longer survived at all — while the briefing repeats these lines
# verbatim. A finding that does not currently survive, or is CONFOUNDED, is not cited, and a
# line whose findings are all gone stays silent.
def _live_finding(cache: dict, a: str, b: str, lag: int) -> dict | None:
    r = cache.get((a, b, lag))
    if not r or not (r.get("significant") or r.get("suggestive")) or r.get("spearman_r") is None:
        return None
    if r.get("reverse_check") == "confounded":
        return None
    return r


def _cite(r: dict) -> str:
    tier = "confirmed" if r.get("significant") else "suggestive"
    return f"ρ {r['spearman_r']:+.2f}, n={r['n']}, {tier}"


async def _extra_line(db: AsyncSession, day: date, dm, label: str,
                      soft: list, hard) -> dict | None:
    """ONE thing worth knowing today that the call itself doesn't say (note 44).

    The user: the coach "just says CLASS every day... it should surprise me sometimes or give
    me a recommendation I wouldn't have thought of". Each candidate is drawn from HIS
    findings and states its own strength honestly — a confirmed pair and a suggestive one
    do not get the same voice. Priority order below, then ROTATED BY DATE so consecutive
    days never draw the same one; when nothing qualifies it stays silent, because filler
    is what made the card ignorable in the first place.
    """
    from app.models import HealthMetric, Workout
    from app.services.correlations import read_correlation_cache

    cands: list[dict] = []
    try:
        cache = {(r["metric_a"], r["metric_b"], r["lag_days"]): r
                 for r in await read_correlation_cache()}
    except Exception as exc:  # noqa: BLE001 — no cache → no correlation-based lines
        logger.warning("coach extra line: correlation cache unavailable: %s", exc)
        cache = {}

    # Yesterday's drinking explains this morning's numbers — both effects confirmed.
    # Yesterday's FOOD day (until 04:00 this morning), so a 00:30 drink counts.
    sdu = sum(v for (v,) in (await db.execute(
        select(HealthMetric.value).where(HealthMetric.metric_type == "alcohol")
        .where(on_food_days_utc(HealthMetric.timestamp, day - timedelta(days=1)))
    )).all() if v)
    # Only when it EXPLAINS THIS MORNING: drinks plus a genuinely LOW HRV. Sent on every
    # drinking day it was 6 of 9 lines in the 14-day replay — true each time, and exactly
    # the repetition note 44 is about. The 3-day backstop covers a drinking run.
    # 2026-10-28: was `hrv_z < 0`, and the line told him to "read that as the drinks" at
    # −0.2σ — an ordinary night given a cause. The daily briefing repeats the coach line
    # verbatim, so this was the source of its attribution; the prompt rule could not undo
    # it. Now only outside the normal range, and worded as consistency, not cause.
    hrv_z_today = dm.hrv_z_score if dm else None
    alc = [(txt, r) for txt, r in (
        ("HRV runs lower", _live_finding(cache, "alcohol_sdu", "hrv_z_score", 1)),
        ("respiratory rate runs higher", _live_finding(cache, "alcohol_sdu", "respiratory_rate", 1)),
    ) if r]
    if alc and sdu >= 1 and hrv_z_today is not None and hrv_z_today <= ALCOHOL_LINE_MAX_HRV_Z:
        cands.append({"key": "alcohol_yday", "every": 3, "detail": (
            f"{sdu:.1f} standard drink{'' if round(sdu, 1) == 1 else 's'} yesterday — the next "
            f"morning your " + " and ".join(f"{t} ({_cite(r)})" for t, r in alc)
            + f". Today's HRV is {hrv_z_today:+.1f}σ, consistent with the drinks rather than "
            f"with training.")})

    debt = dm.sleep_debt_hours if dm else None
    sleep_energy = _live_finding(cache, "sleep_total_h", "subj_energy", 0)
    if sleep_energy and debt is not None and debt >= SLEEP_DEBT_NUDGE_H:
        cands.append({"key": "sleep_debt", "detail": (
            f"Sleep debt is {debt:.1f} h. More sleep → better energy ({_cite(sleep_energy)}) "
            f"— an extra 45 minutes tonight does more for tomorrow than anything training can.")})

    acwr = dm.acwr if dm else None
    if acwr is not None and acwr < FRESH_ACWR and label in ("CLASS", "+ 4TH"):
        cands.append({"key": "fresh", "every": 2, "detail": (
            f"Load is well below your usual (ACWR {acwr:.2f}) — you are fresh. This is the "
            f"cheap week to take a 4th class, or to go hard in one you'd normally coast.")})

    # A closed gym (Sunday) is the best day for this, not a reason to stay quiet — only
    # illness, the early-warning and travel should silence it.
    z2 = [(txt, r) for txt, r in (
        ("mood", _live_finding(cache, "zone2_pct_7d", "subj_mood", 1)),
        ("sleep quality", _live_finding(cache, "zone2_pct_7d", "subj_sleep", 1)),
    ) if r]
    if z2 and label in ("REST", "TARGET MET") and hard in (None, "the gym is closed today"):
        cands.append({"key": "zone2", "every": 3, "detail": (
            "A 30-minute easy walk or jog would suit today: your Zone 2 share tracks next-day "
            + " and ".join(f"{t} ({_cite(r)})" for t, r in z2) + " — untested by experiment.")})

    last_run = (await db.execute(
        select(Workout.start_time).where(Workout.activity_type.in_(_RUN_TYPES))
        .order_by(Workout.start_time.desc()).limit(1)
    )).scalars().first()
    since_run = (day - to_local_date(last_run)).days if last_run else None
    if (since_run is None or since_run >= STALE_RUN_DAYS) and not hard and not soft:
        cands.append({"key": "run", "every": 14, "detail": (
            (f"You haven't run in {since_run} days. " if since_run else "No run on record. ")
            + "One easy 20 minutes would test the Zone 2 link your classes never touch — "
              "they are almost all lifting or conditioning.")})

    # A CHRONIC FACT MUST NOT FIRE DAILY. "You haven't run in 100 days" is true every day
    # for months; shown every day it is the pull-block nag again (first replay: 5 days
    # running). `every` puts such candidates on a fixed cadence — deterministic from the
    # date, so no state is needed and two devices agree. Event-driven ones (yesterday's
    # drinks, today's sleep debt) carry no `every`: they are news on the day they appear.
    cands = [c for c in cands
             if not c.get("every")
             or (day.toordinal() + (sum(map(ord, c["key"])) % c["every"])) % c["every"] == 0]
    if not cands:
        return None
    # Rotate by date: the ordinal moves one per day, so the pick moves with it and two
    # days in a row cannot draw the same candidate while the set is stable.
    return cands[day.toordinal() % len(cands)]


async def plan_for_day(db: AsyncSession, day: date, morning: bool = False) -> dict:
    """The coach card for `day`.

    morning=True ignores everything done ON `day` — the call as it stands at wake. The
    21:00 provisional push (for tomorrow) and the history replay both use it.
    """
    from app.services.coach import (
        MAX_CONSECUTIVE_TRAINING_DAYS, TRAINING_UNAVAILABLE_WEEKDAYS,
        _behavior_by_day, _consecutive_training_days,
    )
    from app.services.day_flags import flags_in_range
    from app.services.nutrition_goals import protein_pace, resolve_protein_goal
    from app.services.recovery_alarm import assess_recovery_alarm
    from app.services.strength import hard_sets_by_day

    closed = TRAINING_UNAVAILABLE_WEEKDAYS
    week_start = day - timedelta(days=day.weekday())
    week_days = [week_start + timedelta(days=i) for i in range(7)]
    beh = await _behavior_by_day(db, week_days)
    done_before = sum(1 for d in week_days if d < day and beh.get(d, {}).get("did_hiit"))
    today_beh = {} if morning else beh.get(day, {})
    class_today = bool(today_beh.get("did_hiit"))
    trained_today = bool(today_beh.get("trained"))

    target = WEEKLY_CLASS_TARGET
    open_days = _open_days(day, closed)
    days_left = len(open_days)
    slack = days_left - max(0, target - done_before)

    dm = (await db.execute(
        select(DailyMetric).where(DailyMetric.date == day)
    )).scalar_one_or_none()
    sleep = (await db.execute(
        select(SleepSession).where(SleepSession.date == day)
    )).scalar_one_or_none()
    flags = (await flags_in_range(db, day, day)).get(day, [])
    alarm = await assess_recovery_alarm(db, day)
    # The alarm falls back to the newest row on/before `day`; only today's counts.
    alarm_level = alarm.get("level") if alarm.get("date") == str(day) else None
    streak = await _consecutive_training_days(db, day)

    # ── Hard stops: REST, whatever the week needs ──────────────────────────────
    hard = None
    if day.weekday() in closed:
        hard = "the gym is closed today"
    elif "sick" in flags:
        hard = "you're flagged sick"
    elif "travel" in flags:
        hard = "you're traveling"
    elif alarm_level == "elevated":
        hard = "the illness early-warning is elevated"
    elif streak >= MAX_CONSECUTIVE_TRAINING_DAYS:
        hard = f"that would be {streak + 1} training days in a row"

    # ── Soft signals: named plainly, with the real number ─────────────────────
    soft: list[str] = []
    hrv_z = dm.hrv_z_score if dm else None
    rhr_z = dm.rhr_z_score if dm else None
    acwr = dm.acwr if dm else None
    if hrv_z is not None and hrv_z < SOFT_HRV_Z:
        soft.append(f"HRV {hrv_z:+.1f}σ")
    if rhr_z is not None and rhr_z > SOFT_RHR_Z:
        soft.append(f"resting HR {rhr_z:+.1f}σ")
    if sleep is not None and sleep.total_min and sleep.total_min / 60 < SHORT_SLEEP_H:
        soft.append(f"{sleep.total_min / 60:.1f}h sleep")

    hrv_week = [v for (v,) in (await db.execute(
        select(DailyMetric.hrv_z_score)
        .where(DailyMetric.date > day - timedelta(days=7)).where(DailyMetric.date <= day)
    )).all() if v is not None]
    hrv7 = sum(hrv_week) / len(hrv_week) if len(hrv_week) >= FOURTH_MIN_HRV_DAYS else None
    fourth = (done_before == target and not hard and not soft and dm is not None
              and hrv7 is not None and hrv7 >= 0
              and (dm.sleep_debt_hours is None or dm.sleep_debt_hours <= FOURTH_MAX_DEBT_H)
              and (acwr is None or acwr <= FOURTH_MAX_ACWR))

    # ── The call ──────────────────────────────────────────────────────────────
    if hard:
        label, color = "REST", _COLOR["REST"]
        detail = f"Rest — {hard}."
    elif done_before >= target:
        if fourth:
            label, color = "+ 4TH", _COLOR["+ 4TH"]
            detail = (f"{target} of {target} done and recovery is strong — a 4th class "
                      "is on offer if you want it.")
        else:
            label, color = "TARGET MET", _COLOR["TARGET MET"]
            detail = f"{done_before} of {target} done this week — rest is earned."
    elif soft and slack > 0:
        after = days_left - 1
        label, color = "REST", _COLOR["REST_SOFT"]
        detail = (f"Better another day — {', '.join(soft)}. {done_before} of {target} "
                  f"done, {after} open day{'' if after == 1 else 's'} left after today.")
    else:
        label, color = "CLASS", _COLOR["CLASS"]
        if slack < 0:
            detail = (f"{done_before} of {target} done — {target} is out of reach this "
                      "week, but every class still counts.")
        elif slack == 0:
            detail = (f"{done_before} of {target} done — you need every open day left "
                      f"to hit {target}.")
        else:
            detail = (f"{done_before} of {target} done, {days_left} open days left "
                      "including today.")
        if soft:
            detail += f" Keep it controlled — {', '.join(soft)}."
    if label in ("CLASS", "+ 4TH") and acwr is not None:
        if acwr > ACWR_EASY_ONLY:
            detail += f" Load is far above your usual (ACWR {acwr:.1f}) — easy effort only."
        elif acwr > ACWR_LIGHTER:
            detail += f" Load is high (ACWR {acwr:.1f}) — go lighter."

    acted = departed = False
    acted_detail = None
    if class_today and label != "REST":
        acted = True
        n = done_before + 1
        acted_detail = (f"Class done — {n} of {target} this week." if n <= target
                        else f"Class done — {n} this week, {n - target} over target.")
    elif trained_today and label == "REST":
        # Neutral, never a scold — same stance as coach._DEPARTED_DETAIL (note 21).
        departed = True
        acted_detail = "You trained today — noted. The week's count follows you."

    # ── Pull block (home, Hevy-only) ──────────────────────────────────────────
    blocks = await home_block_days(db, day - timedelta(days=13), day)
    past_blocks = [d for d in blocks if d < day]
    block_today = (not morning) and day in blocks
    week_blocks = sum(1 for d in past_blocks if d >= week_start) + int(block_today)
    sets = await hard_sets_by_day(db, week_start, day)
    back_sets = sum(v for d, groups in sets.items() if d < day or not morning
                    for g, v in groups.items() if g in BACK_GROUPS)
    gap_ok = not past_blocks or (day - past_blocks[-1]).days >= PULL_MIN_GAP_DAYS
    # WHEN THE BLOCK ACTUALLY FITS (note 44). Offering it on every class day too meant the
    # same sentence 14 days running. A home block belongs on a day the gym isn't taking:
    # a rest day, a Sunday, or after the class is already done.
    fits_today = (label in ("REST", "TARGET MET", "+ 4TH") or class_today
                  or day.weekday() in closed)
    pull = None
    if not blocks and not block_today:
        # Nothing in PULL_DORMANT_DAYS days: stop repeating, ask once (Mondays) instead.
        if day.weekday() == 0:
            pull = {"due": False, "done": False, "dormant": True, "detail": (
                f"No pull block logged in {PULL_DORMANT_DAYS} days. Keep it at "
                f"{PULL_BLOCKS_PER_WEEK}/week, drop to 1, or bin it? It is the only "
                f"lifting the coach controls — the classes decide the rest.")}
    elif block_today:
        pull = {"due": False, "done": True,
                "detail": f"Pull block done — {week_blocks} of {PULL_BLOCKS_PER_WEEK} this week."}
    elif ("sick" not in flags and alarm_level != "elevated" and gap_ok
          and fits_today and week_blocks < PULL_BLOCKS_PER_WEEK):
        count = f"{week_blocks} of {PULL_BLOCKS_PER_WEEK} blocks done"
        if back_sets < BACK_SETS_TARGET:
            detail_pull = (f"Pull block today — back is at {back_sets:.0f} hard sets this "
                           f"week (aim {BACK_SETS_TARGET}+); {count}.")
        else:
            # Class already covered the back this week (replay 2026-09-12: 18 sets by
            # Saturday). The block is still worth doing — it carries the anchor lifts —
            # but "aim 10+" beside 18 read as a contradiction.
            detail_pull = (f"Pull block today — class already gave your back "
                           f"{back_sets:.0f} hard sets this week, so keep it to your "
                           f"anchor moves; {count}.")
        pull = {"due": True, "done": False, "detail": detail_pull}
    if pull is not None:
        pull.update(blocks_this_week=week_blocks, target=PULL_BLOCKS_PER_WEEK,
                    back_sets_week=round(back_sets, 1))

    # ── Leg block (home, Hevy-only) ───────────────────────────
    leg_blocks = await home_block_days(db, day - timedelta(days=LEG_DORMANT_DAYS - 1), day, legs=True)
    leg_today = (not morning) and day in leg_blocks
    leg_week = sum(1 for d in leg_blocks if week_start <= d < day) + int(leg_today)
    building = await hard_sets_by_day(db, week_start, day, building_only=True)
    leg_sets = sum(v for d, groups in building.items() if d < day or not morning
                   for g, v in groups.items() if g in LEG_GROUPS)
    # A rest for sickness, travel (no kit), an alarm or a training streak is not a day for it.
    legs = leg_line(day, leg_blocks, leg_week, leg_sets, fits_today,
                    hard not in (None, "the gym is closed today"), leg_today)

    # ── Protein — only when meaningfully behind the usual pace ────────────────
    protein = None
    if not morning:
        pace = await protein_pace(db, day)
        if pace and pace.get("behind"):
            us = (await db.execute(select(UserSettings).limit(1))).scalars().first()
            goal = await resolve_protein_goal(db, us)
            protein = {"today_g": pace["today_g"], "goal_g": goal, "delta_g": pace["delta_g"],
                       "detail": (f"Protein {pace['today_g']} of {goal} g — "
                                  f"{-pace['delta_g']} g behind your usual by {pace['stage']}.")}

    extra = await _extra_line(db, day, dm, label, soft, hard)

    return {
        "label": label, "detail": detail, "color": color, "extra": extra,
        "acted": acted, "acted_detail": acted_detail, "departed": departed,
        "week": {"done": done_before + int(class_today), "target": target,
                 "open_days": days_left},
        "pull": pull,
        "legs": legs,
        "protein": protein,
        "signals": {"hard": hard, "soft": soft, "acwr": acwr, "hrv7": hrv7,
                    "fourth_offered": fourth},
    }
