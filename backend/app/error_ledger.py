"""Error ledger — every ERROR-level log line, grouped and counted (2026-10-21).

This app's recurring failure mode is the SILENT one: 93 broad `except Exception` handlers,
most of which log and carry on with a default. The briefing's findings block sat empty for
a day, the caffeine-at-bedtime input was 0.0 for weeks, sleep was filed a day early for 19
days — each logged somewhere, none seen. A log line nobody reads is the same as none.

So a logging handler aggregates every ERROR record in memory by (logger, message
TEMPLATE) — "Daily summary: Claude API failed: %s", not each formatted instance — and a
scheduler job flushes the counts per day into `error_events`. Data Health shows the last
7 days; a daily push fires only when a KIND of error appears that wasn't seen in the
previous 30 days. Zero call-site changes: the 80 handlers that already log are covered.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from datetime import date, datetime, timedelta

_LOCK = threading.Lock()
_PENDING: dict = {}            # (key, day) -> {logger, template, sample, count, first, last}


def _key(logger_name: str, template: str) -> str:
    return hashlib.sha1(f"{logger_name}|{template}".encode()).hexdigest()[:16]


class LedgerHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            template = str(record.msg)[:300]
            k = _key(record.name, template)
            day = date.today().isoformat()          # server day; buckets only
            try:
                sample = record.getMessage()[:500]
            except Exception:
                sample = template
            now = datetime.utcnow()
            with _LOCK:
                e = _PENDING.get((k, day))
                if e is None:
                    _PENDING[(k, day)] = {"logger": record.name, "template": template,
                                          "sample": sample, "count": 1, "first": now, "last": now}
                else:
                    e["count"] += 1
                    e["last"], e["sample"] = now, sample
        except Exception:
            pass                                   # a logging handler must never raise


def install() -> None:
    root = logging.getLogger()
    if not any(isinstance(h, LedgerHandler) for h in root.handlers):
        h = LedgerHandler(level=logging.ERROR)
        root.addHandler(h)


async def flush(db) -> int:
    """Write pending counts to error_events (upsert per key+day). Returns rows touched."""
    from sqlalchemy import text
    with _LOCK:
        items = list(_PENDING.items())
        _PENDING.clear()
    for (k, day), e in items:
        await db.execute(text(
            "INSERT INTO error_events (key, day, logger, template, sample, count, first_at, last_at) "
            "VALUES (:k, :d, :l, :t, :s, :c, :f, :la) "
            "ON CONFLICT(key, day) DO UPDATE SET count = count + excluded.count, "
            "sample = excluded.sample, last_at = excluded.last_at"),
            {"k": k, "d": day, "l": e["logger"], "t": e["template"], "s": e["sample"],
             "c": e["count"], "f": e["first"], "la": e["last"]})
    if items:
        await db.commit()
    return len(items)


async def summary(db, days: int = 7) -> dict:
    """Error kinds seen in the last `days`, most recent first, with totals."""
    from sqlalchemy import text
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    rows = (await db.execute(text(
        "SELECT key, logger, template, MAX(sample) AS sample, SUM(count) AS n, "
        "MIN(first_at) AS first_at, MAX(last_at) AS last_at, "
        "(SELECT MIN(day) FROM error_events e2 WHERE e2.key = e.key) AS first_day "
        "FROM error_events e WHERE day >= :since GROUP BY key ORDER BY last_at DESC"),
        {"since": since})).all()
    return {"days": days, "kinds": len(rows), "total": int(sum(r.n for r in rows)),
            "errors": [{"logger": r.logger.replace("app.", ""), "template": r.template,
                        "sample": r.sample, "count": int(r.n),
                        "last_at": str(r.last_at), "first_day": r.first_day} for r in rows[:20]]}


async def new_kinds(db, hours: int = 24, quiet_days: int = 30) -> list:
    """Kinds whose FIRST appearance in the last `quiet_days` falls in the last `hours`."""
    from sqlalchemy import text
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    window = (date.today() - timedelta(days=quiet_days)).isoformat()
    rows = (await db.execute(text(
        "SELECT logger, template, MAX(sample) AS sample, MIN(first_at) AS first_at "
        "FROM error_events WHERE day >= :w GROUP BY key HAVING MIN(first_at) >= :c"),
        {"w": window, "c": cutoff})).all()
    return [{"logger": r.logger, "template": r.template, "sample": r.sample} for r in rows]
