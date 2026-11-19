"""Time-zone awareness — phase 1: RECORD where the phone's clock was. Nothing reads it yet.

Why (2026-10-19): the whole app runs on one configured zone (settings.tz, Eastern). On a
two-week trip several hours east that put the day boundary at 07:00 local, fired the 21:00 coach push
at 04:00, judged morning light against home's sunrise, and (with an HAE quirk) filed every
night of sleep under the wrong date. The user asked for the app to follow him. Phases:

  1. (this file) record a time-zone timeline + manual overrides — no behaviour change
  2. "today" and the scheduler follow the current zone
  3. day bucketing uses the zone in effect for each sample; history rebuilt
  4. clock-based habits (morning light, bed/wake, late caffeine) judged in local time

THE EVIDENCE. Every HAE timestamp is "YYYY-MM-DD HH:MM:SS ±HHMM". The working assumption
is that HAE formats ALL of a payload's timestamps in the phone's zone AT EXPORT TIME, not
the zone each sample was taken in (HealthKit stores no zone for most samples). If so, one
webhook = one observation "the phone was at offset X at time T", and per-sample offsets
on re-sent history are meaningless. `offsets_seen` records the spread per payload so the
assumption can be checked: a payload mixing offsets would falsify it (logged at INFO).
Expect the first test of it on the next trip — at home every payload is uniform anyway.

THE RESOLVER (`tz_at`) is written now so later phases have one entry point, with a fixed
precedence: manual TzPeriod > latest observation at/before the instant > settings.tz.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import TzObservation, TzPeriod

logger = logging.getLogger(__name__)

# Timestamp fields HAE uses on metric samples, sleep summaries and workouts.
_TS_KEYS = ("date", "sleepStart", "sleepEnd", "inBedStart", "inBedEnd", "start", "end")
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ([+-])(\d{2})(\d{2})$")

# An observation older than this says nothing about "now" — fall back to settings.tz
# rather than extend a stale offset across a gap (watch off, HAE broken).
OBSERVATION_MAX_AGE = timedelta(hours=48)


def parse_offset(ts: str) -> Optional[tuple[datetime, int]]:
    """'2026-10-12 05:47:35 +0300' → (naive UTC instant, +180). None if not that shape."""
    m = _TS_RE.match(ts.strip()) if isinstance(ts, str) else None
    if not m:
        return None
    sign = 1 if m.group(2) == "+" else -1
    off = sign * (int(m.group(3)) * 60 + int(m.group(4)))
    local = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    return local - timedelta(minutes=off), off


def _iter_timestamps(metric_list: Iterable, workout_list: Iterable) -> Iterable[str]:
    for block in metric_list or []:
        for sample in (block.get("data") or []) if isinstance(block, dict) else []:
            if isinstance(sample, dict):
                for k in _TS_KEYS:
                    if isinstance(sample.get(k), str):
                        yield sample[k]
    for w in workout_list or []:
        if isinstance(w, dict):
            for k in _TS_KEYS:
                if isinstance(w.get(k), str):
                    yield w[k]


def summarize_payload(metric_list, workout_list) -> Optional[dict]:
    """Offset evidence from one webhook payload, or None if it carried no timestamps."""
    counts: Counter = Counter()
    newest: Optional[tuple[datetime, int]] = None
    for ts in _iter_timestamps(metric_list, workout_list):
        p = parse_offset(ts)
        if p is None:
            continue
        counts[p[1]] += 1
        if newest is None or p[0] > newest[0]:
            newest = p
    if newest is None:
        return None
    return {"utc_offset_min": newest[1], "latest_sample_utc": newest[0],
            "offsets_seen": dict(counts), "n_samples": sum(counts.values())}


async def record_observation(db: AsyncSession, metric_list, workout_list) -> Optional[dict]:
    """Store one TzObservation for a webhook. Never raises — ingestion must not depend on it."""
    try:
        s = summarize_payload(metric_list, workout_list)
        if s is None:
            return None
        if len(s["offsets_seen"]) > 1:
            # The interesting case: evidence about HAE's formatting semantics (see top).
            logger.info("TZ: payload mixes offsets %s (newest sample %s at %+d min)",
                        s["offsets_seen"], s["latest_sample_utc"], s["utc_offset_min"])
        db.add(TzObservation(
            observed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            utc_offset_min=s["utc_offset_min"],
            latest_sample_utc=s["latest_sample_utc"],
            offsets_seen=json.dumps({str(k): v for k, v in sorted(s["offsets_seen"].items())}),
            n_samples=s["n_samples"],
            source="hae",
        ))
        await db.commit()
        return s
    except Exception as exc:
        logger.error("TZ: recording observation failed: %s", exc)
        await db.rollback()
        return None


def zone_from_name(name: str) -> tzinfo:
    """IANA name, or a fixed offset written "+03:00" / "-0430" / "UTC+3"."""
    n = name.strip()
    m = re.fullmatch(r"(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?", n)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0)))
    try:
        return ZoneInfo(n)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown time zone: {name!r}") from exc


def home_zone() -> ZoneInfo:
    return ZoneInfo(settings.tz)


def _home_offset_min(instant_utc: datetime) -> int:
    off = instant_utc.replace(tzinfo=timezone.utc).astimezone(home_zone()).utcoffset()
    return int(off.total_seconds() // 60)


def _home_offsets_ever(instant_utc: datetime) -> set[int]:
    """Home's standard and daylight offsets."""
    y = instant_utc.year
    return {_home_offset_min(datetime(y, 1, 1)), _home_offset_min(datetime(y, 7, 1))}


