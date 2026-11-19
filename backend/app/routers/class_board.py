"""Class board → weight targets (note 91). See services/class_board for the rule."""
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import ClassBoard, HevyWorkout, Workout
from app.routers.blood import _is_heif, _sniff_image
from app.services.class_board import (board_with_targets, exercise_catalog, parse_board,
                                      readiness_today, recommend, remember_alias,
                                      summarize_outcome)
from app.timeutils import local_today, on_local_days
from app.workout_categories import categorize_activity

router = APIRouter(prefix="/api/class-board", tags=["class-board"])

# The client sends a ≤1568 px JPEG (a few hundred KB); this is a guard, not a size.
MAX_BOARD_BYTES = 3_500_000
MAX_BOARD_TEXT = 4000
# A class runs about 45-60 min from the board read (the board is usually read a few minutes before class).
BOARD_CLASS_DONE_MIN = 75


@router.post("/parse")
async def parse(
    file: Optional[UploadFile] = File(None),
    text: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """A board photo (`file`) or the board typed out (`text`) → exercises + targets.

    Stores the read (class_boards) and returns its id so a re-match can be recorded.
    """
    image = media = None
    if file is not None:
        image = await file.read()
        if not image:
            raise HTTPException(status_code=422, detail="The photo is empty")
        media = _sniff_image(image)
        if media is None:
            raise HTTPException(
                status_code=422,
                detail="HEIC photo — this browser couldn't convert it; retake it in Safari"
                if _is_heif(image) else "Not a PNG/JPEG/WebP image")
        if len(image) > MAX_BOARD_BYTES:
            raise HTTPException(status_code=413,
                                detail=f"Photo is {len(image) // 1024} KB (max "
                                       f"{MAX_BOARD_BYTES // 1024} KB)")
    elif text and text.strip():
        text = text.strip()[:MAX_BOARD_TEXT]
    else:
        raise HTTPException(status_code=422, detail="Send a photo of the board or type it")

    catalog = await exercise_catalog(db)
    parsed = await parse_board(catalog, image=image, media_type=media,
                               text=None if image is not None else text)
    if parsed is None:
        raise HTTPException(status_code=503,
                            detail="Couldn't read the board — AI unavailable; check the AI status")
    items = await board_with_targets(db, parsed, catalog)
    row = ClassBoard(date=local_today(), source="photo" if image is not None else "text",
                     model=parsed.get("model"), ms=parsed.get("ms"), items=items,
                     unreadable=parsed.get("unreadable") or [])
    db.add(row)
    await db.commit()
    return {"id": row.id, "items": items, "unreadable": row.unreadable,
            "readiness": await readiness_today(db), "model": row.model, "ms": row.ms}


class TargetIn(BaseModel):
    exercise: str
    reps_target: Optional[int] = None
    board_id: Optional[int] = None
    n: Optional[int] = None
    idx: Optional[int] = None           # position in the board's items (n can repeat per circuit)
    implement: Optional[str] = None     # from the board line; medicine balls step by size


@router.post("/target")
async def target(body: TargetIn, db: AsyncSession = Depends(get_db),
                 _: str = Depends(get_current_user)):
    """The target for one exercise after a re-match; records the pick on the board read."""
    catalog = await exercise_catalog(db)
    types = {c["title"]: c["type"] for c in catalog}
    if body.exercise not in types:
        raise HTTPException(status_code=404, detail="Not an exercise in your log")
    rec = await recommend(db, body.exercise, types[body.exercise], body.reps_target,
                          body.implement)
    if body.board_id is not None and (body.idx is not None or body.n is not None):
        row = (await db.execute(select(ClassBoard).where(ClassBoard.id == body.board_id))
               ).scalar_one_or_none()
        if row is not None:
            key = body.idx if body.idx is not None else body.n
            row.picks = {**(row.picks or {}), str(key): body.exercise}
            items = row.items or []
            if body.idx is not None and 0 <= body.idx < len(items):
                it = items[body.idx]
                await remember_alias(db, [it.get("board_text"), it.get("name")],
                                     body.exercise, "pick")
            await db.commit()
    return {"exercise": body.exercise, "type": types[body.exercise], "rec": rec}


class EasyIn(BaseModel):
    easy: bool


@router.post("/{board_id}/easy")
async def set_easy(board_id: int, body: EasyIn, db: AsyncSession = Depends(get_db),
                   _: str = Depends(get_current_user)):
    """Record the Easy day switch for this class (targets themselves are client-side)."""
    row = (await db.execute(select(ClassBoard).where(ClassBoard.id == board_id))
           ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="No such board read")
    row.easy = body.easy
    await db.commit()
    return {"id": row.id, "easy": row.easy}


@router.get("/today")
async def today_board(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)):
    """Today's latest board read as /parse returned it, with his re-matches applied, so
    closing the sheet doesn't lose it (note 96: on 11-03 the same board was read twice 33 s
    apart). Targets are the ones served; a re-matched line gets its target recomputed.
    Null when there is no board today."""
    row = (await db.execute(select(ClassBoard).where(ClassBoard.date == local_today())
                            .order_by(ClassBoard.id.desc()).limit(1))).scalar_one_or_none()
    if row is None:
        return None
    items = [dict(x) for x in (row.items or [])]
    if row.picks:
        types = {c["title"]: c["type"] for c in await exercise_catalog(db)}
        for key, ex in row.picks.items():
            i = int(key) if str(key).isdigit() else -1
            if 0 <= i < len(items) and ex in types:
                it = items[i]
                rec = await recommend(db, ex, types[ex], it.get("reps_target"), it.get("implement"))
                items[i] = {**it, "match": ex, "type": types[ex], "rec": rec,
                            "match_confidence": "picked"}
    return {"id": row.id, "items": items, "unreadable": row.unreadable or [],
            "readiness": await readiness_today(db), "model": row.model, "ms": row.ms,
            "easy": bool(row.easy)}


