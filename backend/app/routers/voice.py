"""Siri Shortcuts voice logging (docs/VOICE_LOGGING.md).

Auth is a static VOICE_TOKEN (Bearer) that grants ONLY these routes — a Shortcut can't
manage JWT expiry, and a leaked Shortcut must not be a key to the whole app.

Every user-facing outcome is HTTP 200 with `say` — the sentence the Shortcut speaks —
including "I didn't catch that". Shortcuts' "Get Contents of URL" makes non-2xx awkward
to branch on, and a Shortcut that can always just speak `say` has one code path. Only
auth failures use real status codes.
"""
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import bearer_matches
from app.config import settings
from app.database import get_db
from app.services import voice

router = APIRouter(prefix="/api/voice", tags=["voice"])


def _verify_voice_token(authorization: str = Header(default="")) -> None:
    if not settings.voice_token:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="VOICE_TOKEN not configured on server")
    if not bearer_matches(authorization, settings.voice_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid voice token")


class SpokenIn(BaseModel):
    text: str = Field(default="", max_length=500)
    meal: str | None = None            # log meal: the meal picked from the menu


def _out(ok: bool, say: str, **extra) -> dict:
    return {"ok": ok, "say": say, "draft_id": None, "confirm": False, **extra}


@router.post("/drafts/{draft_id}/commit")
async def commit(draft_id: str, db: AsyncSession = Depends(get_db),
                 _: None = Depends(_verify_voice_token)):
    d = await voice.get_open_draft(db, draft_id)
    if d is None:
        return _out(False, "That one expired or was already logged. Start again.")
    await voice.commit_draft(db, d)
    return _out(True, "Logged.", logged=True)


@router.delete("/drafts/{draft_id}")
async def cancel(draft_id: str, db: AsyncSession = Depends(get_db),
                 _: None = Depends(_verify_voice_token)):
    d = await voice.get_open_draft(db, draft_id)
    if d is not None:
        d.expires_at = d.created_at            # expire now; keep the row as a record
        await db.commit()
    return _out(True, "Cancelled. Nothing was logged.")


@router.post("/undo")
async def undo(db: AsyncSession = Depends(get_db), _: None = Depends(_verify_voice_token)):
    d = await voice.undo_last(db)
    if d is None:
        return _out(False, "There's nothing from the last 30 minutes to undo.")
    return _out(True, f"Removed {d.say}.")


# LAST on purpose: a catch-all POST /{kind} registered earlier would swallow /undo.
@router.post("/{kind}")
async def spoken(kind: str, body: SpokenIn, db: AsyncSession = Depends(get_db),
                 _: None = Depends(_verify_voice_token)):
    """Interpret what was said. QUICK kinds log immediately; the rest return a draft
    whose `say` ends in a question, to be confirmed with /drafts/{id}/commit."""
    if kind not in voice.INTERPRETERS:
        return _out(False, f"Logging {kind} by voice isn't set up yet.")
    if not body.text.strip():
        return _out(False, "I didn't hear anything. Try again.")
    interp, draft = await voice.create_draft(db, kind, body.text, meal=body.meal)
    if draft is None:
        return _out(False, interp.say)
    if kind in voice.QUICK:
        await voice.commit_draft(db, draft)
        say = f"Logged {interp.say}."
        if kind == "water":
            when = voice.local_now().date()
            say += f" {voice._fmt_oz(await voice.water_day_total(db, when))} ounces today."
        return _out(True, say, logged=True)
    return _out(True, f"{interp.say}. Log it?", draft_id=draft.id, confirm=True)
