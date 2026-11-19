"""Randomized n-of-1 coach experiments — CRUD, manual assignment, ITT analysis."""
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import Experiment, ExperimentAssignment
from app.timeutils import local_today

router = APIRouter(prefix="/api/experiments", tags=["experiments"])


class ExperimentIn(BaseModel):
    label: str
    arm_a: str
    arm_b: str
    primary_outcome: str = "subj_energy"
    notes: Optional[str] = None
    # "training" | "supplement" (or any other non-training tag). See models.Experiment.
    kind: str = "training"
    # Accrual target for this trial. Omit to take the default; set it when the
    # pre-registration says something else (see Experiment.target_per_arm).
    target_per_arm: Optional[int] = None


from app.services.coach import experiment_copy  # noqa: E402


@router.get("")
async def list_experiments(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    rows = (await db.execute(
        select(Experiment).order_by(Experiment.id.desc())
    )).scalars().all()
    out = []
    for e in rows:
        n = (await db.execute(
            select(func.count(ExperimentAssignment.id))
            .where(ExperimentAssignment.experiment_id == e.id)
        )).scalar() or 0
        out.append({
            "id": e.id, "label": e.label, "arm_a": e.arm_a, "arm_b": e.arm_b,
            "primary_outcome": e.primary_outcome,
            "kind": e.kind,
            "started_at": e.started_at.isoformat() if e.started_at else None,
            "ended_at": e.ended_at.isoformat() if e.ended_at else None,
            "active": e.ended_at is None,
            "n_assignments": n,
            # The pre-registration record and any protocol amendments. Stored since the
            # beginning but never surfaced, which made the thing that distinguishes a
            # pre-registered trial from a fishing expedition invisible in the app.
            "notes": e.notes,
            # What the Today card and Record row say for this experiment (non-training).
            "copy": experiment_copy(e) if e.kind != "training" else None,
        })
    return out


@router.post("")
async def create_experiment(
    body: ExperimentIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    from app.services.coach import TARGET_N_PER_ARM, get_active_experiment
    # Only TRAINING experiments are mutually exclusive: their arm becomes the coach's
    # daily recommendation, and two would fight over it. Other kinds run concurrently —
    # independent randomization makes them orthogonal, so a second experiment costs
    # nothing in precision and buys a second answer over the same days.
    if body.kind == "training" and await get_active_experiment(db) is not None:
        raise HTTPException(status_code=409,
                            detail="A training experiment is already active — end it "
                                   "first (only one may drive the coach card).")
    exp = Experiment(
        label=body.label, arm_a=body.arm_a, arm_b=body.arm_b,
        primary_outcome=body.primary_outcome, notes=body.notes,
        kind=body.kind, started_at=local_today(),
        target_per_arm=body.target_per_arm or TARGET_N_PER_ARM,
    )
    db.add(exp)
    await db.commit()
    await db.refresh(exp)
    return {"id": exp.id, "label": exp.label, "started_at": exp.started_at.isoformat()}


@router.post("/{experiment_id}/end")
async def end_experiment(
    experiment_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    exp = (await db.execute(
        select(Experiment).where(Experiment.id == experiment_id)
    )).scalar_one_or_none()
    if exp is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    if exp.ended_at is None:
        exp.ended_at = local_today()
        await db.commit()
    return {"id": exp.id, "ended_at": exp.ended_at.isoformat()}


@router.post("/assign-today")
async def assign_today_manual(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Manual draw+confirm for today (idempotent — same as the morning pipeline's
    coach step). NOTE: the response reveals the mechanism — use deliberately."""
    from app.services.coach import confirm_today
    return await confirm_today(db)


@router.post("/run-morning-pipeline")
async def run_morning_pipeline_manual(
    force: bool = False,
    _: str = Depends(get_current_user),
):
    """Manual trigger of the full morning pipeline (recompute → coach confirm →
    summary+push → illness check). `force=true` re-runs even if it ran today."""
    from app.services.morning import run_morning_pipeline
    return await run_morning_pipeline(trigger="manual", force=force)


@router.get("/active/accrual")
async def active_accrual(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Blinding-safe progress for the active experiment: per-arm eligible counts,
    ineligible breakdown, and a projected completion date. Deliberately NO
    outcomes — this is the 'how's it going' view that doesn't unblind (unlike
    /analysis). Returns {active: false} when nothing is running."""
    from app.services.coach import get_active_experiments

    exps = await get_active_experiments(db)
    if not exps:
        return {"active": False, "experiments": []}
    today = local_today()
    return {"active": True,
            "experiments": [await _accrual_for(db, e, today) for e in exps]}


async def _accrual_for(db: AsyncSession, exp, today) -> dict:
    # Each trial's own registered target — a shared constant here reported a 30/arm
    # goal for a trial registered at 175/arm, and with it a completion date a year early.
    target = exp.target_per_arm
    rows = (await db.execute(
        select(ExperimentAssignment).where(ExperimentAssignment.experiment_id == exp.id)
    )).scalars().all()

    per_arm = {exp.arm_a: 0, exp.arm_b: 0}
    ineligible: dict = {}
    for a in rows:
        if a.eligible_confirmed:
            per_arm[a.arm] = per_arm.get(a.arm, 0) + 1
        else:
            reason = a.ineligible_reason or "pending/other"
            ineligible[reason] = ineligible.get(reason, 0) + 1

    n_a, n_b = per_arm.get(exp.arm_a, 0), per_arm.get(exp.arm_b, 0)
    total_eligible = n_a + n_b
    target_total = target * 2

    started = exp.started_at
    days_running = max(1, (today - started).days + 1) if started else 1
    rate_per_day = total_eligible / days_running          # eligible days / calendar day
    remaining = max(0, target_total - total_eligible)
    projected = None
    if remaining == 0:
        projected = "complete"
    elif rate_per_day > 0:
        days_left = remaining / rate_per_day
        projected = (today + timedelta(days=round(days_left))).isoformat()

    return {
        "id": exp.id,
        "label": exp.label,
        "notes": exp.notes,
        "kind": exp.kind,
        "arm_a": exp.arm_a,
        "arm_b": exp.arm_b,
        "started_at": started.isoformat() if started else None,
        "days_running": days_running,
        "target_per_arm": target,
        "n_a": n_a,
        "n_b": n_b,
        "total_eligible": total_eligible,
        "pct_complete": round(min(n_a, n_b) / target * 100),
        "ineligible": ineligible,
        "n_ineligible": sum(ineligible.values()),
        "projected_completion": projected,
    }


class DoseIn(BaseModel):
    # NOT `date: Optional[date]`: a field named `date` shadows the type in the class body,
    # Pydantic resolved it to NoneType, and every request carrying a date 422'd — i.e. all
    # backfill from Behaviors → Record (note 65; the evening card sends no date, so it
    # worked). Same gotcha as WellbeingIn in July. The wire name stays "date".
    model_config = ConfigDict(populate_by_name=True)
    day: Optional[date] = Field(default=None, alias="date")   # defaults to today
    complied: bool
    dose_minute: Optional[int] = None  # minutes since local midnight


@router.post("/{experiment_id}/dose")
async def log_dose(
    experiment_id: int,
    body: DoseIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Record whether the assigned intervention was actually taken, and when.

    Does NOT change the arm or the eligibility verdict — the randomisation is fixed at
    draw time and the ITT analysis reads only the assignment. This is per-protocol
    context, and a drift detector: an acute intervention needs a constant
    dose-to-outcome interval, and doses landing at the wrong hour quietly convert
    treatment days into placebo days.
    """
    day = body.day or local_today()
    row = (await db.execute(
        select(ExperimentAssignment)
        .where(ExperimentAssignment.experiment_id == experiment_id)
        .where(ExperimentAssignment.date == day)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"no assignment for {day.isoformat()} — nothing to log against")
    row.complied = body.complied
    if body.dose_minute is not None:
        row.dose_minute = max(0, min(1439, body.dose_minute))
    await db.commit()
    return {"date": day.isoformat(), "arm": row.arm, "complied": row.complied,
            "dose_minute": row.dose_minute}


@router.get("/{experiment_id}/today")
async def assignment_today(
    experiment_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Today's arm for this experiment, plus whether the dose has been logged yet."""
    day = local_today()
    exp = (await db.execute(
        select(Experiment).where(Experiment.id == experiment_id)
    )).scalar_one_or_none()
    if exp is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    row = (await db.execute(
        select(ExperimentAssignment)
        .where(ExperimentAssignment.experiment_id == experiment_id)
        .where(ExperimentAssignment.date == day)
    )).scalar_one_or_none()
    if row is None:
        return {"experiment_id": exp.id, "label": exp.label, "date": day.isoformat(),
                "arm": None, "eligible": None, "complied": None, "dose_minute": None}
    return {"experiment_id": exp.id, "label": exp.label, "kind": exp.kind,
            "date": day.isoformat(), "arm": row.arm,
            "is_treatment": row.arm == exp.arm_a,
            "eligible": row.eligible_confirmed,
            "complied": row.complied, "dose_minute": row.dose_minute}


@router.get("/{experiment_id}/assignments")
async def assignments_range(
    experiment_id: int,
    start: date,
    end: date,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Assignments over a date range, keyed by date — backs the Habits calendar row.

    The calendar writes back through /dose, so `experiment_assignments` stays the single
    source of truth for compliance. A parallel habit_logs column would be a second record
    of the same fact, and the two would eventually disagree while the per-protocol
    analysis reads only this one.
    """
    exp = (await db.execute(
        select(Experiment).where(Experiment.id == experiment_id)
    )).scalar_one_or_none()
    if exp is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    rows = (await db.execute(
        select(ExperimentAssignment)
        .where(ExperimentAssignment.experiment_id == experiment_id)
        .where(ExperimentAssignment.date >= start)
        .where(ExperimentAssignment.date <= end)
    )).scalars().all()
    # Nutrition kind: whether the rule was followed, read from the food log (shared
    # definition). `complied` stays the manual override; the row shows override ?? auto.
    ate = {}
    if exp.kind == "nutrition" and rows:
        from app.services.evening_eating import ate_after_dinner
        ate = await ate_after_dinner(db, start, end)

    def auto(r):
        e = ate.get(r.date)
        return None if e is None else (e if r.arm == exp.arm_a else not e)

    return {
        "experiment_id": exp.id, "arm_a": exp.arm_a, "arm_b": exp.arm_b,
        "days": {r.date.isoformat(): {
            "arm": r.arm,
            "is_treatment": r.arm == exp.arm_a,
            "complied": r.complied,
            "auto_followed": auto(r) if exp.kind == "nutrition" else None,
            "dose_minute": r.dose_minute,
            "eligible": r.eligible_confirmed,
        } for r in rows},
    }


@router.post("/{experiment_id}/schedule")
async def schedule_ahead(
    experiment_id: int,
    days: int = Query(default=7, ge=1, le=31),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Draw the next `days` assignments now and return them.

    For an intervention you physically prepare — loading a week's pill organiser — the
    schedule has to be known in advance. Drawing ahead is statistically clean because
    the draw never depended on eligibility (the morning pipeline confirms that
    separately), and it is strictly BETTER than deciding day by day: the arm is recorded
    before any outcome exists, so it stays a randomisation rather than a recollection.

    Idempotent — already-drawn days are returned as-is, never redrawn.
    """
    from app.services.coach import draw_for_experiment

    exp = (await db.execute(
        select(Experiment).where(Experiment.id == experiment_id)
    )).scalar_one_or_none()
    if exp is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    if exp.ended_at is not None:
        raise HTTPException(status_code=400, detail="experiment has ended")

    today = local_today()
    out = []
    for i in range(days):
        out.append(await draw_for_experiment(db, exp, today + timedelta(days=i)))
    return {"experiment": exp.label, "kind": exp.kind, "days": days, "schedule": out}


@router.get("/{experiment_id}/analysis")
async def experiment_analysis(
    experiment_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    from app.services.coach import analyze_experiment
    return await analyze_experiment(db, experiment_id)
