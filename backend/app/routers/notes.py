"""Quick-capture notes — feature ideas and bugs jotted from the phone.

Deliberately tiny. The value is entirely in how little stands between noticing
something and recording it, so there is no category, no priority, no title.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import AppNote

router = APIRouter(prefix="/api/notes", tags=["notes"])


def _ser(n: AppNote) -> dict:
    return {
        "id": n.id,
        "body": n.body,
        "status": n.status,
        "created_at": n.created_at.isoformat() if n.created_at else None,
        "resolved_at": n.resolved_at.isoformat() if n.resolved_at else None,
    }


@router.get("")
async def list_notes(
    include_done: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Newest first. Done notes are hidden by default but never deleted."""
    q = select(AppNote).order_by(AppNote.created_at.desc())
    if not include_done:
        q = q.where(AppNote.status == "open")
    rows = (await db.execute(q)).scalars().all()
    return {"notes": [_ser(n) for n in rows],
            "open_count": sum(1 for n in rows if n.status == "open")}


class NoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


@router.post("", status_code=201)
async def add_note(body: NoteIn, db: AsyncSession = Depends(get_db),
                   _: str = Depends(get_current_user)):
    text = body.body.strip()
    if not text:
        raise HTTPException(status_code=422, detail="note is empty")
    n = AppNote(body=text, status="open", created_at=datetime.utcnow())
    db.add(n)
    await db.commit()
    await db.refresh(n)
    return _ser(n)


class NotePatch(BaseModel):
    body: str | None = None
    status: str | None = None      # 'open' | 'done'


@router.patch("/{note_id}")
async def patch_note(note_id: int, body: NotePatch, db: AsyncSession = Depends(get_db),
                     _: str = Depends(get_current_user)):
    n = (await db.execute(select(AppNote).where(AppNote.id == note_id))).scalar_one_or_none()
    if n is None:
        raise HTTPException(status_code=404, detail="note not found")
    if body.body is not None and body.body.strip():
        n.body = body.body.strip()
    if body.status in ("open", "done"):
        n.status = body.status
        n.resolved_at = datetime.utcnow() if body.status == "done" else None
    await db.commit()
    await db.refresh(n)
    return _ser(n)


@router.delete("/{note_id}", status_code=204)
async def delete_note(note_id: int, db: AsyncSession = Depends(get_db),
                      _: str = Depends(get_current_user)):
    n = (await db.execute(select(AppNote).where(AppNote.id == note_id))).scalar_one_or_none()
    if n is None:
        raise HTTPException(status_code=404, detail="note not found")
    await db.delete(n)
    await db.commit()
