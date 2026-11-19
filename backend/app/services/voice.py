"""Voice logging — Siri Shortcuts → structured logs (docs/VOICE_LOGGING.md).

One Shortcut per logger ("log water", "log caffeine", "log alcohol", "log meal"), so
routing is by the phrase you chose, never guessed. Every kind has an INTERPRETER (text →
structured payload + the sentence Siri reads back) and a COMMITTER (payload → rows). A
kind in QUICK commits straight away (water); the rest go draft → "Log it" → commit.

Rules:
  • Numbers come from presets, My Foods or fixed reference tables — a model may only
    extract what was SAID. The readback states where an estimate came from.
  • Every row written carries entry_channel='voice', and the commit records which rows
    it wrote, so undo removes exactly those and nothing else.
  • Failure is a spoken sentence (ok=False), never a silent guess.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import HealthMetric, VoiceDraft
from app.services import intake
from app.services.voice_time import parse_spoken_time, say_when
from app.timeutils import local_now as _tz_local_now

DRAFT_TTL = timedelta(minutes=10)
UNDO_WINDOW = timedelta(minutes=30)


def local_now() -> datetime:
    # Wall-clock minute where the user is (timeutils.local_now is zone-aware). The import
    # is aliased: the 2026-10-19 tz pass rewrote this body to call `local_now()`, which
    # resolved to THIS function — infinite recursion on every voice log (fixed 10-21).
    return _tz_local_now().replace(tzinfo=None, second=0, microsecond=0)


@dataclass
class Interpretation:
    ok: bool
    say: str
    payload: dict = field(default_factory=dict)


# ── Water ─────────────────────────────────────────────────────────────────────

_ML_PER_OZ = 29.5735
_NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
              "six": 6, "half a": 0.5, "half an": 0.5, "half": 0.5, "a half": 0.5}
_NUMW = "|".join(sorted((re.escape(k) for k in _NUM_WORDS), key=len, reverse=True))
_QTY = rf"(\d+(?:\.\d+)?|{_NUMW})"
# Words that name a preset without being its label. The preset's own id and label are
# always matched too, so a custom preset ("Hydro flask") works with no code change.
_WATER_SYNONYMS = {"bottle": ("bottle", "water bottle", "refill", "bottles", "refills"),
                   "glass": ("glass", "glasses", "cup", "cups"),
                   "pint": ("pint", "pints")}


def _qty(tok: Optional[str]) -> float:
    if not tok:
        return 1.0
    tok = tok.strip().lower()
    return float(tok) if tok[0].isdigit() else float(_NUM_WORDS.get(tok, 1))


def _fmt_oz(oz: float) -> str:
    return f"{oz:g}" if abs(oz - round(oz)) > 0.05 else f"{round(oz)}"


def _plural(q: float, noun: str) -> str:
    if q == 0.5:
        return f"half a {noun}"
    if q == 1:
        return f"a {noun}"
    return f"{q:g} {noun if noun.endswith('s') else noun + 's'}"


def interpret_water_amount(text: str, presets: list) -> Optional[tuple[float, Optional[str]]]:
    """(oz, detail) from what was said, or None. `detail` is how it was said when that
    wasn't plain ounces ("a refill", "500 milliliters") — spoken back so a preset match
    can be checked. Pure — tested in verify_voice_water."""
    t = " " + (text or "").lower().replace("-", " ") + " "
    t = re.sub(r"\bof water\b|\bwater\b", " ", t)
    m = re.search(rf"{_QTY}\s*(?:fl\.?\s*)?(?:oz|ounces?|ozs)\b", t)
    if m:
        return _qty(m.group(1)), None
    m = re.search(rf"{_QTY}\s*(?:ml|milliliters?|millilitres?|mils?)\b", t)
    if m:
        ml = _qty(m.group(1))
        return ml / _ML_PER_OZ, f"{ml:g} milliliters"
    m = re.search(rf"{_QTY}?\s*(?:l|liters?|litres?)\b", t)
    if m and (m.group(1) or re.search(r"\b(?:liters?|litres?)\b", t)):
        n = _qty(m.group(1))
        return n * 1000 / _ML_PER_OZ, _plural(n, "liter")
    for p in presets:
        names = {str(p.get("id", "")).lower(), str(p.get("label", "")).lower(),
                 *_WATER_SYNONYMS.get(p.get("id"), ())}
        names = sorted((n for n in names if n), key=len, reverse=True)
        for n in names:
            m = re.search(rf"(?:{_QTY}\s+)?\b{re.escape(n)}\b", t)
            if m:
                q = _qty(m.group(1))
                return q * float(p["oz"]), _plural(q, str(p.get("label") or n).lower())
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*", t)          # a bare number = ounces
    if m:
        return float(m.group(1)), None
    return None


async def interpret_water(db: AsyncSession, text: str, now: datetime, **_) -> Interpretation:
    st = parse_spoken_time(text, now)
    got = interpret_water_amount(st.text, await intake.effective_water_presets(db))
    if got is None:
        return Interpretation(False, "I didn't catch an amount. Try something like "
                                     "16 ounces, a glass, or a bottle.")
    oz, detail = got
    if not 0 < oz <= 128:
        return Interpretation(False, f"That's {_fmt_oz(oz)} ounces, which doesn't look right. "
                                     "Try again with a smaller amount.")
    what = f"{_fmt_oz(oz)} ounces of water" + (f", {detail}," if detail else "")
    return Interpretation(True, f"{what} {say_when(st.when, now, st.estimated)}",
                          {"oz": round(oz, 1), "when": st.when.isoformat(timespec="minutes")})


async def commit_water(db: AsyncSession, payload: dict) -> list[dict]:
    when = datetime.fromisoformat(payload["when"])
    row = await intake.log_water(db, when.date(), float(payload["oz"]), when.strftime("%H:%M"))
    row.entry_channel = "voice"
    await db.commit()
    return [{"table": "health_metrics", "metric_type": "water", "id": row.id}]


async def water_day_total(db: AsyncSession, day) -> float:
    return (await intake.day_entries(db, "water", day))["total"]


# ── Registry ──────────────────────────────────────────────────────────────────

INTERPRETERS: dict[str, Callable] = {"water": interpret_water}
COMMITTERS: dict[str, Callable] = {"water": commit_water}
QUICK = {"water"}                      # commit without a "Log it?" step (2026-09-28)


# ── Drafts ────────────────────────────────────────────────────────────────────

async def create_draft(db: AsyncSession, kind: str, text: str, **extra) -> tuple[Interpretation, Optional[VoiceDraft]]:
    now = local_now()
    interp = await INTERPRETERS[kind](db, text, now, **extra)
    if not interp.ok:
        return interp, None
    d = VoiceDraft(id=uuid.uuid4().hex[:16], kind=kind, text=text,
                   payload=json.dumps(interp.payload), say=interp.say,
                   created_at=datetime.utcnow(), expires_at=datetime.utcnow() + DRAFT_TTL)
    db.add(d)
    await db.commit()
    return interp, d


async def get_open_draft(db: AsyncSession, draft_id: str) -> Optional[VoiceDraft]:
    d = (await db.execute(select(VoiceDraft).where(VoiceDraft.id == draft_id))).scalar_one_or_none()
    if d is None or d.committed_at is not None or d.expires_at < datetime.utcnow():
        return None
    return d


async def commit_draft(db: AsyncSession, d: VoiceDraft) -> list[dict]:
    rows = await COMMITTERS[d.kind](db, json.loads(d.payload))
    d.committed_at = datetime.utcnow()
    d.result = json.dumps(rows)
    await db.commit()
    return rows


async def undo_last(db: AsyncSession) -> Optional[VoiceDraft]:
    """Remove every row the most recent voice commit wrote, if it is within the window."""
    d = (await db.execute(
        select(VoiceDraft).where(VoiceDraft.committed_at.isnot(None))
        .where(VoiceDraft.undone_at.is_(None))
        .where(VoiceDraft.committed_at >= datetime.utcnow() - UNDO_WINDOW)
        .order_by(VoiceDraft.committed_at.desc()).limit(1)
    )).scalar_one_or_none()
    if d is None:
        return None
    for r in json.loads(d.result or "[]"):
        if r["table"] == "health_metrics":
            await intake.delete_entry(db, r["metric_type"], r["id"])
        else:                                   # later kinds register their own deleters
            raise ValueError(f"undo: no deleter for {r['table']}")
    d.undone_at = datetime.utcnow()
    await db.commit()
    return d
