"""Timezone helpers.

Health metrics, workouts and sleep timestamps are stored as **naive UTC**
(see ingestion._parse_date). "Today" for the user, however, is a calendar day in
the configured local timezone (settings.tz). Building day-boundary datetimes from
a local date without converting to UTC mis-aligns the window by the UTC offset —
e.g. in America/New_York the activity rings would roll over at ~20:00 local.

These helpers compute the correct naive-UTC bounds for a local calendar day so
range queries against the stored (naive UTC) timestamps line up with the local day.
"""
import bisect
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import NamedTuple, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import Integer, and_, case, func, literal_column

from app.config import settings

logger = logging.getLogger(__name__)


# ── The CURRENT zone (time-zone awareness, phase 2, 2026-10-19) ─────────────────────
#
# "Now" and "today" follow the zone the user is actually in — resolved by
# services/tz.tz_at (manual period > recent HAE observation > settings.tz) and cached
# here so these helpers stay synchronous for their ~100 callers. Refreshed on startup,
# after every webhook, every 15 minutes, and whenever a tz period changes
# (services/tz.refresh_current_zone). At home it is settings.tz, so nothing moves.
#
# NOT yet zone-aware (phase 3): day BUCKETING — local_day_bounds_utc, local_date_sql,
# to_local_date — still use settings.tz / the process TZ. Abroad, "today" is the local
# date but its bounds are the home zone's, until phase 3 lands.
_current_tz = None


def current_tz():
    """The zone the user is in now (cached; settings.tz until first refresh)."""
    return _current_tz or ZoneInfo(settings.tz)


def set_current_tz(tz) -> bool:
    """Install a newly resolved zone. Returns True if it changed."""
    global _current_tz
    old = current_tz()
    _current_tz = tz
    return _zone_key(old) != _zone_key(tz)


def _zone_key(tz) -> str:
    return getattr(tz, "key", None) or str(tz)


def local_now() -> datetime:
    """Aware 'now' in the user's current zone."""
    return datetime.now(current_tz())


def local_now_naive() -> datetime:
    """Wall-clock 'now' in the user's current zone, tzinfo stripped."""
    return local_now().replace(tzinfo=None)


def local_today() -> date:
    """Current calendar date where the user is (current_tz)."""
    return local_now().date()


# ── AWAY SEGMENTS: zone-aware day bucketing (time-zone awareness, phase 3) ─────────
#
# Every "which local day is this instant" answer — Python (to_local_date,
# local_day_bounds_utc) and SQL (local_date_sql & co.) — comes from ONE mapping:
#
#   instant inside an away segment  →  wall clock = UTC + that segment's fixed offset
#   anywhere else                   →  the home zone (settings.tz / process 'localtime')
#
# Segments are spans where the user was in a zone whose offset differs from home,
# built by services/tz.build_segments from manual tz_periods (+ observation runs) and
# installed here with set_segments(). Their edges sit on MIDNIGHTS of the away zone,
# which keeps local dates monotonic across a zone change: flying east the departure day
# comes out short, flying west the arrival day comes out long, and no instant is ever
# counted on two days or none.
#
# FAST PATH (2026-10-19: "default Eastern behavior should be quick as before"):
# with no segments every helper returns exactly the pre-phase-3 expression; with
# segments, only the few relevant to a call add a range check.

class AwaySegment(NamedTuple):
    start: datetime      # naive UTC, inclusive
    end: datetime        # naive UTC, exclusive
    offset_min: int      # fixed UTC offset in effect throughout
    label: str           # zone name, for logs/inspection


_segments: list = []
_seg_starts: list = []


def set_segments(segs: list) -> None:
    """Install the away segments (sorted, non-overlapping). See services/tz."""
    global _segments, _seg_starts
    _segments = sorted(segs, key=lambda s: s.start)
    _seg_starts = [s.start for s in _segments]


def away_segments() -> list:
    return list(_segments)


