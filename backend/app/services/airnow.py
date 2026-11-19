"""Ground-truth air quality from EPA AirNow.

Why this exists: Open-Meteo's air quality is CAMS, a ~40 km GLOBAL FORECAST MODEL,
not a sensor network. It tracks broad trends fine but smooths out local plumes.
On 2026-07-17 (Canadian wildfire smoke) EPA monitors recorded a daily PM2.5 AQI of
212 "Very Unhealthy" while CAMS peaked at 112 — roughly half. That error is
worst precisely on the severe days most likely to move sleep/respiratory outcomes,
i.e. it would blunt the exact signal the AQI correlation input exists to detect.

Source: the public AirNow reporting-area file, which needs no API key:
    https://files.airnowtech.org/airnow/today/reportingarea.dat

Pipe-delimited, one row per (reporting area, parameter, day). Columns:
    0 issue date | 1 valid date | 2 valid time (blank on daily rows) | 3 tz
    4 day offset (-1 yesterday, 0 today, 1+ forecast) | 5 type (Y/O/F)
    6 primary-pollutant flag | 7 city | 8 state | 9 lat | 10 lon
    11 parameter | 12 AQI | 13 category | 14 action day | 15 — | 16 agency

Two row types matter:
  - type 'O' — the latest hourly OBSERVATION for today.
  - type 'Y' — YESTERDAY's official daily AQI (EPA's 24 h-average-derived value).
    This is the authoritative daily number and backfills the prior day's row.

Overall AQI is the MAX across parameters (PM2.5 / PM10 / OZONE), per the EPA
definition — the sub-index that drives the headline number.

Never raises into callers: on any failure it returns None and the caller falls
back to Open-Meteo.
"""
import logging
from datetime import date, datetime
from math import asin, cos, radians, sin, sqrt
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

REPORTING_AREA_URL = "https://files.airnowtech.org/airnow/today/reportingarea.dat"

_CACHE_TTL_SEC = 1800       # file republishes hourly; 30 min keeps us fresh but polite
_MAX_DISTANCE_KM = 100.0    # beyond this the nearest monitor isn't representative

# module-level cache: {"at": datetime, "rows": list[list[str]]}
_cache: dict = {}


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r_lat1, r_lat2 = radians(lat1), radians(lat2)
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(r_lat1) * cos(r_lat2) * sin(dlon / 2) ** 2
    return 6371.0 * 2 * asin(sqrt(a))


async def _fetch_rows() -> Optional[list]:
    """Download + split the reporting-area file, with a short-lived cache."""
    cached_at = _cache.get("at")
    if cached_at is not None:
        age = (datetime.utcnow() - cached_at).total_seconds()
        if age < _CACHE_TTL_SEC and _cache.get("rows"):
            return _cache["rows"]

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(REPORTING_AREA_URL)
            resp.raise_for_status()
            text = resp.text
    except Exception as exc:
        logger.warning("AirNow fetch failed: %s", exc)
        return _cache.get("rows")      # stale beats nothing

    rows = []
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) >= 14:
            rows.append(parts)
    if not rows:
        logger.warning("AirNow file parsed to zero rows")
        return _cache.get("rows")

    _cache["at"], _cache["rows"] = datetime.utcnow(), rows
    return rows


def _nearest_area(rows: list, lat: float, lon: float) -> Optional[tuple]:
    """Nearest reporting area to (lat, lon). Matching is by COORDINATES, never by
    name — 'Springfield' alone is ambiguous (several states report one)."""
    best = None
    for parts in rows:
        try:
            r_lat, r_lon = float(parts[9]), float(parts[10])
        except (ValueError, IndexError):
            continue
        dist = _haversine_km(lat, lon, r_lat, r_lon)
        if best is None or dist < best[0]:
            best = (dist, parts[7], parts[8])
    if best is None or best[0] > _MAX_DISTANCE_KM:
        if best is not None:
            logger.warning("Nearest AirNow area %s, %s is %.0f km away — too far",
                           best[1], best[2], best[0])
        return None
    return best


def _aqi_of(parts: list) -> Optional[int]:
    """AQI column, or None (forecast rows sometimes carry a blank value)."""
    try:
        return int(parts[12])
    except (ValueError, IndexError):
        return None


def _pick_max(candidates: list) -> Optional[dict]:
    """Overall AQI = max sub-index across parameters."""
    best = None
    for parts in candidates:
        aqi = _aqi_of(parts)
        if aqi is None:
            continue
        if best is None or aqi > best["aqi"]:
            best = {"aqi": aqi, "pollutant": parts[11], "category": parts[13]}
    return best


async def get_air_quality(lat: float, lon: float) -> Optional[dict]:
    """Latest observed AQI + yesterday's official daily AQI for the nearest area.

    Returns None when unavailable so the caller can fall back to Open-Meteo:
        {
          "area": "Springfield, IL", "distance_km": 12.0,
          "observed": {"aqi": 52, "pollutant": "PM2.5", "category": "Moderate",
                       "time": "14:00", "date": date(2026, 7, 18)},
          "yesterday": {"aqi": 212, "pollutant": "PM2.5",
                        "category": "Very Unhealthy", "date": date(2026, 7, 17)},
        }
    """
    rows = await _fetch_rows()
    if not rows:
        return None
    area = _nearest_area(rows, lat, lon)
    if area is None:
        return None
    dist, city, state = area

    mine = [p for p in rows if p[7] == city and p[8] == state]
    observed_rows = [p for p in mine if p[5] == "O"]
    yesterday_rows = [p for p in mine if p[5] == "Y"]

    def _parse_date(s: str) -> Optional[date]:
        try:
            return datetime.strptime(s, "%m/%d/%y").date()
        except (ValueError, TypeError):
            return None

    result = {
        "area": f"{city}, {state}",
        "distance_km": round(dist, 1),
        "observed": None,
        "yesterday": None,
    }

    if observed_rows:
        # Several hours may be present; keep only the latest timestamp.
        latest_time = max(p[2] for p in observed_rows)
        latest = [p for p in observed_rows if p[2] == latest_time]
        obs = _pick_max(latest)
        if obs:
            obs["time"] = latest_time
            obs["date"] = _parse_date(latest[0][1])
            result["observed"] = obs

    if yesterday_rows:
        yday = _pick_max(yesterday_rows)
        if yday:
            yday["date"] = _parse_date(yesterday_rows[0][1])
            result["yesterday"] = yday

    if result["observed"] is None and result["yesterday"] is None:
        return None
    return result
