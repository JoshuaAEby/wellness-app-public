"""Outdoor conditions via Open-Meteo + air quality via EPA AirNow.

Two jobs:
  1. A cached "current conditions" snapshot for the Today card (temp, condition,
     US AQI, UV, barometric pressure + trend).
  2. A persisted `weather_daily` row per LOCAL day, so outdoor conditions can
     become correlation inputs (AQI → respiratory rate? pressure swing → mood?).

Design notes:
  - Temps requested in °F to match the rest of the app's US units; pressure stays
    hPa (the standard for barometric comparisons).
  - Daily aggregates are computed from the day's hourly series. Fetched early in
    the day they're forecast-inclusive; the hourly refresh job re-upserts the row
    so it converges to actuals by day's end.
  - AIR QUALITY COMES FROM EPA AIRNOW (see services/airnow.py), not Open-Meteo.
    Open-Meteo's CAMS model under-reported the 2026-07-17 wildfire peak by ~2x.
    Open-Meteo remains the FALLBACK when no AirNow monitor is in range, and the
    row records which source it used in `aqi_source`.
  - AQI is accumulated from hourly OBSERVATIONS across the day (`aqi_samples` /
    `aqi_last_obs` make that restart-safe and idempotent), then overwritten the
    next day with EPA's official daily value — the authoritative number.
  - Source stays isolated here: a local HA AQI sensor could later overwrite the
    `aqi_*` fields without touching callers or the schema.
  - Never raises into callers — on any failure it logs and returns None, so the
    card just doesn't render.
"""
import logging
from datetime import date, datetime, timezone
from statistics import mean
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import WeatherDaily
from app.services import airnow
from app.timeutils import local_today

logger = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
_CACHE_TTL_SEC = 600           # 10 min — conditions don't move fast; be polite to the API

# module-level snapshot cache: {"at": datetime, "data": dict}
_snapshot_cache: dict = {}

# WMO weather codes → short human labels
_WMO = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light showers", 81: "Showers", 82: "Violent showers",
    85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "Thunderstorm + hail", 99: "Thunderstorm + hail",
}


def _aqi_category(aqi: Optional[float]) -> Optional[str]:
    """US AQI band label."""
    if aqi is None:
        return None
    if aqi <= 50:   return "Good"
    if aqi <= 100:  return "Moderate"
    if aqi <= 150:  return "Unhealthy (sensitive)"
    if aqi <= 200:  return "Unhealthy"
    if aqi <= 300:  return "Very unhealthy"
    return "Hazardous"


def _uv_category(uv: Optional[float]) -> Optional[str]:
    if uv is None:
        return None
    if uv < 3:   return "Low"
    if uv < 6:   return "Moderate"
    if uv < 8:   return "High"
    if uv < 11:  return "Very high"
    return "Extreme"


def is_configured() -> bool:
    return bool(settings.weather_lat) and bool(settings.weather_lon)


def _clean(seq) -> list:
    return [v for v in (seq or []) if v is not None]


def _local_iso_to_naive_utc(s: Optional[str]) -> Optional[datetime]:
    """Open-Meteo (timezone=auto) returns wall-clock local strings like
    '2026-07-18T06:24'. Store as naive UTC to match the rest of the DB."""
    if not s:
        return None
    try:
        local = datetime.fromisoformat(s).replace(tzinfo=ZoneInfo(settings.tz))
        return local.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