def is_home_offset(offset_min: int, *instants: Optional[datetime]) -> bool:
    """Was home on this offset at any of these instants? An observation must be judged at
    its OWN moments, never just now: on 2026-11-01 the last evening's -0400 was compared
    with the new -0500, so a phone at home read as travelling until the next export
    (zone UTC-04:00, "where are you?"). Pass observed_at AND latest_sample_utc: a payload
    of old samples sent after a change still carries the offset those samples had."""
    return any(i is not None and _home_offset_min(i) == offset_min for i in instants)


def _offset_zone(offset_min: int, *instants: Optional[datetime]) -> tzinfo:
    """Fixed zone for an observed offset — but the home zone itself when it matches, so
    ordinary home observations keep DST semantics instead of a frozen offset."""
    if is_home_offset(offset_min, *instants):
        return home_zone()
    return timezone(timedelta(minutes=offset_min))


async def _period_at(db: AsyncSession, instant_utc: datetime) -> Optional[tzinfo]:
    """Manual period covering the instant. Periods are in LOCAL dates of their own zone,
    so the candidate set is widened by a day each side and checked exactly."""
    d = instant_utc.date()
    rows = (await db.execute(
        select(TzPeriod).where(TzPeriod.start_date <= d + timedelta(days=1))
        .where(TzPeriod.end_date >= d - timedelta(days=1))
        .order_by(TzPeriod.id.desc())
    )).scalars().all()
    for p in rows:
        z = zone_from_name(p.tz_name)
        local_day = instant_utc.replace(tzinfo=timezone.utc).astimezone(z).date()
        if p.start_date <= local_day <= p.end_date:
            return z
    return None


async def tz_at(db: AsyncSession, instant_utc: datetime) -> tzinfo:
    """The zone the user was in at a (naive UTC) instant. Manual > observed > home."""
    z = await _period_at(db, instant_utc)
    if z is not None:
        return z
    obs = (await db.execute(
        select(TzObservation).where(TzObservation.observed_at <= instant_utc)
        .order_by(TzObservation.observed_at.desc()).limit(1)
    )).scalar_one_or_none()
    if obs is not None and instant_utc - obs.observed_at <= OBSERVATION_MAX_AGE:
        return _offset_zone(obs.utc_offset_min, instant_utc, obs.observed_at, obs.latest_sample_utc)
    return home_zone()


