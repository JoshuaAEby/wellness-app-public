"""Randomized n-of-1 coach: assignment + intent-to-treat analysis.

Pre-registered design (2026-07-11):

ELIGIBILITY (a day is randomized only if ALL hold — the rule is fixed in advance
so the estimand is "the effect on days where the choice is genuinely open"):
  - today's daily_metrics row exists (the 08:00 recompute ran)
  - the rule-based training rec is NOT "REST" (red-flag days are never randomized)
  - the day is NOT "strongly ready" — every signal favourable (added 2026-08-30,
    see _strongly_ready); the mirror image of the REST rule at the top end
  - not sick (today's check-in if present, else yesterday's flag carries over)
  - fewer than MAX_CONSECUTIVE_TRAINING_DAYS training days immediately preceding

RANDOMIZATION: permuted blocks of 4 (2 per arm) over the assignment *sequence*,
each block's permutation seeded deterministically by (experiment_id, block index)
— stateless, balanced, and reproducible after the fact.

ANALYSIS: intent-to-treat is primary — outcome grouped by ASSIGNMENT, which the
randomization keeps unconfounded no matter how compliance goes. Compliance is
auto-detected from workout records (never self-reported); per-protocol contrast
is reported as a secondary, explicitly labeled non-randomized view. Significance
via permutation test on the assignment labels (the exact test the design earns).
Outcomes are pre-registered: primary next-morning energy; secondaries same-day
day_score, next-morning mood, next-morning HRV z.

You can always decline the coach — that is expected, recorded, and handled by
ITT. What breaks the experiment is systematic noncompliance (ITT attenuates
toward zero), which the compliance rates on the status card make visible.
"""
import logging
import math
import random
from datetime import date, datetime, timedelta

import numpy as np
from scipy import stats as _st
from scipy.integrate import quad
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DailyMetric, Experiment, ExperimentAssignment, HealthMetric, SubjectiveLog, Workout,
)
from app.timeutils import local_date_sql, local_today
from app.workout_categories import LOW_INTENSITY_TYPES, categorize_activity

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_TRAINING_DAYS = 3
BLOCK_SIZE = 4                       # permuted [a, a, b, b]
# Default accrual target for a NEW experiment. Per-trial targets live on
# Experiment.target_per_arm — read that, not this, when reporting progress.
# Note on size: 30/arm gives 0.48 power at d=0.5 (two-sided, alpha=0.05), NOT the 0.80
# an earlier comment here claimed; 80% at d=0.5 needs 64/arm. Left at 30 deliberately —
# changing a running trial's target is a protocol amendment, not a code tidy-up.
TARGET_N_PER_ARM = 30
ANALYSIS_MIN_N_PER_ARM = 5           # below this, report progress only
N_PERMUTATIONS = 5000

# Bayes factor prior width. The JZS default (Rouder et al. 2009) — a Cauchy on the
# standardized effect with scale r, which says "an effect, if present, is as likely to
# be small as large" without committing to a direction or magnitude. 0.707 is the
# conventional default; it is a real modelling choice and is reported in the payload
# so a reader can see what prior produced the number.
BF_PRIOR_R = 0.707
BF_POSTERIOR_DRAWS = 20000           # for the posterior on d; seeded, so deterministic

# Pre-registered outcomes: key → (label, source, day offset from assignment day)
OUTCOMES = {
    "subj_energy": ("Energy (next morning)",      "energy",     1),
    "subj_day":    ("Day quality (same evening)", "day_score",  0),
    "subj_mood":   ("Mood (next morning)",        "mood",       1),
    "hrv_z":       ("HRV z (next morning)",       "hrv_z",      1),
    # Added 2026-10-04 for the after-dinner experiment. Offset 1 = the night that ENDS
    # the next morning (sleep_hr_elevation is stamped at wake).
    "sleep_hr_elev": ("Early-night HR rise (that night)", "sleep_hr_elevation", 1),
    "subj_sleep":    ("Sleep quality (next morning)",     "sleep_quality",      1),
}

# Which outcomes each experiment KIND reports. Outcomes are part of a pre-registration:
# adding the two new ones to every experiment would retro-fit them onto L-theanine, a
# trial already running — measuring it on outcomes chosen after it started. So kinds that
# predate them keep exactly their original four.
OUTCOMES_BY_KIND = {
    "nutrition": ["sleep_hr_elev", "hrv_z", "subj_sleep", "subj_energy"],
}
DEFAULT_OUTCOMES = ["subj_energy", "subj_day", "subj_mood", "hrv_z"]

