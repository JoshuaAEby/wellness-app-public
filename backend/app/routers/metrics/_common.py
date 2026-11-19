"""Small helpers shared by the /api/metrics modules.

Part of the /api/metrics package (split out of routers/metrics.py 2026-10-21)."""
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DailyMetric, SleepSession, SubjectiveLog
from app.timeutils import to_local


def _group_stats(values: list[float]) -> dict:
    """
    Descriptive mean + naive 95% CI for a group of values.
    CI = mean ± 1.96 * sd / sqrt(n).

    For day-of-week groupings, same-day samples are spaced 7 days apart
    (largely de-correlated at weekly lag, so the naive CI is less wrong
    than for adjacent-day stats) — but still exploratory only; no
    significance claims should be made from these numbers.
    """
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": None, "ci_low": None, "ci_high": None}
    if n == 1:
        return {"n": 1, "mean": round(values[0], 2), "ci_low": None, "ci_high": None}
    m = sum(values) / n
    var = sum((v - m) ** 2 for v in values) / (n - 1)
    ci_half = 1.96 * (var ** 0.5) / (n ** 0.5)
    return {
        "n": n,
        "mean": round(m, 2),
        "ci_low": round(m - ci_half, 2),
        "ci_high": round(m + ci_half, 2),
    }


def _serialize_daily(row: DailyMetric) -> dict:
    return {
        "date": row.date.isoformat(),
        "daily_load": row.daily_load,
        "atl": row.atl,
        "ctl": row.ctl,
        "acwr": row.acwr,
        "hrv_value": row.hrv_value,
        "hrv_baseline": row.hrv_baseline,
        "hrv_z_score": row.hrv_z_score,
        "rhr_value": row.rhr_value,
        "rhr_baseline": row.rhr_baseline,
        "rhr_z_score": row.rhr_z_score,
        "sleep_debt_hours": row.sleep_debt_hours,
        "wrist_temp_deviation": row.wrist_temp_deviation,
        "readiness_score": row.readiness_score,
        "readiness_label": row.readiness_label,
        "ai_summary": row.ai_summary,
        "computed_at": row.computed_at.isoformat() if row.computed_at else None,
    }


def _offset_min(ts):
    """Minutes east of UTC at a stored naive-UTC instant, in the zone the user was in."""
    return int(to_local(ts).utcoffset().total_seconds() // 60) if ts else None


# ── Task C: Day-of-week contrast ──────────────────────────────────────────────

_DOW_OUTCOME_LABELS = {
    "readiness_score": "Recovery score",
    "hrv_z_score":     "HRV z-score",
    "sleep_total_h":   "Sleep (hours)",
    "subj_energy":     "Subjective energy",
    "subj_mood":       "Subjective mood",
    "subj_stress":     "Subjective stress",
    "subj_day":        "Day quality",
}


async def _outcome_points(db: AsyncSession, outcome: str) -> dict[date, float]:
    """All-history {date: value} for a contrast outcome (shared by day-of-week
    and workout-type contrasts)."""
    points: dict[date, float] = {}
    if outcome == "readiness_score":
        rows = (await db.execute(
            select(DailyMetric.date, DailyMetric.readiness_score)
            .where(DailyMetric.readiness_score.isnot(None))
        )).all()
        points = {r.date: float(r.readiness_score) for r in rows}

    elif outcome == "hrv_z_score":
        rows = (await db.execute(
            select(DailyMetric.date, DailyMetric.hrv_z_score)
            .where(DailyMetric.hrv_z_score.isnot(None))
        )).all()
        points = {r.date: float(r.hrv_z_score) for r in rows}

    elif outcome == "sleep_total_h":
        rows = (await db.execute(
            select(SleepSession.date, SleepSession.total_min)
            .where(SleepSession.total_min.isnot(None))
        )).all()
        points = {r.date: r.total_min / 60.0 for r in rows}

    elif outcome == "subj_energy":
        rows = (await db.execute(
            select(SubjectiveLog.date, SubjectiveLog.energy)
            .where(SubjectiveLog.energy.isnot(None))
        )).all()
        points = {r.date: float(r.energy) for r in rows}

    elif outcome == "subj_mood":
        rows = (await db.execute(
            select(SubjectiveLog.date, SubjectiveLog.mood)
            .where(SubjectiveLog.mood.isnot(None))
        )).all()
        points = {r.date: float(r.mood) for r in rows}

    elif outcome == "subj_stress":
        rows = (await db.execute(
            select(SubjectiveLog.date, SubjectiveLog.stress)
            .where(SubjectiveLog.stress.isnot(None))
        )).all()
        points = {r.date: float(r.stress) for r in rows}

    elif outcome == "subj_day":
        rows = (await db.execute(
            select(SubjectiveLog.date, SubjectiveLog.day_score)
            .where(SubjectiveLog.day_score.isnot(None))
        )).all()
        points = {r.date: float(r.day_score) for r in rows}

    return points


def local_date_sql_value(ts) -> str:
    """Naive-UTC timestamp -> local YYYY-MM-DD (the Python twin of local_date_sql)."""
    from zoneinfo import ZoneInfo

    from app.config import settings as _s
    return (ts.replace(tzinfo=ZoneInfo("UTC"))
              .astimezone(ZoneInfo(_s.tz)).date().isoformat())