def _segment_at(naive_utc: datetime) -> Optional[AwaySegment]:
    if not _segments:
        return None
    i = bisect.bisect_right(_seg_starts, naive_utc) - 1
    if i >= 0 and naive_utc < _segments[i].end:
        return _segments[i]
    return None


def in_away_segment(naive_utc: datetime) -> bool:
    """True if the instant falls in an away segment (a non-home zone)."""
    return _segment_at(naive_utc) is not None


def to_local_date(naive_utc: datetime) -> date:
    """Local calendar date of a stored naive-UTC timestamp — where the user was."""
    seg = _segment_at(naive_utc)
    if seg is not None:
        return (naive_utc + timedelta(minutes=seg.offset_min)).date()
    return (
        naive_utc.replace(tzinfo=timezone.utc)
        .astimezone(ZoneInfo(settings.tz))
        .date()
    )


def to_local(naive_utc: datetime) -> datetime:
    """Aware local wall-clock time of a stored naive-UTC timestamp, in the zone the user
    was in at that instant (an away segment's fixed offset, else the home zone)."""
    seg = _segment_at(naive_utc)
    aware = naive_utc.replace(tzinfo=timezone.utc)
    if seg is not None:
        return aware.astimezone(timezone(timedelta(minutes=seg.offset_min)))
    return aware.astimezone(ZoneInfo(settings.tz))


def zone_for_date(day: date):
    """Zone of a local calendar day: the away segment covering that day's midday, else
    home. For turning a clock time typed for a past day ("08:00 on the 12th") into UTC."""
    noon = (datetime.combine(day, time(12)).replace(tzinfo=ZoneInfo(settings.tz))
            .astimezone(timezone.utc).replace(tzinfo=None))
    seg = _segment_at(noon)
    return timezone(timedelta(minutes=seg.offset_min)) if seg else ZoneInfo(settings.tz)


def _seg_case(col, fn):
    """CASE over the away segments, `fn(modifier)` building each branch; the ELSE is the
    home 'localtime' expression. Returns the plain home expression when there are none.
    Bounds are inlined as 'YYYY-MM-DD HH:MM:SS' literals: stored timestamps sometimes
    lack microseconds, and a bound parameter formatted with them would misorder rows
    sitting exactly on an edge."""
    home = fn("localtime")
    if not _segments:
        return home
    whens = []
    for sg in _segments:
        lo = literal_column("'" + sg.start.strftime("%Y-%m-%d %H:%M:%S") + "'")
        hi = literal_column("'" + sg.end.strftime("%Y-%m-%d %H:%M:%S") + "'")
        whens.append((and_(col >= lo, col < hi), fn(f"{sg.offset_min:+d} minutes")))
    return case(*whens, else_=home)


def local_date_sql(col):
    """
    SQL expression bucketing a naive-UTC timestamp column into the LOCAL calendar
    day (returned as a 'YYYY-MM-DD' string, same as plain func.date).

    Uses SQLite's 'localtime' modifier, which converts via the *process* timezone —
    the container's TZ env var must equal settings.tz (checked at startup by
    check_process_tz). Do NOT apply to FoodLog.timestamp: food entries are stored
    at local noon already, so plain func.date() is their local day.

    Zone-aware (phase 3): rows inside an away segment bucket by that segment's offset.
    """
    return _seg_case(col, lambda mod: func.date(col, mod))


def local_food_date_sql(col):
    """local_date_sql with the FOOD-DAY boundary (04:00, app/foodday.py) for a naive-UTC
    column: local wall-clock minus 4 h, then the date. For UTC-stored rows that follow
    the food day — alcohol (2026-10-21: a 00:30 drink belongs to the night it was drunk).
    Same away-segment handling as local_date_sql; SQLite applies the modifiers in order."""
    from app.foodday import FOOD_DAY_START_HOUR
    shift = f"-{FOOD_DAY_START_HOUR} hours"
    return _seg_case(col, lambda mod: func.date(col, mod, shift))