@router.get("/last")
async def last_result(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)):
    """The most recent checked class: targets vs what was logged (null when none yet)."""
    row = (await db.execute(select(ClassBoard).where(ClassBoard.checked_at.isnot(None))
                            .order_by(ClassBoard.date.desc(), ClassBoard.id.desc()).limit(1))
           ).scalar_one_or_none()
    if row is None:
        return None
    s = summarize_outcome(row.outcome or [])
    return {"date": row.date.isoformat(), "easy": bool(row.easy), "outcome": row.outcome,
            "judged": s["judged"], "at_or_above": s["at_or_above"]}


def board_says_done(first_read_utc: Optional[datetime], now_utc: datetime) -> bool:
    """Today's first board read was long enough ago that class is over."""
    return first_read_utc is not None and now_utc - first_read_utc >= timedelta(minutes=BOARD_CLASS_DONE_MIN)


@router.get("/gym-today")
async def gym_today(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)):
    """Has he already been to the gym today? (note 94: the Today row goes away if so.)
    Both sources: Hevy can lag by days (10-31's class was in Apple Health, not Hevy), and
    Apple Health arrives ~2 h after the fact. Walks and runs are not the gym.
    A board read today more than BOARD_CLASS_DONE_MIN ago also counts (2026-11-10:
    "make sure it disappears after I do a class"): he reads it minutes before class, so by
    then class is over, well before the Apple workout arrives."""
    today = local_today()
    board = (await db.execute(select(ClassBoard.created_at).where(ClassBoard.date == today)
                              .order_by(ClassBoard.id).limit(1))).scalar_one_or_none()
    if board_says_done(board, datetime.utcnow()):
        return {"done": True}
    hevy = (await db.execute(select(HevyWorkout.id).where(on_local_days(
        HevyWorkout.start_time, today)).limit(1))).first()
    if hevy:
        return {"done": True}
    types = (await db.execute(select(Workout.activity_type).where(on_local_days(
        Workout.start_time, today)))).scalars().all()
    return {"done": any(categorize_activity(t) in ("strength", "hiit") for t in types)}


@router.get("/exercises")
async def exercises(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)):
    """The exercise names he has logged since March, most used first (the re-match list)."""
    return await exercise_catalog(db)
