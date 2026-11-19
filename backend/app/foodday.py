"""The FOOD DAY: what counts as "a day" for eating and logging (2026-10-21).

The user: "if I eat something between midnight and a few hours later that should be counted
to the previous day (that just ended), not the day that will start after I wake up."
Chosen boundary: 04:00 local. A food day D runs [D 04:00, D+1 04:00).

STORAGE IS UNCHANGED: `food_log.timestamp` stays the real local wall-clock moment, so a
01:30 snack on the night of the 20th is stored as 2026-10-21 01:30. Timing-sensitive
uses (evening eating, the after-dinner experiment) keep the true time. Only the mapping
from timestamp to day changes — always through this module:

  food_day(ts)              Python: the food day of a timestamp
  food_date_sql(col)        SQL:    'YYYY-MM-DD' food day of a timestamp column
  food_day_start(d) / food_day_end(d)   half-open bounds: ts >= start AND ts < end
  food_today()              the food day in progress right now (yesterday until 04:00)
  food_timestamp(d, t)      the real moment for "clock time t on food day d"
  food_day_bounds_utc(d)    the same day as naive-UTC bounds (intake rows are UTC)
  food_day_of_utc(ts)       food day of a naive-UTC instant (alcohol)
  on_food_days_utc(col,a,b) sargable naive-UTC food-day range filter
  timeutils.local_food_date_sql(col)   SQL twin of food_day_of_utc

History is unaffected by the switch: no food entry before 2026-10-21 was timestamped
between 00:00 and 05:00, so every existing entry keeps its day.

The LOG date bar uses the same day (it defaults to yesterday until 04:00), so intake
logged from it (alcohol, water, naps) goes through food_timestamp on write and
food-day bounds on its day cards — see services/intake.py.

ALCOHOL follows the food day everywhere too (2026-10-21): streaks, the habit
calendar, correlations (SDU per day and the at-bed residual), scorecard, events, coach,
summary charts and the raw-trend chart. Water, naps and caffeine ANALYTICS still use
calendar days.
"""
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from sqlalchemy import func

FOOD_DAY_START_HOUR = 4
_START = time(FOOD_DAY_START_HOUR, 0)
_SHIFT = timedelta(hours=FOOD_DAY_START_HOUR)


def food_day(ts: datetime) -> date:
    """Food day of a naive-local timestamp."""
    return (ts - _SHIFT).date()


def food_date_sql(col):
    """SQL food day ('YYYY-MM-DD') of a naive-local timestamp column."""
    return func.date(col, f"-{FOOD_DAY_START_HOUR} hours")


def food_day_start(day: date) -> datetime:
    """Inclusive start of food day `day` (naive local)."""
    return datetime.combine(day, _START)


def food_day_end(day: date) -> datetime:
    """Exclusive end of food day `day` (naive local) — the next day's start."""
    return datetime.combine(day + timedelta(days=1), _START)


def food_today() -> date:
    """The food day in progress now: until 04:00 local, that is yesterday."""
    from app.timeutils import local_now_naive
    return food_day(local_now_naive())


def food_timestamp(day: date, t: time) -> datetime:
    """Real local moment for clock time `t` on food day `day`: before 04:00 it is the
    following calendar date (01:30 on the night of the 20th = the 21st, 01:30)."""
    return datetime.combine(day + timedelta(days=1) if t < _START else day, t)


def food_day_bounds_utc(day: date) -> tuple[datetime, datetime]:
    """[start, end) of food day `day` as naive UTC — for intake rows (health_metrics,
    stored in UTC) shown on the LOG date bar's day. Each edge is converted in the zone
    that calendar date was spent in, so it stays 04:00 local across DST and trips."""
    from app.timeutils import zone_for_date

    def utc(local: datetime) -> datetime:
        return (local.replace(tzinfo=zone_for_date(local.date()))
                .astimezone(timezone.utc).replace(tzinfo=None))
    return utc(food_day_start(day)), utc(food_day_end(day))


def food_day_of_utc(ts_utc: datetime) -> date:
    """Food day of a naive-UTC instant (alcohol and other health_metrics rows): local
    wall-clock where the user was, then the 04:00 rule. Python twin of
    timeutils.local_food_date_sql."""
    from app.timeutils import to_local
    return food_day(to_local(ts_utc).replace(tzinfo=None))


def on_food_days_utc(col, start: date, end: Optional[date] = None):
    """Sargable filter: naive-UTC `col` within food days [start, end]. Literal 19-char
    bounds (HAE rows are stored without microseconds — see timeutils.on_local_days)."""
    from sqlalchemy import and_, literal_column
    lo = food_day_bounds_utc(start)[0]
    hi = food_day_bounds_utc(end or start)[1]
    f = lambda dt: literal_column("'" + dt.strftime("%Y-%m-%d %H:%M:%S") + "'")
    return and_(col >= f(lo), col < f(hi))