# What the app SAYS for each non-training experiment (Today card + Record row). Kept on
# the server so a new experiment needs no frontend change. `show_from_hour`: when the
# card appears — the L-theanine dose is at bedtime (evening card, as before); the
# after-dinner rule must be known in the morning to be planned around.
# `auto_compliance`: followed-or-not is read from the food log, nothing to tap.
EXPERIMENT_COPY = {
    "theanine": {
        "row_label": "L", "show_from_hour": 19, "auto_compliance": False,
        "a_title": "Tonight: take theanine", "b_title": "Tonight: none",
        "a_hint": "Take it right before sleep. The timing matters more than the dose — take it "
                  "in the morning and it's cleared long before it could do anything.",
        "b_hint": "No dose tonight. Log it anyway so the off-nights are recorded.",
    },
    "after_dinner": {
        "row_label": "D", "show_from_hour": 0, "auto_compliance": True,
        "a_title": "Tonight: have something after dinner",
        "b_title": "Tonight: kitchen closes after dinner",
        "a_hint": "A normal-sized dessert or snack after dinner — your usual kind, roughly "
                  "200–400 kcal. Log it as Dessert or Snack, timed after dinner.",
        "b_hint": "Nothing to eat after dinner. Zero-calorie drinks are fine; alcohol is up to you.",
    },
}


def experiment_copy(exp) -> dict | None:
    """Display copy for a non-training experiment, keyed by its treatment arm."""
    return EXPERIMENT_COPY.get(exp.arm_a)


async def get_active_experiments(db: AsyncSession) -> list[Experiment]:
    """Every running experiment. Concurrent experiments are independently randomized,
    which makes them orthogonal by construction — a 2x2 factorial rather than a
    confound. Each main effect is estimated at nearly the precision it would have had
    running alone, on the same days, so a second experiment is close to free power.
    (The interaction is NOT well powered at these n; only main effects are claimed.)"""
    return list((await db.execute(
        select(Experiment).where(Experiment.ended_at.is_(None))
        .order_by(Experiment.id)
    )).scalars().all())


async def get_active_experiment(db: AsyncSession) -> Experiment | None:
    """The active TRAINING experiment — the one whose arm becomes the coach's daily
    recommendation. Deliberately not "any active experiment": arms are mapped through
    _ARM_TO_LABEL into training labels, so a supplement experiment surfacing here would
    render its arm as a workout. Only one training experiment may run at a time."""
    return (await db.execute(
        select(Experiment).where(Experiment.ended_at.is_(None))
        .where(Experiment.kind == "training")
        .order_by(Experiment.id.desc()).limit(1)
    )).scalar_one_or_none()


async def _consecutive_training_days(db: AsyncSession, before: date) -> int:
    """Training (non-walk) days in an unbroken run immediately before `before`."""
    wo_day = local_date_sql(Workout.start_time)
    rows = (await db.execute(
        select(wo_day.label("d")).distinct()
        .where(Workout.activity_type.notin_(LOW_INTENSITY_TYPES))
        .where(wo_day >= str(before - timedelta(days=MAX_CONSECUTIVE_TRAINING_DAYS)))
        .where(wo_day < str(before))
    )).all()
    days = {date.fromisoformat(str(r.d)) for r in rows}
    streak = 0
    d = before - timedelta(days=1)
    while d in days:
        streak += 1
        d -= timedelta(days=1)
    return streak


# Weekdays when a training arm cannot be delivered (Mon=0 … Sun=6).
TRAINING_UNAVAILABLE_WEEKDAYS = {6}

# ── "Strongly ready" guard (added 2026-08-30) ────────────────────────────────
# The REST guard below has always been one-sided: a day the metrics call REST is
# never randomized, but a day every metric called excellent still drew REST half
# the time. This closes that asymmetry at the top end.
#
# CONJUNCTIVE by design — *every* signal must be favourable, and any missing one
# disqualifies. "Everything looks ready" is a claim you cannot make from partial
# data, so unlike the REST rule (any one red flag is enough to stop) this needs
# the full set. 34 of 40 experiment days carry all four.
#
# The ACWR band is two-sided for a structural reason: _training_rec sends
# acwr < 0.8 to MODERATE before it ever reaches its HARD branch, so admitting
# low-ACWR days here would make an ineligible day fall through to MODERATE and
# silently contradict the guard's own intent. With 0.8 <= acwr <= 1.3 every
# strongly-ready day resolves to HARD — verified across all history, 0 mismatches.
#
# Costs 3 of 40 days (7.5%) against a target the trial is already short of. Worth
# it: two of those three are 2026-08-18 and 2026-08-29, the days that prompted the
# complaint, and both had drawn REST.
STRONG_HRV_Z   = 1.0
STRONG_RHR_Z   = 0.5     # RHR at/below baseline+0.5σ (higher = worse)
STRONG_DEBT_H  = 1.0
STRONG_ACWR    = (0.8, 1.3)


