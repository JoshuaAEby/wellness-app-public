"""TV sessions from Home Assistant, and whose they were (note 85, 2026-11-01).

The TV's `media_player` entity reports the app in use (e.g. a streaming app), with no
titles. It cannot say who is watching: another household member uses this TV too. So sessions are
saved here (HA keeps ~10 days) and a Today card asks "Me / Family / Other".

tv_prebed_min, from wake date TV_CONFIRMED_WAKE_FROM: confirmed minutes in the 2 h before
bedtime. A night with an unanswered session has NO value: unknown, never zero. Earlier rows
counted anyone's viewing, so they stay stored but correlations do not read them.
"""
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import SleepSession, TvSession
from app.timeutils import local_day_bounds_utc, local_today, to_local, to_local_date

logger = logging.getLogger(__name__)

ACTIVE_STATES = {"playing", "on", "paused", "buffering"}
HISTORY_DAYS = 10           # all HA keeps
GAP_MIN = 10                # shorter pauses, ad breaks and flaps are one session
MIN_SESSION_MIN = 5
PAUSE_CAP_MIN = 5           # a pause counts this long; past it he has probably walked away
PREBED_HOURS = 2
ASK_FROM = date(2026, 10, 31)
ASK_DAYS = 7                # older unanswered sessions stay unknown, and stop asking
TV_CONFIRMED_WAKE_FROM = date(2026, 11, 1)
WATCHERS = ("me", "family", "other")


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(timezone.utc).replace(tzinfo=None)


def build_sessions(points: list[dict], window_start: datetime, now: datetime) -> list[dict]:
    """HA history rows → finished sessions. Left out: a session cut by the window's start
    (its real start is unknown and would re-key it on every run), one still running, and
    one that ended under GAP_MIN ago (it could still merge with the next)."""
    spans = []
    for i, p in enumerate(points):
        if p.get("state") not in ACTIVE_STATES:
            continue
        t0 = _utc(p["last_changed"])
        open_end = i + 1 >= len(points)
        t1 = now if open_end else _utc(points[i + 1]["last_changed"])
        if p["state"] == "paused" and t1 - t0 > timedelta(minutes=PAUSE_CAP_MIN):
            t1, open_end = t0 + timedelta(minutes=PAUSE_CAP_MIN), False
        if t1 > t0:
            spans.append({"start": t0, "end": t1, "open": open_end,
                          "app": (p.get("attributes") or {}).get("app_name") or "Unknown"})
    groups: list[list[dict]] = []
    for s in spans:
        if groups and (s["start"] - groups[-1][-1]["end"]).total_seconds() < GAP_MIN * 60:
            groups[-1].append(s)
        else:
            groups.append([s])
    out = []
    for g in groups:
        start, end = g[0]["start"], g[-1]["end"]
        minutes = sum((s["end"] - s["start"]).total_seconds() for s in g) / 60
        if (minutes < MIN_SESSION_MIN or g[-1]["open"]
                or start < window_start + timedelta(minutes=1)
                or now - end < timedelta(minutes=GAP_MIN)):
            continue
        segments = []
        for s in g:
            if segments and segments[-1][2] == s["app"] and segments[-1][1] == s["start"].isoformat():
                segments[-1][1] = s["end"].isoformat()
            else:
                segments.append([s["start"].isoformat(), s["end"].isoformat(), s["app"]])
        out.append({"start": start, "end": end, "minutes": round(minutes, 1), "segments": segments})
    return out


