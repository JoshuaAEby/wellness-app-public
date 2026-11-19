"""Home Assistant environment-sensor bridge.

Pulls configured HA entities (bedroom temp/humidity today; CO2 etc. later) over
each night's sleep window and writes ONE aggregate health_metrics row per night
per metric (mean of numeric states, timestamped at the window end = wake time,
source "home_assistant"). From there the values ride the normal machinery:
night-inputs in the correlation engine, trend registry, ITS outcomes.

Runs as a morning-pipeline step. Each run covers the last 3 nights (catches HA
or pipeline hiccups); INSERT OR REPLACE makes re-runs idempotent. The sleep
window comes from sleep_sessions (bedtime→wake); nights without a session fall
back to 23:00–07:00 local. No-ops unless HA_URL + HA_TOKEN + HA_ENV_ENTITIES
are configured. HA's recorder only keeps ~10 days by default — this bridge is
forward-collecting, not a historical backfill.
"""
import logging
from datetime import date, datetime, time, timedelta, timezone

import httpx
from sqlalchemy import select, text

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import DayFlag, SleepSession
from app.timeutils import local_day_bounds_utc, local_today

logger = logging.getLogger(__name__)

CATCHUP_NIGHTS = 3


def _entity_map() -> dict[str, str]:
    """Parse HA_ENV_ENTITIES ("entity:metric_key,...") → {entity_id: metric_key}."""
    out: dict[str, str] = {}
    for pair in (settings.ha_env_entities or "").split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        entity, key = pair.split(":", 1)
        out[entity.strip()] = key.strip()
    return out


async def _night_window(db, wake_day: date) -> tuple[datetime, datetime]:
    """[start, end) naive-UTC bounds of the night ending on `wake_day`'s morning.
    Sleep session bounds when recorded; else 23:00 (prev local day) – 07:00."""
    s = (await db.execute(
        select(SleepSession).where(SleepSession.date == wake_day)
    )).scalar_one_or_none()
    if s is not None and s.bedtime and s.wake_time:
        return s.bedtime, s.wake_time
    prev_start, _ = local_day_bounds_utc(wake_day - timedelta(days=1))
    day_start, _ = local_day_bounds_utc(wake_day)
    return prev_start + timedelta(hours=23), day_start + timedelta(hours=7)


HOME_FRACTION_MIN = 0.5
TV_PREBED_HOURS = 2                                # window before bedtime
# Screen-lit states. `buffering` added for Chromecast. A smart TV's built-in streaming apps
# report through the same entity, which flaps off↔unavailable between sessions (both
# not-watching). HDMI inputs are unverified. See services/tv.
TV_ACTIVE_STATES = {"playing", "on", "paused", "buffering"}


# States that mean "HA does not know", not "somewhere else". See _home_fraction.
UNKNOWN_STATES = frozenset({"unknown", "unavailable", ""})
MIN_KNOWN_FRACTION = 0.5