def _strongly_ready(dm: DailyMetric | None) -> bool:
    """True when every recovery signal is present AND favourable. See above."""
    if dm is None:
        return False
    hrv, rhr = dm.hrv_z_score, dm.rhr_z_score
    debt, acwr = dm.sleep_debt_hours, dm.acwr
    if any(v is None for v in (hrv, rhr, debt, acwr)):
        return False
    return (hrv >= STRONG_HRV_Z and rhr <= STRONG_RHR_Z and debt <= STRONG_DEBT_H
            and STRONG_ACWR[0] <= acwr <= STRONG_ACWR[1])


async def check_eligibility(
    db: AsyncSession, day: date, kind: str = "training",
) -> tuple[bool, str, DailyMetric | None]:
    """(eligible, reason, daily_metrics_row). Reason describes the first failed rule.

    Rules depend on the experiment KIND. The training rules exist because assigning
    HIIT on a day the metrics say REST is both unsafe and unrepresentative — but they
    are meaningless for an intervention you simply take, and applying them to one would
    discard roughly half the days for no reason. Exclusions that reflect a genuinely
    unrepresentative DAY (sickness, travel, missing data) apply to every kind.
    """
    dm = (await db.execute(
        select(DailyMetric).where(DailyMetric.date == day)
    )).scalar_one_or_none()
    training = kind == "training"

    # Training arms are chosen from the day's recovery state, so that state must exist.
    # A supplement arm doesn't depend on it — dm is only a provenance snapshot there.
    if dm is None and training:
        return False, "no daily metrics yet (recompute hasn't run)", None

    # Days the training intervention CANNOT be delivered. Sunday: the gym is closed, so
    # a HIIT assignment is a guaranteed non-compliance — verified, 0 of 3 Sunday HIIT
    # draws produced a session (two nothing, one 21-minute walk at zero high-intensity
    # minutes).
    #
    # This is safe because eligibility is evaluated INDEPENDENTLY OF THE ARM, so it
    # removes Sundays from both arms symmetrically. Excluding only the HIIT Sundays
    # would be exactly the bias this avoids — dropping days because the treatment
    # failed.
    #
    # It does narrow the estimand: the trial now answers "HIIT vs rest on days training
    # is possible" rather than "on any day". That is the more useful question and the
    # only one the protocol can actually deliver.
    if training and day.weekday() in TRAINING_UNAVAILABLE_WEEKDAYS:
        return False, "training unavailable this weekday (gym closed)", dm

    if training:
        from app.services.training_rec import _training_rec
        rec = _training_rec(dm)
        if rec is not None and rec["label"] == "REST":
            return False, f"training rec is REST ({rec['rationale']})", dm
        # Symmetric top-end guard: days every signal calls excellent are not the
        # days the choice is open, and assigning REST on one is where compliance
        # actually broke. Arm-independent like every other rule here, so it
        # removes such days from BOTH arms and cannot bias the contrast.
        if _strongly_ready(dm):
            return False, "all recovery signals strongly favourable", dm

    subj_today = (await db.execute(
        select(SubjectiveLog).where(SubjectiveLog.date == day)
    )).scalar_one_or_none()
    subj_yday = (await db.execute(
        select(SubjectiveLog).where(SubjectiveLog.date == day - timedelta(days=1))
    )).scalar_one_or_none()
    sick = (subj_today.sick if subj_today is not None and subj_today.sick is not None
            else (subj_yday.sick if subj_yday is not None else None))
    if sick:
        return False, "sick day flagged", dm

    # Nonstandard-day flags (travel, illness, poor environment, incomplete data…)
    # exclude a day from the experiment just as sickness does — the outcome on
    # such days is confounded and unrepresentative. `day` here is the assignment
    # day; a flag on it means don't count it in the ITT analysis.
    from app.services.day_flags import day_exclusion_reason
    reason = await day_exclusion_reason(db, day)
    if reason is not None:
        return False, f"day flagged: {reason}", dm

    if training:
        streak = await _consecutive_training_days(db, day)
        if streak >= MAX_CONSECUTIVE_TRAINING_DAYS:
            return False, f"{streak} consecutive training days", dm

    return True, "eligible", dm