async def collect_sessions(db: AsyncSession, client: httpx.AsyncClient | None = None) -> int:
    """Save finished sessions; an answer already given is kept. Raises if HA fails, so a
    caller cannot read an outage as a night without TV."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    start = now - timedelta(days=HISTORY_DAYS)
    params = {"end_time": now.replace(tzinfo=timezone.utc).isoformat(),
              "filter_entity_id": settings.ha_tv_entity}
    path = f"/api/history/period/{start.replace(tzinfo=timezone.utc).isoformat()}"

    async def fetch(c):
        r = await c.get(path, params=params)
        r.raise_for_status()
        return r.json()
    if client is not None:
        data = await fetch(client)
    else:
        async with httpx.AsyncClient(base_url=settings.ha_url.rstrip("/"), timeout=30,
                                     headers={"Authorization": f"Bearer {settings.ha_token}"}) as c:
            data = await fetch(c)
    sessions = build_sessions(data[0] if data else [], start, now)
    have = {r.start_time: r for r in (await db.execute(
        select(TvSession).where(TvSession.start_time >= start))).scalars()}
    # A rule change can re-cut a session; its old unanswered row must not linger on the card.
    keys = {s["start"] for s in sessions}
    for t, r in have.items():
        if t not in keys and r.mine is None and t >= start + timedelta(minutes=1):
            await db.delete(r)
    for s in sessions:
        row = have.get(s["start"]) or TvSession(start_time=s["start"], date=to_local_date(s["start"]))
        row.end_time, row.minutes, row.segments = s["end"], s["minutes"], s["segments"]
        db.add(row)
    await db.commit()
    return len(sessions)


def app_minutes(segments: list) -> dict:
    out: dict = defaultdict(float)
    for a, b, app in segments:
        out[app] += (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60
    # Apps flicker past on launch; under a minute is not viewing (segments keep them).
    return {k: round(v) for k, v in sorted(out.items(), key=lambda kv: -kv[1]) if v >= 1}


def serialize(r: TvSession) -> dict:
    return {"id": r.id, "date": r.date.isoformat(), "start": to_local(r.start_time).strftime("%H:%M"),
            "minutes": round(r.minutes), "apps": app_minutes(r.segments), "watcher": r.watcher}


async def pending(db: AsyncSession) -> list[dict]:
    since = max(ASK_FROM, local_today() - timedelta(days=ASK_DAYS - 1))
    rows = (await db.execute(
        select(TvSession).where(TvSession.mine.is_(None)).where(TvSession.date >= since)
        .order_by(TvSession.start_time)
    )).scalars().all()
    return [serialize(r) for r in rows]


async def range_by_watcher(db: AsyncSession, start: date, end: date) -> dict:
    """Exposures' TV block: minutes per day for each watcher, answered sessions only."""
    rows = (await db.execute(
        select(TvSession).where(TvSession.date >= max(start, ASK_FROM)).where(TvSession.date <= end)
    )).scalars().all()
    days: dict = defaultdict(lambda: {w: 0.0 for w in WATCHERS})
    apps: dict = {w: defaultdict(float) for w in WATCHERS}
    unanswered = 0.0
    for r in rows:
        if r.watcher is None:
            unanswered += r.minutes
            continue
        days[r.date][r.watcher] += r.minutes
        for app, m in app_minutes(r.segments).items():
            apps[r.watcher][app] += m
    return {"days": [{"date": d.isoformat(), **{w: round(v[w], 1) for w in WATCHERS}}
                     for d, v in sorted(days.items())],
            "apps": {w: {k: round(v) for k, v in sorted(a.items(), key=lambda kv: -kv[1])}
                     for w, a in apps.items()},
            "unanswered_min": round(unanswered)}


def overlap_min(row: TvSession, lo: datetime, hi: datetime) -> float:
    """Minutes of the row's segments inside [lo, hi), which leaves out pauses between them."""
    total = 0.0
    for a, b, _ in row.segments:
        s, e = max(datetime.fromisoformat(a), lo), min(datetime.fromisoformat(b), hi)
        if e > s:
            total += (e - s).total_seconds() / 60
    return total


async def refresh_prebed(db: AsyncSession, wake_day: date) -> Optional[float]:
    """Rewrite one night's tv_prebed_min from the answers. None = unknown, and the row goes."""
    from app.services.env_sensors import _upsert_night
    if wake_day < TV_CONFIRMED_WAKE_FROM:
        return None
    s = (await db.execute(select(SleepSession).where(SleepSession.date == wake_day))
         ).scalar_one_or_none()
    if s is None or not s.bedtime or not s.wake_time:
        return None
    hi, lo = s.bedtime, s.bedtime - timedelta(hours=PREBED_HOURS)
    rows = (await db.execute(
        select(TvSession).where(TvSession.end_time > lo).where(TvSession.start_time < hi)
    )).scalars().all()
    rows = [r for r in rows if overlap_min(r, lo, hi) > 0]
    if any(r.mine is None for r in rows):
        day_lo, day_hi = local_day_bounds_utc(wake_day)
        await db.execute(
            text("DELETE FROM health_metrics WHERE metric_type = 'tv_prebed_min' "
                 "AND source = 'home_assistant' AND timestamp >= :lo AND timestamp < :hi"),
            {"lo": day_lo, "hi": day_hi})
        await db.commit()
        return None
    value = round(sum(overlap_min(r, lo, hi) for r in rows if r.mine), 1)
    await _upsert_night(db, "tv_prebed_min", wake_day, s.wake_time, value)
    await db.commit()
    return value
