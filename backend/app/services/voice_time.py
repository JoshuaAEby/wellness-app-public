"""Spoken-time reader for voice logging (docs/VOICE_LOGGING.md).

Pulls ONE time expression out of what Siri transcribed — "at 8", "8:30 this morning",
"an hour ago", "yesterday at 7 pm", "with lunch" — resolves it against `now`, and returns
the text with that phrase REMOVED, so "cold brew at 9" still matches the "cold brew"
preset. Deterministic on purpose: the resolved time is read back before anything is
saved, and a rule you can test beats a model you can't.

Rules worth knowing:
  • Nothing said → now.
  • A bare hour ("at 8") is the MOST RECENT PAST 8 — 8 am if said at 10 am, 8 pm if said
    at 9 pm, last night's 8 pm if said at 7 am.
  • Never in the future: an am/pm time later than now (with no day word) means
    yesterday ("at 11 pm", said at breakfast, is last night).
  • A day or meal word with no clock ("this morning", "with lunch", "yesterday") gives a
    typical time and is flagged `estimated`, like the food log's meal slots.
Clock phrases need an anchor — "at", a colon, or am/pm — so "16 oz" is never a time.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional

_WORD_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_WORD_MIN = {"fifteen": 15, "thirty": 30, "forty five": 45, "forty-five": 45,
             "oh five": 5, "o'clock": 0, "oclock": 0}
_AMOUNT_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                 "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty five": 45,
                 "forty-five": 45}

# Typical clock for a meal or part of day said without a time (food_log._MEAL_SLOT).
_MEAL_TIME = {"breakfast": time(8, 0), "lunch": time(12, 0), "dinner": time(19, 0),
              "dessert": time(20, 0)}
_PART_TIME = {"morning": time(9, 0), "afternoon": time(15, 0), "evening": time(19, 0),
              "tonight": time(20, 0), "last night": time(20, 0)}

_AMPM = r"(a\.?\s?m\.?|p\.?\s?m\.?)"
_WNUM = "|".join(sorted(_WORD_NUM, key=len, reverse=True))
_WMIN = "|".join(sorted((re.escape(k) for k in _WORD_MIN), key=len, reverse=True))
_AMT = "|".join(sorted((re.escape(k) for k in _AMOUNT_WORDS), key=len, reverse=True))

_RE_AGO = re.compile(
    rf"\b(?:(half an?\s+hour)|(\d+(?:\.\d+)?|{_AMT})\s*(minutes?|mins?|hours?|hrs?))\s+ago\b", re.I)
_RE_NOON = re.compile(r"\b(?:at\s+)?(noon|midday|midnight)\b", re.I)
# "at 7 percent", "at 12 ounces", "at 200 mg" are amounts, not times — the unit that
# follows vetoes the match (found by the test file: "16 ounce IPA at 7 percent" -> 7 AM).
_NOT_TIME = r"(?!\s*(?:%|percent|per\s?cent|abv|proof|oz\b|ounces?|mg\b|milligrams?|ml\b|degrees?))"
_RE_AT_WORD = re.compile(rf"\bat\s+({_WNUM})(?:\s+({_WMIN}))?{_NOT_TIME}(?:\s*{_AMPM})?(?=\W|$)", re.I)
_RE_AT_NUM = re.compile(rf"\bat\s+(\d{{1,2}})(?::(\d{{2}}))?{_NOT_TIME}(?:\s*{_AMPM})?(?=\W|$)", re.I)
_RE_AMPM = re.compile(rf"\b(\d{{1,2}})(?::(\d{{2}}))?\s*{_AMPM}(?=\W|$)", re.I)
_RE_COLON = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_RE_MEAL = re.compile(r"\b(?:at|with|after|during)\s+(breakfast|lunch|dinner|dessert)\b", re.I)
_RE_DAY = re.compile(r"\b(yesterday|last night|this morning|this afternoon|this evening|"
                     r"tonight|earlier today|today)\b", re.I)


@dataclass
class SpokenTime:
    text: str                      # input with the time phrase removed
    when: datetime                 # resolved LOCAL naive datetime
    said: Optional[str] = None     # the phrase that was removed, None = nothing said
    estimated: bool = False        # a typical time, not a stated one


def _tidy(text: str) -> str:
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\s+([,.;!?])", r"\1", text)
    return text.strip(" ,.;-")


def _cut(text: str, m: re.Match) -> str:
    return text[:m.start()] + " " + text[m.end():]


def _hour24(h: int, ampm: Optional[str]) -> int:
    if not ampm:
        return h
    pm = ampm.lower().replace(".", "").replace(" ", "").startswith("p")
    h = h % 12
    return h + 12 if pm else h


def _recent_past(now: datetime, h: int, m: int, day_hint: Optional[date]) -> datetime:
    """Most recent past occurrence of an ambiguous h (1-12): am or pm, today or yesterday."""
    days = [day_hint] if day_hint else [now.date(), now.date() - timedelta(days=1)]
    cands = []
    for d in days:
        for hh in ({h % 12, h % 12 + 12} if 1 <= h <= 12 else {h}):
            cands.append(datetime.combine(d, time(hh % 24, m)))
    past = [c for c in cands if c <= now + timedelta(minutes=2)]
    return max(past) if past else min(cands)


def parse_spoken_time(text: str, now: datetime) -> SpokenTime:
    """`now` is LOCAL naive. See the module docstring for the rules."""
    raw = text or ""
    work = raw
    said: list[str] = []

    # Day qualifier first — it disambiguates any clock phrase that follows.
    day_word = None
    m = _RE_DAY.search(work)
    if m:
        day_word = m.group(1).lower()
        said.append(m.group(0))
        work = _cut(work, m)
    yesterday = day_word in ("yesterday", "last night")
    day_hint = now.date() - timedelta(days=1) if yesterday else (
        now.date() if day_word in ("this morning", "this afternoon", "this evening",
                                   "tonight", "earlier today", "today") else None)
    part = {"this morning": "morning", "this afternoon": "afternoon",
            "this evening": "evening", "tonight": "tonight",
            "last night": "last night"}.get(day_word)

    # 1. Relative: "an hour ago", "20 minutes ago", "half an hour ago".
    m = _RE_AGO.search(work)
    if m:
        if m.group(1):
            delta = timedelta(minutes=30)
        else:
            n = m.group(2).lower()
            qty = float(n) if n[0].isdigit() else _AMOUNT_WORDS[n]
            unit = m.group(3).lower()
            delta = timedelta(hours=qty) if unit.startswith("h") else timedelta(minutes=qty)
        said.append(m.group(0))
        return SpokenTime(_tidy(_cut(work, m)), (now - delta).replace(second=0, microsecond=0),
                          " ".join(said))

    # 2. Clock phrases.
    hm = None           # (hour, minute, ampm or None)
    for rx in (_RE_NOON, _RE_AT_WORD, _RE_AT_NUM, _RE_AMPM, _RE_COLON):
        m = rx.search(work)
        if not m:
            continue
        if rx is _RE_NOON:
            hm = (0 if m.group(1).lower() == "midnight" else 12, 0, "set")
        elif rx is _RE_AT_WORD:
            hm = (_WORD_NUM[m.group(1).lower()],
                  _WORD_MIN.get((m.group(2) or "").lower(), 0), m.group(3))
        elif rx is _RE_COLON:
            hm = (int(m.group(1)), int(m.group(2)), None)
        else:
            hm = (int(m.group(1)), int(m.group(2) or 0), m.group(3))
        if not (0 <= hm[0] <= 23 and 0 <= hm[1] <= 59):
            hm = None
            continue
        said.append(m.group(0))
        work = _cut(work, m)
        break

    if hm is not None:
        h, mi, ampm = hm
        if ampm == "set":                                  # noon / midnight
            base = day_hint or now.date()
            when = datetime.combine(base, time(h, mi))
            if when > now + timedelta(minutes=2) and not day_hint:
                when -= timedelta(days=1)
        elif ampm or h > 12 or h == 0:                     # unambiguous hour
            hh = _hour24(h, ampm)
            base = day_hint or now.date()
            when = datetime.combine(base, time(hh, mi))
            if when > now + timedelta(minutes=2) and not day_hint:
                when -= timedelta(days=1)
        elif part in ("morning",):
            when = datetime.combine(day_hint or now.date(), time(h % 12, mi))
        elif part in ("afternoon", "evening", "tonight"):
            when = datetime.combine(day_hint or now.date(), time(h % 12 + 12, mi))
        elif part == "last night":
            # "last night at 1" is 1 am TODAY; "last night at 11" is 11 pm yesterday.
            when = (datetime.combine(now.date(), time(h, mi)) if h <= 4
                    else datetime.combine(now.date() - timedelta(days=1), time(h % 12 + 12, mi)))
        else:
            when = _recent_past(now, h, mi, day_hint)
        return SpokenTime(_tidy(work), when, " ".join(said))

    # 3. Meal anchor: "with lunch" → typical meal time (estimated).
    m = _RE_MEAL.search(work)
    if m:
        said.append(m.group(0))
        base = day_hint or now.date()
        when = datetime.combine(base, _MEAL_TIME[m.group(1).lower()])
        if when > now and not day_hint:
            when -= timedelta(days=1)
        return SpokenTime(_tidy(_cut(work, m)), when, " ".join(said), estimated=True)

    # 4. Day word alone.
    if day_word:
        if part and part in _PART_TIME:
            base = day_hint or now.date()
            when = datetime.combine(base, _PART_TIME[part])
            if when > now:
                when = now.replace(second=0, microsecond=0)
            return SpokenTime(_tidy(work), when, " ".join(said), estimated=True)
        if yesterday:
            return SpokenTime(_tidy(work), datetime.combine(day_hint, time(12, 0)),
                              " ".join(said), estimated=True)
        # "today" / "earlier today" — no clock: now, and the day word was noise.
        return SpokenTime(_tidy(work), now.replace(second=0, microsecond=0), " ".join(said))

    return SpokenTime(_tidy(raw), now.replace(second=0, microsecond=0), None)


def say_when(when: datetime, now: datetime, estimated: bool = False) -> str:
    """Readback phrase: 'at 2:14 PM', 'yesterday at 7:00 PM', 'on Sat Sep 26 at 8:00 AM'."""
    clock = when.strftime("%-I:%M %p")
    if when.date() == now.date():
        s = f"at {clock}"
    elif when.date() == now.date() - timedelta(days=1):
        s = f"yesterday at {clock}"
    else:
        s = f"on {when.strftime('%a %b %-d')} at {clock}"
    return s + (" (estimated)" if estimated else "")