async def tz_for_date(db: AsyncSession, day: date) -> tzinfo:
    """Zone for a local calendar day: a manual period naming it, else the zone at that
    day's local noon (probed in the home zone — a ±12 h error still lands on the day)."""
    period = (await db.execute(
        select(TzPeriod).where(TzPeriod.start_date <= day).where(TzPeriod.end_date >= day)
        .order_by(TzPeriod.id.desc()).limit(1)
    )).scalar_one_or_none()
    if period is not None:
        return zone_from_name(period.tz_name)
    noon = datetime.combine(day, time(12)).replace(tzinfo=home_zone())
    return await tz_at(db, noon.astimezone(timezone.utc).replace(tzinfo=None))


async def timeline(db: AsyncSession, start_utc: datetime, end_utc: datetime) -> list[dict]:
    """Observations in [start, end] collapsed into runs of equal offset, for inspection."""
    rows = (await db.execute(
        select(TzObservation).where(TzObservation.observed_at >= start_utc)
        .where(TzObservation.observed_at <= end_utc).order_by(TzObservation.observed_at)
    )).scalars().all()
    runs: list[dict] = []
    for o in rows:
        if runs and runs[-1]["utc_offset_min"] == o.utc_offset_min:
            runs[-1]["last_seen"] = o.observed_at.isoformat()
            runs[-1]["n"] += 1
            if o.offsets_seen and len(json.loads(o.offsets_seen)) > 1:
                runs[-1]["mixed_payloads"] += 1
        else:
            runs.append({"utc_offset_min": o.utc_offset_min,
                         "first_seen": o.observed_at.isoformat(),
                         "last_seen": o.observed_at.isoformat(), "n": 1,
                         "mixed_payloads": int(bool(o.offsets_seen and
                                                    len(json.loads(o.offsets_seen)) > 1))})
    return runs