async def _fetch() -> Optional[dict]:
    """Fetch forecast + air quality. Returns the raw pair, or None on failure."""
    params = {
        "latitude": settings.weather_lat,
        "longitude": settings.weather_lon,
        "timezone": "auto",
        "forecast_days": 1,
        "temperature_unit": "fahrenheit",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            fc = await client.get(FORECAST_URL, params={
                **params,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                           "is_day,precipitation,weather_code,pressure_msl,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min,uv_index_max,"
                         "precipitation_sum,sunrise,sunset",
                "hourly": "temperature_2m,relative_humidity_2m,pressure_msl",
            })
            fc.raise_for_status()
            aq = await client.get(AIR_URL, params={
                **{k: v for k, v in params.items() if k != "temperature_unit"},
                "current": "us_aqi,pm2_5,pm10,ozone,nitrogen_dioxide,uv_index",
                "hourly": "us_aqi,pm2_5",
            })
            aq.raise_for_status()
        raw = {"forecast": fc.json(), "air": aq.json()}
    except Exception as exc:            # network/API hiccups must never break /today
        logger.warning("Weather fetch failed: %s", exc)
        return None

    # Ground-truth AQI. Independent of the block above: if AirNow is down we
    # still want temp/pressure/UV, just with CAMS-modelled AQI as fallback.
    try:
        raw["airnow"] = await airnow.get_air_quality(
            settings.weather_lat, settings.weather_lon
        )
    except Exception as exc:
        logger.warning("AirNow lookup failed: %s", exc)
        raw["airnow"] = None
    return raw


def _build_snapshot(raw: dict) -> dict:
    """Current-conditions payload for the Today card."""
    fc, aq = raw["forecast"], raw["air"]
    cur, daily = fc.get("current", {}) or {}, fc.get("daily", {}) or {}
    acur = aq.get("current", {}) or {}
    press_series = _clean(fc.get("hourly", {}).get("pressure_msl"))

    # Barometric trend: compare the most recent hourly pressure to ~3 h earlier.
    pressure = cur.get("pressure_msl")
    trend = None
    if len(press_series) >= 4 and pressure is not None:
        delta = pressure - press_series[max(0, len(press_series) - 4)]
        trend = "rising" if delta > 1.0 else "falling" if delta < -1.0 else "steady"

    def first(key):
        seq = daily.get(key) or []
        return seq[0] if seq else None

    # AQI: prefer the EPA ground monitor, fall back to Open-Meteo's CAMS model.
    an = (raw.get("airnow") or {}).get("observed")
    if an:
        aqi, aqi_label = an["aqi"], an.get("category") or _aqi_category(an["aqi"])
        aqi_source, aqi_pollutant = "airnow", an.get("pollutant")
        aqi_area = raw["airnow"].get("area")
    else:
        aqi, aqi_label = acur.get("us_aqi"), _aqi_category(acur.get("us_aqi"))
        aqi_source, aqi_pollutant, aqi_area = "open-meteo", None, None

    uv = acur.get("uv_index")
    return {
        "temp": cur.get("temperature_2m"),
        "feels_like": cur.get("apparent_temperature"),
        "humidity": cur.get("relative_humidity_2m"),
        "condition": _WMO.get(cur.get("weather_code"), "—"),
        "weather_code": cur.get("weather_code"),
        "is_day": bool(cur.get("is_day", 1)),
        "temp_max": first("temperature_2m_max"),
        "temp_min": first("temperature_2m_min"),
        "uv": uv,
        "uv_max": first("uv_index_max"),
        "uv_label": _uv_category(uv),
        "pressure": pressure,
        "pressure_trend": trend,
        "aqi": aqi,
        "aqi_label": aqi_label,
        "aqi_source": aqi_source,
        "aqi_pollutant": aqi_pollutant,
        "aqi_area": aqi_area,
        "pm25": acur.get("pm2_5"),
        "sunrise": first("sunrise"),
        "sunset": first("sunset"),
        "as_of": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
    }


async def _upsert_daily(db: AsyncSession, raw: dict, day: date) -> None:
    """Persist today's aggregates (idempotent — re-upserts as the day progresses)."""
    fc, aq = raw["forecast"], raw["air"]
    daily = fc.get("daily", {}) or {}
    fh, ah = fc.get("hourly", {}) or {}, aq.get("hourly", {}) or {}
    cur = fc.get("current", {}) or {}

    temps = _clean(fh.get("temperature_2m"))
    hums = _clean(fh.get("relative_humidity_2m"))
    press = _clean(fh.get("pressure_msl"))
    aqis = _clean(ah.get("us_aqi"))
    pm25 = _clean(ah.get("pm2_5"))

    def first(key):
        seq = daily.get(key) or []
        return seq[0] if seq else None

    row = (await db.execute(
        select(WeatherDaily).where(WeatherDaily.date == day)
    )).scalar_one_or_none()
    if row is None:
        row = WeatherDaily(date=day)
        db.add(row)

    row.temp_mean = round(mean(temps), 1) if temps else None
    row.temp_min = first("temperature_2m_min")
    row.temp_max = first("temperature_2m_max")
    row.apparent_max = cur.get("apparent_temperature")
    row.humidity_mean = round(mean(hums), 1) if hums else None
    row.pressure_mean = round(mean(press), 1) if press else None
    row.pressure_min = min(press) if press else None
    row.pressure_delta = round(max(press) - min(press), 1) if press else None
    row.uv_max = first("uv_index_max")
    row.precip_sum = first("precipitation_sum")
    row.pm25_mean = round(mean(pm25), 2) if pm25 else None
    row.condition = _WMO.get(cur.get("weather_code"))
    row.sunrise = _local_iso_to_naive_utc(first("sunrise"))
    row.sunset = _local_iso_to_naive_utc(first("sunset"))

    an = raw.get("airnow")
    observed = (an or {}).get("observed")
    if observed and observed.get("date") == day:
        # Accumulate hourly ground observations. `aqi_last_obs` makes this
        # idempotent: a scheduler double-run within the same hour, or a restart,
        # must not double-count the same reading into the running mean.
        stamp = f"{day.isoformat()}T{observed.get('time') or ''}"
        if row.aqi_last_obs != stamp:
            n = row.aqi_samples or 0
            prev = row.aqi_mean if n else None
            val = float(observed["aqi"])
            row.aqi_mean = round(((prev * n) + val) / (n + 1), 1) if prev is not None else val
            # First ground sample RESETS the max — otherwise a modelled value left
            # over from the Open-Meteo fallback would survive on an 'airnow' row
            # and silently mix provenance within one field.
            row.aqi_max = val if n == 0 else max(row.aqi_max or val, val)
            row.aqi_samples = n + 1
            row.aqi_last_obs = stamp
        row.aqi_pollutant = observed.get("pollutant")
        row.aqi_source = "airnow"
    elif not row.aqi_samples:
        # No ground monitor in range (or AirNow down) and nothing accumulated —
        # fall back to the CAMS model rather than leaving the day blank.
        row.aqi_mean = round(mean(aqis), 1) if aqis else None
        row.aqi_max = max(aqis) if aqis else None
        row.aqi_source = "open-meteo" if aqis else None

    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()

    if an and an.get("yesterday"):
        await _backfill_official(db, an["yesterday"])


async def _backfill_official(db: AsyncSession, yday: dict) -> None:
    """Overwrite a past day's AQI with EPA's OFFICIAL daily value.

    EPA publishes yesterday's daily AQI (derived from the 24 h-average
    concentration) once the day closes. That is the authoritative number and
    supersedes our running mean of hourly observations, so it lands in
    `aqi_mean` — the field the correlation engine reads.
    """
    day = yday.get("date")
    if day is None:
        return
    row = (await db.execute(
        select(WeatherDaily).where(WeatherDaily.date == day)
    )).scalar_one_or_none()
    if row is not None and row.aqi_source == "airnow-official":
        return                                  # already patched
    if row is None:
        # Day predates weather collection. Still worth a row: the official AQI is
        # a real exposure the correlation engine can use, and the other columns
        # stay NULL rather than being invented (pairwise stats tolerate gaps).
        row = WeatherDaily(date=day)
        db.add(row)
    val = float(yday["aqi"])
    row.aqi_mean = val
    row.aqi_max = max(row.aqi_max or val, val)
    row.aqi_pollutant = yday.get("pollutant")
    row.aqi_source = "airnow-official"
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()
    logger.info("Backfilled official EPA AQI for %s: %s (%s)",
                day, val, yday.get("category"))


async def refresh_weather(db: AsyncSession) -> Optional[dict]:
    """Fetch → persist today's daily row → return the current snapshot."""
    if not is_configured():
        return None
    raw = await _fetch()
    if raw is None:
        return None
    snapshot = _build_snapshot(raw)
    try:
        await _upsert_daily(db, raw, local_today())
    except Exception as exc:
        logger.error("weather_daily upsert failed: %s", exc)
        await db.rollback()
    _snapshot_cache["at"] = datetime.now(timezone.utc)
    _snapshot_cache["data"] = snapshot
    return snapshot


async def get_current(db: AsyncSession) -> Optional[dict]:
    """Cached current conditions for the card (refreshes past the TTL)."""
    if not is_configured():
        return None
    cached_at = _snapshot_cache.get("at")
    if cached_at is not None:
        age = (datetime.now(timezone.utc) - cached_at).total_seconds()
        if age < _CACHE_TTL_SEC and _snapshot_cache.get("data"):
            return _snapshot_cache["data"]
    return await refresh_weather(db)