async def _state_fraction(client: httpx.AsyncClient, entity: str,
                          start: datetime, end: datetime,
                          active_states: set[str],
                          ignore_states: frozenset = frozenset()) -> float | None:
    """Time-weighted fraction of [start, end) the entity spent in active_states.
    None when HA has no history for the window.

    `ignore_states` are removed from the denominator instead of counting as inactive,
    and if they cover more than half the window the answer is None — too little is
    known to judge. Callers opt in, because for some entities an "unavailable" state
    genuinely means inactive (the TV, below)."""
    resp = await client.get(
        f"/api/history/period/{start.replace(tzinfo=timezone.utc).isoformat()}",
        params={
            "end_time": end.replace(tzinfo=timezone.utc).isoformat(),
            "filter_entity_id": entity,
            "minimal_response": "true",
            "no_attributes": "true",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    points = data[0] if data else []
    if not points:
        return None
    total = (end - start).total_seconds()
    home = 0.0
    unknown = 0.0
    for i, p in enumerate(points):
        try:
            t0 = datetime.fromisoformat(p["last_changed"]).astimezone(timezone.utc).replace(tzinfo=None)
        except (KeyError, ValueError):
            continue
        t0 = max(t0, start)
        t1 = end
        if i + 1 < len(points):
            try:
                t1 = min(end, datetime.fromisoformat(points[i + 1]["last_changed"])
                         .astimezone(timezone.utc).replace(tzinfo=None))
            except (KeyError, ValueError):
                pass
        if t1 <= t0:
            continue
        state = p.get("state")
        if ignore_states and (state is None or state in ignore_states):
            unknown += (t1 - t0).total_seconds()
        elif state in active_states:
            home += (t1 - t0).total_seconds()
    known = total - unknown
    if total <= 0 or known < MIN_KNOWN_FRACTION * total:
        return None
    return home / known


async def _home_fraction(client: httpx.AsyncClient, entity: str,
                         start: datetime, end: datetime) -> float | None:
    """Fraction of the KNOWN part of the window a presence entity was 'home'
    (None = no history, or under half the window known — callers fail open rather
    than silently discarding data).

    `unknown`/`unavailable` are NOT "away" (2026-09-23). They used to count as
    not-home, so when person.partner went `unknown` after the 2026-09-09 HA upgrade
    (her Companion app stopped sending location) every night was written as
    partner_present = 0 — 12 rows, all false. The same arithmetic produces
    slept_away, so a tracker outage on the user's phone would have recorded him away and
    auto-flagged the days as travel."""
    return await _state_fraction(client, entity, start, end, {"home"},
                                 ignore_states=UNKNOWN_STATES)


async def _upsert_night(db, metric_type: str, wake_day: date, end: datetime,
                        value: float) -> None:
    """Write one night's aggregate, REPLACING any earlier row for the same night.

    Rows are timestamped at the window end (wake time). A night first computed from
    the 23:00–07:00 fallback and recomputed once the sleep session lands gets a
    different timestamp, so a plain upsert left two rows per night (seen on
    2026-09-14 and 09-17). Only this bridge's own rows are touched."""
    from app.services.ingestion import _upsert_metric
    lo, hi = local_day_bounds_utc(wake_day)
    await db.execute(
        text("DELETE FROM health_metrics WHERE metric_type = :m AND source = 'home_assistant' "
             "AND timestamp >= :lo AND timestamp < :hi AND timestamp != :end"),
        {"m": metric_type, "lo": lo, "hi": hi, "end": end},
    )
    await _upsert_metric(db, metric_type, end, value, None, "home_assistant")


async def _fetch_mean(client: httpx.AsyncClient, entity: str,
                      start: datetime, end: datetime) -> float | None:
    """Mean of an entity's numeric states over [start, end) via HA's history API."""
    resp = await client.get(
        f"/api/history/period/{start.replace(tzinfo=timezone.utc).isoformat()}",
        params={
            "end_time": end.replace(tzinfo=timezone.utc).isoformat(),
            "filter_entity_id": entity,
            "minimal_response": "true",
            "no_attributes": "true",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    vals: list[float] = []
    for series in data or []:
        for point in series:
            try:
                vals.append(float(point["state"]))
            except (KeyError, TypeError, ValueError):
                continue  # "unavailable"/"unknown" states
    return sum(vals) / len(vals) if vals else None


async def _sync_travel_flag(db, day: date, away: bool) -> None:
    """Keep an auto `travel` DayFlag in sync with the away/home verdict for `day`.

    Only touches AUTO travel flags — never deletes a user's manual flag. Idempotent.
    A user who deletes an auto travel flag inside the catch-up window may see it
    re-created on the next run; that's an accepted edge (old overrides are outside
    the window and safe).
    """
    existing = (await db.execute(
        select(DayFlag).where(DayFlag.date == day).where(DayFlag.reason == "travel")
    )).scalar_one_or_none()
    if away and existing is None:
        db.add(DayFlag(date=day, reason="travel", source="auto",
                       note="auto: slept away from home"))
        await db.commit()
    elif not away and existing is not None and (existing.source or "manual") == "auto":
        await db.delete(existing)          # home night → retract a stale auto flag
        await db.commit()


async def ingest_env_sensors() -> dict:
    """Aggregate configured HA entities over the last CATCHUP_NIGHTS nights."""
    entities = _entity_map()
    if not (settings.ha_url and settings.ha_token and entities):
        return {"status": "disabled"}

    from app.services.ingestion import _upsert_metric

    today = local_today()
    written = 0
    async with httpx.AsyncClient(
        base_url=settings.ha_url.rstrip("/"),
        headers={"Authorization": f"Bearer {settings.ha_token}"},
        timeout=20,
    ) as client:
        async with AsyncSessionLocal() as db:
            for i in range(CATCHUP_NIGHTS):
                wake_day = today - timedelta(days=i)
                start, end = await _night_window(db, wake_day)

                # Presence gate: skip env metrics for nights not slept at home
                # (bedroom conditions are meaningless when the bed is empty — e.g.
                # travel + eco thermostat would poison the environment→sleep pairs).
                # The home-fraction itself is kept as a free binary night-input:
                # slept_away (1 = away) — written for every judgeable night.
                if settings.ha_presence_entity:
                    try:
                        frac = await _home_fraction(
                            client, settings.ha_presence_entity, start, end)
                        if frac is not None:
                            away = frac < HOME_FRACTION_MIN
                            await _upsert_night(db, "slept_away", wake_day, end,
                                                1.0 if away else 0.0)
                            written += 1
                            # Materialise the away night as a `travel` DayFlag so it
                            # excludes the day from experiments + correlations and is
                            # visible/overridable in the UI. Only within CATCHUP_NIGHTS,
                            # so a user's manual override of an OLD travel flag is safe.
                            await _sync_travel_flag(db, wake_day, away)
                            if away:
                                logger.info("Env bridge: %s away night (home %.0f%%) — "
                                            "slept_away=1, env metrics skipped",
                                            wake_day, frac * 100)
                                continue
                            # Home night → partner presence over the same window
                            # (meaningless on away nights, so nested here)
                            if settings.ha_partner_entity:
                                pfrac = await _home_fraction(
                                    client, settings.ha_partner_entity, start, end)
                                if pfrac is not None:
                                    await _upsert_night(
                                        db, "partner_present", wake_day, end,
                                        1.0 if pfrac >= HOME_FRACTION_MIN else 0.0)
                                    written += 1
                            # Home night → TV minutes in the 2h before bedtime
                            # (bedtime-relative; `start` here IS bedtime when a
                            # sleep session exists — the only case worth scoring)
                            from app.services import tv
                            if settings.ha_tv_entity and wake_day >= tv.TV_CONFIRMED_WAKE_FROM:
                                # Only confirmed viewing counts; an outage raises, so it
                                # is not read as a night without TV.
                                await tv.collect_sessions(db, client)
                                await tv.refresh_prebed(db, wake_day)
                            elif settings.ha_tv_entity:
                                tv_frac = await _state_fraction(
                                    client, settings.ha_tv_entity,
                                    start - timedelta(hours=TV_PREBED_HOURS), start,
                                    TV_ACTIVE_STATES)
                                if tv_frac is not None:
                                    await _upsert_night(
                                        db, "tv_prebed_min", wake_day, end,
                                        round(tv_frac * TV_PREBED_HOURS * 60, 1))
                                    written += 1
                    except Exception as exc:
                        logger.warning("Env bridge: presence check failed for %s: %s "
                                       "(failing open)", wake_day, exc)

                for entity, metric_key in entities.items():
                    try:
                        mean = await _fetch_mean(client, entity, start, end)
                        if mean is None:
                            continue
                        await _upsert_night(db, metric_key, wake_day, end, round(mean, 2))
                        written += 1
                    except Exception as exc:
                        logger.warning("Env bridge: %s for %s failed: %s",
                                       entity, wake_day, exc)
            # WFH days: weekday 9:00–17:00 home fraction (person.me). Weekends
            # are skipped (a Saturday at home isn't "working from home" — mixing
            # them in would conflate the weekend effect with the WFH effect).
            if settings.ha_presence_entity:
                for i in range(CATCHUP_NIGHTS):
                    day = today - timedelta(days=i)
                    if day.weekday() >= 5:
                        continue
                    day_start, _next = local_day_bounds_utc(day)
                    w_start = day_start + timedelta(hours=9)
                    w_end = day_start + timedelta(hours=17)
                    if w_end > datetime.utcnow():
                        continue  # workday not over yet — judge it tomorrow
                    try:
                        frac = await _home_fraction(
                            client, settings.ha_presence_entity, w_start, w_end)
                        if frac is not None:
                            await _upsert_metric(db, "wfh_day", w_end,
                                                 1.0 if frac >= 0.6 else 0.0,
                                                 None, "home_assistant")
                            written += 1
                    except Exception as exc:
                        logger.warning("Env bridge: wfh check failed for %s: %s", day, exc)

            await db.commit()

    logger.info("Env bridge: wrote %d night-aggregates", written)
    return {"status": "ok", "written": written}