def _day_pieces(day0: date, day1: date, zone: tzinfo, label: str) -> list:
    """[local midnight(d), local midnight(d+1)) in `zone` for d in day0..day1, as UTC
    pieces with the offset at the START of each day (a DST change applies from the next
    local midnight — at most an hour misplaced on the changeover day)."""
    from app.timeutils import AwaySegment
    out, d = [], day0
    while d <= day1:
        a = datetime.combine(d, time.min).replace(tzinfo=zone)
        b = datetime.combine(d + timedelta(days=1), time.min).replace(tzinfo=zone)
        off = int(a.utcoffset().total_seconds() // 60)
        out.append(AwaySegment(a.astimezone(timezone.utc).replace(tzinfo=None),
                               b.astimezone(timezone.utc).replace(tzinfo=None), off, label))
        d += timedelta(days=1)
    return out


def _merge(pieces: list) -> list:
    """Sort, drop pieces whose offset equals home's at that moment (same-zone trips need
    no special bucketing), and coalesce contiguous pieces with equal offsets."""
    from app.timeutils import AwaySegment
    home = home_zone()
    keep = []
    for p in sorted(pieces, key=lambda x: x.start):
        h = p.start.replace(tzinfo=timezone.utc).astimezone(home).utcoffset()
        if h == timedelta(minutes=p.offset_min):
            continue
        if keep and keep[-1].end == p.start and keep[-1].offset_min == p.offset_min:
            keep[-1] = AwaySegment(keep[-1].start, p.end, p.offset_min, keep[-1].label)
        elif keep and p.start < keep[-1].end:
            continue                       # overlap: the earlier (higher-precedence) wins
        else:
            keep.append(p)
    return keep


async def build_segments(db: AsyncSession) -> list:
    """Away segments for zone-aware bucketing (phase 3) — see timeutils "AWAY SEGMENTS".

    Manual tz_periods are authoritative. Observation runs (an HAE offset other than home)
    fill only days no manual period covers; their edges are snapped to local midnights of
    the observed offset — coarse, which is why the "where are you?" prompt turns them
    into manual periods. An ongoing run extends to the end of today, local.
    """
    pieces, manual_days = [], set()
    for p in (await db.execute(select(TzPeriod).order_by(TzPeriod.id.desc()))).scalars().all():
        try:
            z = zone_from_name(p.tz_name)
        except ValueError:
            logger.error("TZ: period %s has an unknown zone %r — ignored", p.id, p.tz_name)
            continue
        days = [p.start_date + timedelta(days=i)
                for i in range((p.end_date - p.start_date).days + 1)]
        days = [d for d in days if d not in manual_days]     # newest period wins
        for d in days:
            pieces.extend(_day_pieces(d, d, z, p.tz_name))
        manual_days.update(days)

    obs = (await db.execute(
        select(TzObservation.observed_at, TzObservation.utc_offset_min,
               TzObservation.latest_sample_utc)
        .order_by(TzObservation.observed_at)
    )).all()
    runs = []                                 # [offset, first_seen, last_seen, first_sample]
    for at, off, sample in obs:
        if runs and runs[-1][0] == off:
            runs[-1][2] = at
        else:
            runs.append([off, at, at, sample])
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for i, (off, first, last, sample) in enumerate(runs):
        if is_home_offset(off, first, sample):
            continue
        z = timezone(timedelta(minutes=off))
        if i == len(runs) - 1 and now - last <= OBSERVATION_MAX_AGE:
            last = now                         # still there
        d0 = (first + timedelta(minutes=off)).date()
        d1 = (last + timedelta(minutes=off)).date()
        days = [d0 + timedelta(days=k) for k in range((d1 - d0).days + 1)]
        for d in days:
            if d not in manual_days:
                pieces.extend(_day_pieces(d, d, z, f"observed {z}"))
    return _merge(pieces)


async def refresh_current_zone(db: AsyncSession) -> bool:
    """Rebuild the away segments (phase 3), then resolve the zone for NOW and install it as the process-wide current zone
    (timeutils.current_tz). If it changed, re-register the scheduler's cron jobs in the
    new zone so the 09:30 / 21:00 / … jobs fire at local time. Returns True on change.
    Never raises — a resolver failure keeps the previous zone."""
    from app.timeutils import away_segments, current_tz, set_current_tz, set_segments
    # Phase 3: the away segments behind zone-aware day bucketing are rebuilt on the same
    # cadence. Cheap — two small tables — and a no-op for bucketing while there are none.
    try:
        segs = await build_segments(db)
        if segs != away_segments():
            set_segments(segs)
            logger.warning("TZ: %d away segment(s) installed: %s", len(segs),
                           [(s.start.isoformat(), s.end.isoformat(), s.offset_min) for s in segs])
    except Exception as exc:
        logger.error("TZ: building away segments failed (keeping previous): %s", exc)
    try:
        z = await tz_at(db, datetime.now(timezone.utc).replace(tzinfo=None))
    except Exception as exc:
        logger.error("TZ: resolving current zone failed (keeping %s): %s", current_tz(), exc)
        return False
    old = current_tz()
    if not set_current_tz(z):
        return False
    logger.warning("TZ: current zone %s → %s", getattr(old, "key", old), getattr(z, "key", z))
    try:
        from app.services.scheduler import follow_zone
        follow_zone(z)
    except Exception as exc:
        logger.error("TZ: re-registering scheduler jobs failed: %s", exc)
    return True



# ── "You're traveling — where are you?" (2026-10-19's idea) ────────────────────
#
# Automatic evidence (HAE offsets) is coarse: it gives an OFFSET, not a zone (so no DST
# rules), its edges are wherever syncs happened to land, and it is absent whenever HAE
# doesn't sync. Rather than guess, ask — but only when it matters:
#
#   "offset"    — the phone is on a non-home offset and no manual period covers today
#   "mixed"     — the newest payload mixed offsets (HAE semantics in doubt) and no period
#   "no_signal" — today is an away day, no period covers it, and no observation in 24 h
#
# The answer becomes a manual TzPeriod (authoritative), backdated to the start of the
# observed run, and is extended day by day while the trip lasts (extend_open_periods).
# A domestic trip in the home zone never asks: its offset equals home's.

POPULAR_ZONES = [
    "America/New_York", "America/Chicago", "America/Denver", "America/Phoenix",
    "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu", "America/Toronto",
    "America/Halifax", "America/Mexico_City", "America/Bogota", "America/Lima",
    "America/Sao_Paulo", "Atlantic/Reykjavik", "Europe/London", "Europe/Dublin",
    "Europe/Lisbon", "Europe/Paris", "Europe/Berlin", "Europe/Rome", "Europe/Madrid",
    "Europe/Amsterdam", "Europe/Athens", "Asia/Jerusalem", "Europe/Istanbul",
    "Africa/Cairo", "Africa/Johannesburg", "Africa/Nairobi", "Europe/Moscow",
    "Asia/Riyadh", "Asia/Dubai", "Asia/Kolkata", "Asia/Bangkok", "Asia/Singapore",
    "Asia/Hong_Kong", "Asia/Shanghai", "Asia/Manila", "Asia/Seoul", "Asia/Tokyo",
    "Australia/Perth", "Australia/Sydney", "Pacific/Auckland",
]
NO_SIGNAL_HOURS = 24


def zones_with_offset(offset_min: int, at_utc: datetime, limit: int = 8,
                      popular_only: bool = False) -> list[str]:
    """IANA zones currently on `offset_min` — popular cities first, then others."""
    from zoneinfo import available_timezones
    at = at_utc.replace(tzinfo=timezone.utc)
    want = timedelta(minutes=offset_min)

    def _ok(name: str) -> bool:
        try:
            return at.astimezone(ZoneInfo(name)).utcoffset() == want
        except Exception:
            return False
    out = [z for z in POPULAR_ZONES if _ok(z)]
    if len(out) < limit and not (popular_only and out):
        extra = sorted(z for z in available_timezones()
                       if "/" in z and not z.startswith(("Etc/", "SystemV/", "US/"))
                       and z not in out and _ok(z))
        out += extra[: limit - len(out)]
    return out[:limit]


async def _period_covering(db: AsyncSession, day: date) -> Optional[TzPeriod]:
    return (await db.execute(
        select(TzPeriod).where(TzPeriod.start_date <= day).where(TzPeriod.end_date >= day)
        .order_by(TzPeriod.id.desc()).limit(1)
    )).scalar_one_or_none()


async def prompt_state(db: AsyncSession) -> dict:
    """Whether to ask "where are you?", why, and which zones to offer (+ `home`)."""
    st = await _prompt_state(db)
    if st.get("needed"):
        st["home"] = home_zone().key
    return st


async def _prompt_state(db: AsyncSession) -> dict:
    from app.services.day_flags import is_away
    from app.timeutils import local_today
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today = local_today()
    if await _period_covering(db, today) is not None:
        return {"needed": False}

    latest = (await db.execute(
        select(TzObservation).order_by(TzObservation.observed_at.desc()).limit(1)
    )).scalar_one_or_none()
    fresh = latest is not None and now - latest.observed_at <= OBSERVATION_MAX_AGE

    # Home's own two offsets in one payload is summer history re-sent in winter, not travel.
    offs = sorted(int(k) for k in json.loads(latest.offsets_seen)) \
        if fresh and latest.offsets_seen else []
    if len(offs) > 1 and not set(offs) <= _home_offsets_ever(now):
        cands = []
        for o in offs:
            cands += [z for z in zones_with_offset(o, now, 4, popular_only=True) if z not in cands]
        return {"needed": True, "reason": "mixed", "offset_min": latest.utc_offset_min,
                "offsets_seen": offs, "candidates": cands}
    if fresh and not is_home_offset(latest.utc_offset_min, now, latest.observed_at,
                                    latest.latest_sample_utc):
        return {"needed": True, "reason": "offset", "offset_min": latest.utc_offset_min,
                "candidates": zones_with_offset(latest.utc_offset_min, now)}
    recent = latest is not None and now - latest.observed_at <= timedelta(hours=NO_SIGNAL_HOURS)
    if not recent and await is_away(db, today):
        return {"needed": True, "reason": "no_signal", "offset_min": None,
                "candidates": [home_zone().key] + [z for z in POPULAR_ZONES[:12]
                                                   if z != home_zone().key]}
    return {"needed": False}


async def confirm_zone(db: AsyncSession, tz_name: str) -> dict:
    """Record the answer as a manual period from the start of the current non-home
    observation run (or today) through today, extending a matching period that ends
    yesterday rather than stacking a new one. Then refresh zone + segments."""
    from app.timeutils import local_today
    z = zone_from_name(tz_name)                 # raises ValueError on junk
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today = now.replace(tzinfo=timezone.utc).astimezone(z).date()
    start = today
    off_now = int(now.replace(tzinfo=timezone.utc).astimezone(z).utcoffset().total_seconds() // 60)
    # Backdate to where the phone started reporting this offset, if it did.
    obs = (await db.execute(
        select(TzObservation.observed_at, TzObservation.utc_offset_min)
        .order_by(TzObservation.observed_at.desc()).limit(500)
    )).all()
    first = None
    home_off_now = int(now.replace(tzinfo=timezone.utc).astimezone(home_zone())
                       .utcoffset().total_seconds() // 60)
    if off_now != home_off_now:          # never backdate an answer of "home time"
        for at, off in obs:
            if off != off_now:
                break
            first = at
    if first is not None and now - first <= timedelta(days=60):
        start = min(start, first.replace(tzinfo=timezone.utc).astimezone(z).date())

    prev = (await db.execute(
        select(TzPeriod).where(TzPeriod.tz_name == tz_name.strip())
        .where(TzPeriod.end_date >= start - timedelta(days=1)).where(TzPeriod.start_date <= today)
        .order_by(TzPeriod.id.desc()).limit(1)
    )).scalar_one_or_none()
    if prev is not None:
        prev.start_date = min(prev.start_date, start)
        prev.end_date = max(prev.end_date, today)
        period = prev
    else:
        period = TzPeriod(start_date=start, end_date=today, tz_name=tz_name.strip(),
                          source="prompt", note="answered 'where are you?'")
        db.add(period)
    await db.commit()
    await refresh_current_zone(db)
    return {"tz_name": period.tz_name, "start_date": period.start_date.isoformat(),
            "end_date": period.end_date.isoformat()}


async def extend_open_periods(db: AsyncSession) -> list:
    """Carry an answered zone forward while the trip lasts: a manual period that ended
    YESTERDAY is extended to today if today is an away day and any fresh observation
    agrees with its offset. Stops by itself on the first home day."""
    from app.services.day_flags import is_away
    from app.timeutils import local_today
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today = local_today()
    if not await is_away(db, today):
        return []
    latest = (await db.execute(
        select(TzObservation).order_by(TzObservation.observed_at.desc()).limit(1)
    )).scalar_one_or_none()
    fresh = latest is not None and now - latest.observed_at <= OBSERVATION_MAX_AGE
    out = []
    for p in (await db.execute(
        select(TzPeriod).where(TzPeriod.end_date == today - timedelta(days=1))
    )).scalars().all():
        try:
            z = zone_from_name(p.tz_name)
        except ValueError:
            continue
        off = int(now.replace(tzinfo=timezone.utc).astimezone(z).utcoffset().total_seconds() // 60)
        if fresh and latest.utc_offset_min != off:
            continue                    # the phone says we've moved on — ask instead
        p.end_date = today
        out.append(p.id)
    if out:
        await db.commit()
    return out