def local_hour_sql(col):
    """SQL LOCAL hour-of-day (0-23, as an integer) for a naive-UTC timestamp column.

    Same 'localtime' mechanism as local_date_sql, so it carries the same caveat: do NOT
    use on FoodLog.timestamp, which is already local.
    """
    return func.cast(_seg_case(col, lambda mod: func.strftime("%H", col, mod)), Integer)


def local_month_sql(col):
    """SQL 'YYYY-MM' local-month bucket for a naive-UTC timestamp column."""
    return _seg_case(col, lambda mod: func.strftime("%Y-%m", col, mod))


def on_local_days(col, start: date, end: Optional[date] = None):
    """Sargable `start <= local_date_sql(col) <= end` (end defaults to start).

    Filtering on `local_date_sql(col) == day` wraps the column in a function, so SQLite
    cannot use the (metric_type, timestamp) index and scans every row of the metric —
    the bulk of /today's SQL time (2026-10-21). This is the same local-day mapping as a
    UTC range off `local_day_bounds_utc`, which the index serves directly. Bounds are
    inlined as 'YYYY-MM-DD HH:MM:SS' literals for the same reason as `_seg_case`.
    """
    lo = local_day_bounds_utc(start)[0]
    hi = local_day_bounds_utc(end or start)[1]
    return and_(col >= literal_column("'" + lo.strftime("%Y-%m-%d %H:%M:%S") + "'"),
                col < literal_column("'" + hi.strftime("%Y-%m-%d %H:%M:%S") + "'"))


def check_process_tz() -> None:
    """
    Warn if the process timezone disagrees with settings.tz — local_date_sql
    relies on SQLite's 'localtime', which uses the process TZ, not settings.tz.
    """
    now = datetime.now()
    proc_offset = now.astimezone().utcoffset()
    cfg_offset = now.astimezone(ZoneInfo(settings.tz)).utcoffset()
    if proc_offset != cfg_offset:
        logger.warning(
            "Process timezone offset %s != settings.tz (%s) offset %s — "
            "SQL local-day bucketing (local_date_sql) will be wrong. "
            "Set the TZ env var to %s.",
            proc_offset, settings.tz, cfg_offset, settings.tz,
        )


def local_day_bounds_utc(day: date) -> tuple[datetime, datetime]:
    """
    Return the half-open [start, end) bounds of a local calendar `day`, expressed
    as naive UTC datetimes (matching how timestamps are stored).
    Use as:  ts >= start AND ts < end.
    """
    tz = ZoneInfo(settings.tz)
    start_local = datetime.combine(day, time.min).replace(tzinfo=tz)
    end_local = start_local + timedelta(days=1)

    def _to_naive_utc(d: datetime) -> datetime:
        return d.astimezone(timezone.utc).replace(tzinfo=None)

    home = (_to_naive_utc(start_local), _to_naive_utc(end_local))
    if not _segments:
        return home
    # Fast path: no away segment within two days of this day → the home bounds.
    lo, hi = home[0] - timedelta(days=2), home[1] + timedelta(days=2)
    near = [sg for sg in _segments if sg.start < hi and sg.end > lo]
    if not near:
        return home
    return _day_start(day, near), _day_start(day + timedelta(days=1), near)


def _day_start(day: date, near: list) -> datetime:
    """Earliest instant whose local date (to_local_date) is >= `day`.

    The mapping is monotonic and only jumps at midnights of the zone in force or at a
    segment edge, so the answer is the smallest such candidate that maps to >= day."""
    home_mid = (datetime.combine(day, time.min).replace(tzinfo=ZoneInfo(settings.tz))
                .astimezone(timezone.utc).replace(tzinfo=None))
    cands = {home_mid}
    for sg in near:
        cands.update((sg.start, sg.end,
                      datetime.combine(day, time.min) - timedelta(minutes=sg.offset_min)))
    return min(t for t in cands if to_local_date(t) >= day)