def _block_arm(experiment_id: int, n_prior_assignments: int) -> str:
    """Permuted-block arm ('a'/'b') for the next assignment — deterministic,
    stateless: each block of BLOCK_SIZE is a seeded permutation of [a,a,b,b]."""
    block_idx, pos = divmod(n_prior_assignments, BLOCK_SIZE)
    block = ["a"] * (BLOCK_SIZE // 2) + ["b"] * (BLOCK_SIZE // 2)
    random.Random(f"coach:{experiment_id}:{block_idx}").shuffle(block)
    return block[pos]


async def _get_assignment(db: AsyncSession, exp_id: int, day: date) -> ExperimentAssignment | None:
    return (await db.execute(
        select(ExperimentAssignment)
        .where(ExperimentAssignment.experiment_id == exp_id)
        .where(ExperimentAssignment.date == day)
    )).scalar_one_or_none()


async def draw_for_experiment(db: AsyncSession, exp: Experiment, day: date) -> dict:
    """Draw one experiment's arm for `day` WITHOUT checking eligibility (the draw is
    independent of eligibility by design, so drawing early — even a week early — is
    statistically clean; the morning pipeline confirms eligibility). Idempotent.

    Drawing ahead is what makes a physically-prepared intervention workable: a week of
    arms can be revealed at once so a pill organiser can be loaded to match, and because
    the draw is recorded BEFORE any outcome it stays a genuine randomisation rather than
    a recollection.
    """
    existing = await _get_assignment(db, exp.id, day)
    if existing is not None:
        return {"status": "already_drawn", "arm": existing.arm, "date": day.isoformat(),
                "experiment": exp.label, "experiment_id": exp.id}

    # A day already marked AWAY (a pre-planned trip) can never be eligible, so don't
    # spend a draw on it: each draw consumes a slot in the permuted block, and a block
    # emptied by ineligible days drifts out of balance. Decided before — and
    # independently of — the arm, so it cannot bias the contrast (2026-10-19).
    from app.services.day_flags import is_away
    if await is_away(db, day):
        return {"status": "skipped_away", "arm": None, "date": day.isoformat(),
                "experiment": exp.label, "experiment_id": exp.id}

    n_prior = (await db.execute(
        select(func.count(ExperimentAssignment.id))
        .where(ExperimentAssignment.experiment_id == exp.id)
    )).scalar() or 0
    arm = exp.arm_a if _block_arm(exp.id, n_prior) == "a" else exp.arm_b

    db.add(ExperimentAssignment(
        experiment_id=exp.id, date=day, arm=arm, eligible_confirmed=None,
    ))
    await db.commit()
    logger.info("Coach: [%s] drew %s for %s (draw #%d, unconfirmed)",
                exp.label, arm, day, n_prior + 1)
    return {"status": "drawn", "arm": arm, "date": day.isoformat(),
            "experiment": exp.label, "experiment_id": exp.id}


async def draw_for_date(db: AsyncSession, day: date) -> list[dict]:
    """Draw `day` for EVERY running experiment. One result per experiment."""
    exps = await get_active_experiments(db)
    if not exps:
        return [{"status": "no_active_experiment"}]
    return [await draw_for_experiment(db, e, day) for e in exps]


async def _confirm_one(db: AsyncSession, exp: Experiment, today: date) -> dict:
    assignment = await _get_assignment(db, exp.id, today)
    if assignment is None:
        res = await draw_for_experiment(db, exp, today)
        if res["status"] != "drawn":
            return res
        assignment = await _get_assignment(db, exp.id, today)

    if assignment.eligible_confirmed is not None:
        return {"status": "already_confirmed", "arm": assignment.arm,
                "eligible": assignment.eligible_confirmed,
                "reason": assignment.ineligible_reason,
                "experiment": exp.label, "experiment_id": exp.id}

    # Eligibility rules differ by kind — see check_eligibility.
    eligible, reason, dm = await check_eligibility(db, today, exp.kind)
    assignment.eligible_confirmed = eligible
    assignment.ineligible_reason = None if eligible else reason
    assignment.readiness_at_assignment = dm.readiness_score if dm else None
    assignment.hrv_z_at_assignment = dm.hrv_z_score if dm else None
    await db.commit()
    logger.info("Coach: [%s] %s confirmed %s (%s)", exp.label, today,
                "ELIGIBLE" if eligible else "ineligible", reason)
    return {"status": "confirmed", "arm": assignment.arm, "eligible": eligible,
            "reason": None if eligible else reason,
            "experiment": exp.label, "experiment_id": exp.id}


async def confirm_today(db: AsyncSession) -> list[dict]:
    """Morning step for EVERY running experiment: ensure today has a draw, then
    confirm/deny eligibility and snapshot the recovery state. Idempotent — re-running
    never flips a verdict."""
    exps = await get_active_experiments(db)
    if not exps:
        return [{"status": "no_active_experiment"}]
    today = local_today()
    return [await _confirm_one(db, e, today) for e in exps]


# ── Blinded always-on recommendation ─────────────────────────────────────────
# The coach card shows the SAME vocabulary and tone every day, whether the label
# came from the random draw (eligible experiment day) or from the rule-based rec
# (all other days) — so compliance doesn't differ by mechanism. Deliberately: no
# mechanism reveal, and no number-citing rationale (metric-derived numbers would
# unblind by their presence/absence; the numbers live in the readiness card).
#
# HARD and REST can come from EITHER source (the experiment draw only ever
# produces these two), so their text must stay static/identical regardless of
# source — that's the blinding guarantee. EASY and MODERATE, by contrast, are
# ALWAYS rule-based (the experiment never draws them) — so varying their wording
# carries no blinding risk, and that's where the more versatile modality
# suggestions (Zone 2, long walk, etc.) requested 2026-07-15 live.
_ARM_TO_LABEL = {"hiit": "HARD", "rest": "REST"}
_COACH_DETAIL = {
    "REST": "Full rest today — let the body absorb the work.",
    "HARD": "Go hard today — a HIIT session fits well.",
}
# Rule-based-only labels: a few concrete, versatile phrasings each, rotated
# deterministically by date (stable within a day, varied across days).
_EASY_VARIANTS = [
    "Easy movement only — a Zone 2 jog or an easy long walk.",
    "Take a long walk today — keep the effort light.",
    "Mobility work or a gentle Zone 2 spin fits well.",
]
_MODERATE_VARIANTS = [
    "A steady Zone 2 run is a good call today.",
    "A moderate session — strength or steady-state cardio, your pick.",
    "A brisk long walk or a moderate lift both fit well today.",
]


def _rotate(day: date, variants: list[str]) -> str:
    """Deterministic pick by calendar date — stable for the day, varies day to day."""
    idx = int(day.strftime("%Y%m%d")) % len(variants)
    return variants[idx]
_COACH_COLOR = {"REST": "#ef4444", "EASY": "#f59e0b", "MODERATE": "#10b981", "HARD": "#818cf8"}


# Friendly acknowledgment shown once the day's recommended kind of training is
# done (loose detection — see _coach_acted). Blinded-safe: about compliance, not
# mechanism.
_ACTED_DETAIL = {
    "HARD":     "Nice work — you got your hard session in. 💪",
    "MODERATE": "Nice — steady session done. 👍",
    "EASY":     "Nice — you moved today. 🙂",
}

# Shown when the day's training DEPARTED from the call — currently only "REST was
# recommended and a real session happened" (note 21).
#
# TONE IS LOAD-BearING, do not make this a scold. The experiment is intention-to-treat:
# `_complied` records the departure and the analysis is unbiased regardless, as the
# module docstring says explicitly. It is his body and his call, and a card that tuts at
# him is a card he stops reading. Neutral acknowledgment only — the point is that the
# card stops telling him to rest on an evening when he has already trained.
_DEPARTED_DETAIL = {
    "REST": "You trained today — noted. Rest was the call, but the plan follows you.",
}


async def _coach_acted(
    db: AsyncSession, day: date, label: str,
) -> tuple[bool, str | None, bool]:
    """Has the day's training resolved? Returns (acted, detail, departed).

    `acted` — the recommended KIND of training happened. Flips the card to a 'done'
    acknowledgment and suppresses the (now-moot) strength hint. Deliberately looser than
    the experiment compliance ledger, which keeps its own stricter, separate accounting.

    `departed` — something happened that the call did not ask for. Added 2026-09-07
    (note 21): REST used to return early here on the reasoning that "REST isn't an
    action, so it never acts", which is true but left the card repeating "rest today"
    all evening to someone who had already trained. **The needed logic already existed**
    — `_complied()` below encodes exactly this rule for the experiment's compliance card
    (`arm == "rest" -> not behavior["trained"]`); the coach card simply never asked.

    REST + NO training is deliberately NOT acknowledged. Resting is not observable until
    the day is over, and "rest day taken ✓" at 09:00 asserts something that is not yet
    true — the same reason an unlogged habit is only an implied miss on a PAST day.
    """
    beh = (await _behavior_by_day(db, [day])).get(day, {})

    if label == "REST":
        if beh.get("trained", False):
            return False, _DEPARTED_DETAIL["REST"], True
        return False, None, False

    if label not in ("EASY", "MODERATE", "HARD"):
        return False, None, False
    did = beh.get("moved", False) if label == "EASY" else beh.get("trained", False)
    if not did:
        return False, None, False
    return True, _ACTED_DETAIL.get(label, "Done ✓"), False


async def get_coach_recommendation(db: AsyncSession, day: date) -> dict | None:
    """Unified daily recommendation. Source (randomized vs metric-based) is
    intentionally NOT included — query the experiments API to unblind on purpose.
    Carries `acted`/`acted_detail`: once today's recommended training is done the
    card acknowledges it instead of re-prompting."""
    label = detail = color = None
    exp = await get_active_experiment(db)
    if exp is not None:
        assignment = await _get_assignment(db, exp.id, day)
        if assignment is not None and assignment.eligible_confirmed:
            label = _ARM_TO_LABEL.get(assignment.arm, assignment.arm.upper())
            detail = _COACH_DETAIL.get(label, "")
            color = _COACH_COLOR.get(label, "#94a3b8")

    if label is None:
        # No training experiment today: the weekly-plan coach (note 29, 2026-09-12;
        # docs/COACH.md). It returns the whole card, acted/departed included.
        # `_training_rec` is NOT retired — experiment eligibility still reads it — and
        # the EASY/MODERATE variants above are kept for any future caller.
        from app.services.coach_plan import plan_for_day
        return await plan_for_day(db, day)

    acted, acted_detail, departed = await _coach_acted(db, day, label)
    return {"label": label, "detail": detail, "color": color,
            "acted": acted, "acted_detail": acted_detail,
            # Distinct from `acted` on purpose: the card must not render a green
            # "✓ DONE" for a day that went the other way.
            "departed": departed}


async def _behavior_by_day(db: AsyncSession, days: list[date]) -> dict[date, dict]:
    """{day: {did_hiit, trained, moved}} auto-detected from workout records.

    `did_hiit` = a GYM CLASS happened (hiit ∪ strength labels) — deliberately
    label-independent, see below. `trained` = a non-walk workout; `moved` = any workout
    (incl. a walk)."""
    if not days:
        return {}
    wo_day = local_date_sql(Workout.start_time)
    rows = (await db.execute(
        select(wo_day.label("d"), Workout.activity_type)
        .where(wo_day >= str(min(days)))
        .where(wo_day <= str(max(days)))
    )).all()
    out: dict[date, dict] = {d: {"did_hiit": False, "trained": False, "moved": False} for d in days}
    for r in rows:
        d = date.fromisoformat(str(r.d))
        if d not in out:
            continue
        out[d]["moved"] = True
        cat = categorize_activity(r.activity_type)
        # GYM CLASS, not the Apple label (fixed 2026-09-09). This gated on cat == "hiit",
        # which worked only because the user labelled every class "High Intensity Interval
        # Training". The moment he logs a strength session as "Traditional Strength
        # Training" — which he already did until 2026-04, and intends to resume — that
        # day would read as NON-COMPLIANCE with a hiit assignment he actually honoured,
        # silently, in a trial that is still running.
        #
        # The arm is literally named "Gym class vs rest", so the union is also what the
        # experiment always meant. `did_hiit` keeps its key name because `_complied`
        # and the compliance card read it.
        if cat in ("hiit", "strength"):
            out[d]["did_hiit"] = True
        if (r.activity_type or "") not in LOW_INTENSITY_TYPES:
            out[d]["trained"] = True
    return out


def _complied(arm: str, behavior: dict) -> bool | None:
    """Did the actual behavior match the assignment? None = arm not auto-detectable."""
    if arm == "hiit":
        return behavior["did_hiit"]
    if arm == "rest":
        return not behavior["trained"]
    return None


def _perm_test(a_vals: list[float], b_vals: list[float]) -> dict | None:
    """Two-sided permutation test on the difference of means (ITT's exact test)."""
    if len(a_vals) < ANALYSIS_MIN_N_PER_ARM or len(b_vals) < ANALYSIS_MIN_N_PER_ARM:
        return None
    a, b = np.asarray(a_vals, float), np.asarray(b_vals, float)
    obs = float(a.mean() - b.mean())
    pooled = np.concatenate([a, b])
    na = len(a)
    rng = np.random.default_rng(11)
    count = 0
    for _ in range(N_PERMUTATIONS):
        p = rng.permutation(pooled)
        if abs(float(p[:na].mean() - p[na:].mean())) >= abs(obs):
            count += 1
    return {
        "diff": round(obs, 3),
        "mean_a": round(float(a.mean()), 3),
        "mean_b": round(float(b.mean()), 3),
        "n_a": len(a), "n_b": len(b),
        "p": round((count + 1) / (N_PERMUTATIONS + 1), 4),
    }


def _bf10_jzs(t: float, n_a: int, n_b: int, r: float = BF_PRIOR_R) -> float:
    """JZS Bayes factor (BF10) for an independent-samples t — Rouder et al. 2009.

    This is the closed form `pingouin.bayesfactor_ttest` evaluates. It is written out
    here rather than taking the dependency: pingouin pulls pandas, statsmodels,
    matplotlib, seaborn and scikit-learn to supply this one integral, on a 2-core box
    whose boot SSD has already run out of room once.

    Note what a BF from a t-statistic is and is not. It is a genuine Bayes factor, but
    it is computed from the t, so it inherits the t-test's assumptions (normality,
    equal variances) — it does not launder them away. It is reported ALONGSIDE the
    permutation test, which assumes neither, and alongside the posterior below, which
    is what actually answers "how big is it".
    """
    n_eff = n_a * n_b / (n_a + n_b)          # effective n for the two-sample case
    df = n_a + n_b - 2

    def integrand(g):
        return ((1 + n_eff * g * r ** 2) ** -0.5
                * (1 + t ** 2 / ((1 + n_eff * g * r ** 2) * df)) ** (-(df + 1) / 2)
                * (2 * math.pi) ** -0.5 * g ** -1.5 * math.exp(-1 / (2 * g)))

    numerator = quad(integrand, 0, np.inf)[0]
    denominator = (1 + t ** 2 / df) ** (-(df + 1) / 2)
    return float(numerator / denominator)


def _bf_label(bf10: float) -> str:
    """Conventional reading of a Bayes factor. The bands are a convention, not a law —
    and unlike alpha they are not a decision procedure, which is the point."""
    if bf10 > 3:
        return "evidence for effect"
    if bf10 < 1 / 3:
        return "evidence for null"
    return "inconclusive"


def _bayes_estimate(a_vals: list[float], b_vals: list[float]) -> dict | None:
    """Posterior for the treatment effect, plus a JZS Bayes factor.

    WHY THIS EXISTS ALONGSIDE `_perm_test`. The permutation test answers "could label
    shuffling produce a gap this big" — the exact question the randomization earns, and
    it stays primary. What it cannot say is how large the effect is or how sure we are,
    and for an n-of-1 trial that is the decision-relevant quantity: "should I keep going
    to the gym" is a question about magnitude, not about the existence of a difference.
    A Bayes factor alone does not fix that either — it is a model-comparison scalar and
    discards the magnitude just as thoroughly. Hence a posterior.

    THE MODEL. Standard reference prior p(mu_a, mu_b, sigma) proportional to 1/sigma
    with a common variance. Under it the posterior for the mean difference is exactly
    a shifted, scaled Student t on n_a + n_b - 2 degrees of freedom, so the interval on
    the raw scale is analytic and noise-free. Be honest about what this buys: those
    limits coincide numerically with the frequentist CI. The gain is that under this
    model the statement "there is a 95% probability the effect lies in this interval"
    is licensed, and P(effect > 0) is available — neither of which a CI supports.

    The standardized effect d = delta / sigma needs sigma's own uncertainty carried
    through, so it is drawn from the joint posterior rather than computed as a point
    estimate divided by s_p. Seeded, hence reproducible run to run.
    """
    if len(a_vals) < ANALYSIS_MIN_N_PER_ARM or len(b_vals) < ANALYSIS_MIN_N_PER_ARM:
        return None
    a, b = np.asarray(a_vals, float), np.asarray(b_vals, float)
    n_a, n_b = len(a), len(b)
    df = n_a + n_b - 2

    ss = float(((a - a.mean()) ** 2).sum() + ((b - b.mean()) ** 2).sum())
    s_pooled = math.sqrt(ss / df)
    if s_pooled == 0:                        # both arms perfectly constant
        return None
    se = s_pooled * math.sqrt(1 / n_a + 1 / n_b)
    diff = float(a.mean() - b.mean())
    t_stat = diff / se

    # Raw scale — analytic, from the Student-t posterior for the mean difference.
    lo, hi = _st.t.interval(0.95, df, loc=diff, scale=se)
    # P(effect > 0). Direction is meaningful even when the magnitude is uncertain.
    p_positive = float(_st.t.sf(0.0, df, loc=diff, scale=se))

    # Standardized scale — Monte Carlo over the joint posterior, so sigma's
    # uncertainty widens d's interval the way it should.
    rng = np.random.default_rng(11)
    sigma2 = ss / rng.chisquare(df, BF_POSTERIOR_DRAWS)
    delta = rng.normal(diff, np.sqrt(sigma2 * (1 / n_a + 1 / n_b)))
    d_draws = delta / np.sqrt(sigma2)
    d_lo, d_hi = np.percentile(d_draws, [2.5, 97.5])

    bf10 = _bf10_jzs(t_stat, n_a, n_b)

    return {
        "diff": round(diff, 3),
        "ci95": [round(float(lo), 3), round(float(hi), 3)],
        "d": round(float(np.median(d_draws)), 3),
        "d_ci95": [round(float(d_lo), 3), round(float(d_hi), 3)],
        "p_direction": round(p_positive, 4),
        "bf10": round(bf10, 3),
        "bf_label": _bf_label(bf10),
        "prior": f"JZS Cauchy, r={BF_PRIOR_R}",
        "n_a": n_a, "n_b": n_b,
    }


async def analyze_experiment(db: AsyncSession, experiment_id: int) -> dict:
    exp = (await db.execute(
        select(Experiment).where(Experiment.id == experiment_id)
    )).scalar_one_or_none()
    if exp is None:
        return {"error": "experiment not found"}

    all_rows = (await db.execute(
        select(ExperimentAssignment)
        .where(ExperimentAssignment.experiment_id == experiment_id)
        .order_by(ExperimentAssignment.date)
    )).scalars().all()
    # Only eligibility-CONFIRMED days are randomized experiment days; night-before
    # draws that turned out ineligible (or are still pending) never enter the ITT.
    assignments = [a for a in all_rows if a.eligible_confirmed]
    n_ineligible = sum(1 for a in all_rows if a.eligible_confirmed is False)
    n_pending = sum(1 for a in all_rows if a.eligible_confirmed is None)

    days = [a.date for a in assignments]
    behavior = await _behavior_by_day(db, days)
    # Nutrition kind: compliance from the food log (shared definition), with a manual
    # override from Behaviors → Record taking precedence when set.
    ate: dict = {}
    if exp.kind == "nutrition" and days:
        from app.services.evening_eating import ate_after_dinner
        ate = await ate_after_dinner(db, min(days), max(days))
    override = {a.date: a.complied for a in assignments}

    def _followed(arm: str, d: date):
        if exp.kind == "nutrition":
            if override.get(d) is not None:
                return override[d]
            e = ate.get(d)
            return None if e is None else (e if arm == exp.arm_a else not e)
        b = behavior.get(d)
        return _complied(arm, b) if b else None

    outcome_keys = OUTCOMES_BY_KIND.get(exp.kind, DEFAULT_OUTCOMES)

    # Outcome lookups over the needed date range
    subj_rows: dict[date, SubjectiveLog] = {}
    dm_rows: dict[date, DailyMetric] = {}
    if days:
        lo, hi = min(days), max(days) + timedelta(days=1)
        subj_rows = {r.date: r for r in (await db.execute(
            select(SubjectiveLog).where(SubjectiveLog.date >= lo).where(SubjectiveLog.date <= hi)
        )).scalars().all()}
        dm_rows = {r.date: r for r in (await db.execute(
            select(DailyMetric).where(DailyMetric.date >= lo).where(DailyMetric.date <= hi)
        )).scalars().all()}

    hm_rows: dict[date, float] = {}
    if days and "sleep_hr_elev" in outcome_keys:
        lday = local_date_sql(HealthMetric.timestamp)
        hm_rows = {date.fromisoformat(str(d)): float(v) for d, v in (await db.execute(
            select(lday, func.avg(HealthMetric.value))
            .where(HealthMetric.metric_type == "sleep_hr_elevation")
            .where(lday >= str(min(days))).where(lday <= str(max(days) + timedelta(days=1)))
            .group_by(lday))).all() if v is not None}

    def _outcome_value(key: str, day: date):
        label, attr, offset = OUTCOMES[key]
        target = day + timedelta(days=offset)
        if key == "hrv_z":
            dm = dm_rows.get(target)
            return dm.hrv_z_score if dm else None
        if key == "sleep_hr_elev":
            return hm_rows.get(target)
        subj = subj_rows.get(target)
        return getattr(subj, attr) if subj else None

    per_arm = {exp.arm_a: [], exp.arm_b: []}
    compliance = {exp.arm_a: {"complied": 0, "total": 0}, exp.arm_b: {"complied": 0, "total": 0}}
    for a in assignments:
        per_arm[a.arm].append(a)
        ok = _followed(a.arm, a.date)
        if ok is not None:
            compliance[a.arm]["total"] += 1
            compliance[a.arm]["complied"] += int(ok)

    analyses = {}
    for key in outcome_keys:
        label, _attr, offset = OUTCOMES[key]
        vals_a = [v for v in (_outcome_value(key, a.date) for a in per_arm[exp.arm_a]) if v is not None]
        vals_b = [v for v in (_outcome_value(key, a.date) for a in per_arm[exp.arm_b]) if v is not None]
        itt = _perm_test(vals_a, vals_b)

        # Per-protocol (secondary, NON-randomized): only complied days
        pp_a = [v for a in per_arm[exp.arm_a] if _followed(exp.arm_a, a.date)
                and (v := _outcome_value(key, a.date)) is not None]
        pp_b = [v for a in per_arm[exp.arm_b] if _followed(exp.arm_b, a.date)
                and (v := _outcome_value(key, a.date)) is not None]
        pp = _perm_test(pp_a, pp_b)

        # Same ITT arrays as the permutation test — a second reading of one contrast,
        # never a second contrast to choose between after the fact.
        bayes = _bayes_estimate(vals_a, vals_b)

        analyses[key] = {"label": label, "itt": itt, "bayes": bayes, "per_protocol": pp,
                         "primary": key == exp.primary_outcome}

    def _rate(c):
        return round(c["complied"] / c["total"], 2) if c["total"] else None

    return {
        "experiment": {
            "id": exp.id, "label": exp.label,
            "arm_a": exp.arm_a, "arm_b": exp.arm_b,
            "primary_outcome": exp.primary_outcome,
            "started_at": exp.started_at.isoformat() if exp.started_at else None,
            "ended_at": exp.ended_at.isoformat() if exp.ended_at else None,
        },
        "progress": {
            "n_a": len(per_arm[exp.arm_a]),
            "n_b": len(per_arm[exp.arm_b]),
            "target_per_arm": exp.target_per_arm,
            "n_ineligible_draws": n_ineligible,
            "n_pending": n_pending,
        },
        "compliance": {
            exp.arm_a: _rate(compliance[exp.arm_a]),
            exp.arm_b: _rate(compliance[exp.arm_b]),
        },
        "analyses": analyses,
        "notes": "ITT (by assignment) is the primary, randomization-protected estimate. "
                 "Per-protocol conditions on compliance and is NOT randomized — "
                 "context only.",
    }
