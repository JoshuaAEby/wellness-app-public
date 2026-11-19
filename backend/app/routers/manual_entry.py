# `date as Date`: the Pydantic-v2 gotcha — a field NAMED `date`
# shadows the TYPE `date` during annotation resolution, turning Optional[date]
# into "must be None" (422 on any real date). Latent here until the evening
# check-in became the first client to actually send a date (2026-07-12).
import logging
from datetime import date, datetime, timedelta
from datetime import date as Date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import BodyMeasurement, BodyScan, BodyWeightLog, HealthMetric, SubjectiveLog
from app.timeutils import local_today

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/manual", tags=["manual"])


# ── Weight ────────────────────────────────────────────────────────────────────

class WeightIn(BaseModel):
    weight_kg: float
    timestamp: Optional[datetime] = None
    notes: Optional[str] = None


@router.post("/weight")
async def log_weight(
    body: WeightIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    entry = BodyWeightLog(
        weight_kg=body.weight_kg,
        timestamp=body.timestamp or datetime.utcnow(),
        notes=body.notes,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return {
        "id": entry.id,
        "weight_kg": entry.weight_kg,
        "timestamp": entry.timestamp.isoformat(),
        "notes": entry.notes,
    }


@router.get("/weight")
async def get_weight(
    days: int = Query(default=30, le=365),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    since = datetime.utcnow() - timedelta(days=days)
    result = await db.execute(
        select(BodyWeightLog)
        .where(BodyWeightLog.timestamp >= since)
        .order_by(BodyWeightLog.timestamp.desc())
    )
    rows = result.scalars().all()
    return [
        {"id": r.id, "weight_kg": r.weight_kg, "timestamp": r.timestamp.isoformat(), "notes": r.notes}
        for r in rows
    ]


# ── Tape measurements ─────────────────────────────────────────────────────────
# Added 2026-09-12 (see models.BodyMeasurement for why). A second entry on the same
# day MERGES — only the sites sent are overwritten — so waist in the morning and arm
# later in the day do not erase each other.

_TAPE_SITES = ("waist_cm", "arm_cm", "chest_cm")
_TAPE_RANGE_CM = (10.0, 250.0)   # rejects inches-typed-as-cm and stray digits


class MeasurementIn(BaseModel):
    date: Optional[Date] = None
    waist_cm: Optional[float] = None
    arm_cm: Optional[float] = None
    chest_cm: Optional[float] = None
    notes: Optional[str] = None


def _serialize_measurement(r: BodyMeasurement) -> dict:
    return {"date": r.date.isoformat(), "waist_cm": r.waist_cm, "arm_cm": r.arm_cm,
            "chest_cm": r.chest_cm, "notes": r.notes}


@router.post("/measurements")
async def log_measurement(
    body: MeasurementIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    from app.timeutils import local_today

    vals = {k: getattr(body, k) for k in _TAPE_SITES if getattr(body, k) is not None}
    if not vals:
        raise HTTPException(status_code=422, detail="send at least one of waist_cm, arm_cm, chest_cm")
    bad = [k for k, v in vals.items() if not _TAPE_RANGE_CM[0] <= v <= _TAPE_RANGE_CM[1]]
    if bad:
        raise HTTPException(status_code=422, detail=f"out of range (cm): {', '.join(bad)}")
    if body.notes is not None:
        vals["notes"] = body.notes
    day = body.date or local_today()
    await db.execute(
        sqlite_insert(BodyMeasurement).values(date=day, **vals)
        .on_conflict_do_update(index_elements=["date"], set_=vals)
    )
    await db.commit()
    row = (await db.execute(
        select(BodyMeasurement).where(BodyMeasurement.date == day)
    )).scalar_one()
    return _serialize_measurement(row)


@router.get("/measurements")
async def get_measurements(
    days: int = Query(default=365, le=1825),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    since = local_today() - timedelta(days=days)
    rows = (await db.execute(
        select(BodyMeasurement).where(BodyMeasurement.date >= since)
        .order_by(BodyMeasurement.date.desc())
    )).scalars().all()
    return [_serialize_measurement(r) for r in rows]


@router.delete("/measurements")
async def delete_measurement(
    date: Date = Query(...),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    row = (await db.execute(
        select(BodyMeasurement).where(BodyMeasurement.date == date)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="no measurement on that date")
    await db.delete(row)
    await db.commit()
    return Response(status_code=204)


# ── Body scans ────────────────────────────────────────────────────────────────
# Added 2026-10-29 (see models.BodyScan). A scan is one printout, so a second entry on
# the same date REPLACES the row (unlike tape, which merges sites).

_SCAN_RANGES = {
    "weight_lb": (50.0, 600.0), "body_fat_pct": (2.0, 75.0), "fat_mass_lb": (1.0, 400.0),
    "lean_mass_lb": (30.0, 300.0), "smm_lb": (15.0, 200.0), "tbw_lb": (20.0, 300.0),
}
_SCAN_SEGMENTS = ("left_arm", "right_arm", "trunk", "left_leg", "right_leg")
_SCAN_HOME_WINDOW_D = 3   # nearest home-scale reading within ±3 local days


class ScanIn(BaseModel):
    date: Date
    time: Optional[str] = None
    device: Optional[str] = None
    weight_lb: float
    body_fat_pct: float
    fat_mass_lb: Optional[float] = None
    lean_mass_lb: Optional[float] = None
    smm_lb: Optional[float] = None
    tbw_lb: Optional[float] = None
    segments: Optional[dict[str, float]] = None
    notes: Optional[str] = None


async def _home_scale_near(db: AsyncSession, day: Date) -> Optional[dict]:
    """The home scale's reading on the local day nearest `day` (±3 d), same weigh-in.

    Weight, BF% and lean arrive together from the scale with one timestamp, so the
    three are taken from the same instant. None when the scale has nothing nearby.
    """
    from app.timeutils import local_day_bounds_utc, to_local_date

    lo = local_day_bounds_utc(day - timedelta(days=_SCAN_HOME_WINDOW_D))[0]
    hi = local_day_bounds_utc(day + timedelta(days=_SCAN_HOME_WINDOW_D))[1]
    rows = (await db.execute(
        select(HealthMetric.timestamp, HealthMetric.metric_type, HealthMetric.value)
        .where(HealthMetric.metric_type.in_(("weight", "body_fat_percentage", "lean_body_mass")))
        .where(HealthMetric.timestamp >= lo).where(HealthMetric.timestamp < hi)
    )).all()
    by_ts: dict = {}
    for ts, mt, v in rows:
        by_ts.setdefault(ts, {})[mt] = v
    full = [(ts, m) for ts, m in by_ts.items() if "lean_body_mass" in m and "weight" in m]
    if not full:
        return None
    # Nearest day first; on a tie prefer the earlier reading (the scan is a morning one).
    ts, m = min(full, key=lambda t: (abs((to_local_date(t[0]) - day).days), t[0]))
    return {"date": to_local_date(ts).isoformat(), "weight_lb": m["weight"],
            "body_fat_pct": (round(m["body_fat_percentage"], 1) if m.get("body_fat_percentage") is not None else None),
            "lean_mass_lb": round(m["lean_body_mass"], 1)}


def _serialize_scan(r: BodyScan, home: Optional[dict]) -> dict:
    out = {c: getattr(r, c) for c in (
        "time", "device", "weight_lb", "body_fat_pct", "fat_mass_lb", "lean_mass_lb",
        "smm_lb", "tbw_lb", "segments", "notes")}
    out["id"] = r.id
    out["date"] = r.date.isoformat()
    out["home_scale"] = home
    if home and r.lean_mass_lb is not None:
        # + = the home scale reads MORE lean (less fat) than the scan.
        out["home_scale"] = {**home, "lean_gap_lb": round(home["lean_mass_lb"] - r.lean_mass_lb, 1),
                             "bf_gap_pct": (round(home["body_fat_pct"] - r.body_fat_pct, 1)
                                            if home.get("body_fat_pct") is not None else None)}
    return out


@router.post("/scans")
async def log_scan(
    body: ScanIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    vals = body.model_dump()
    bad = [k for k, (lo, hi) in _SCAN_RANGES.items()
           if vals.get(k) is not None and not lo <= vals[k] <= hi]
    if bad:
        raise HTTPException(status_code=422, detail=f"out of range: {', '.join(bad)}")
    if vals["segments"] is not None:
        segs = {k: v for k, v in vals["segments"].items() if v is not None}
        unknown = set(segs) - set(_SCAN_SEGMENTS)
        if unknown or any(not 0.5 <= v <= 150 for v in segs.values()):
            raise HTTPException(status_code=422, detail="segments: unknown site or out of range")
        vals["segments"] = segs or None
    if vals["fat_mass_lb"] is None:
        vals["fat_mass_lb"] = round(vals["weight_lb"] * vals["body_fat_pct"] / 100, 1)
    if vals["lean_mass_lb"] is None:
        vals["lean_mass_lb"] = round(vals["weight_lb"] - vals["fat_mass_lb"], 1)
    day = vals.pop("date")
    await db.execute(
        sqlite_insert(BodyScan).values(date=day, **vals)
        .on_conflict_do_update(index_elements=["date"], set_=vals)
    )
    await db.commit()
    row = (await db.execute(select(BodyScan).where(BodyScan.date == day))).scalar_one()
    return _serialize_scan(row, await _home_scale_near(db, day))


@router.get("/scans")
async def get_scans(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    rows = (await db.execute(select(BodyScan).order_by(BodyScan.date.desc()))).scalars().all()
    return [_serialize_scan(r, await _home_scale_near(db, r.date)) for r in rows]


@router.delete("/scans")
async def delete_scan(
    date: Date = Query(...),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    row = (await db.execute(select(BodyScan).where(BodyScan.date == date))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="no scan on that date")
    await db.delete(row)
    await db.commit()
    return Response(status_code=204)


# ── Wellbeing ─────────────────────────────────────────────────────────────────

def _serialize_subjective(r: SubjectiveLog) -> dict:
    return {
        "id": r.id,
        "date": r.date.isoformat(),
        "wellbeing_score": r.wellbeing_score,
        "sleep_quality": r.sleep_quality,
        "energy": r.energy,
        "mood": r.mood,
        "soreness": r.soreness,
        "stress": r.stress,
        "sick": r.sick,
        "day_score": r.day_score,
        "notes": r.notes,
    }


class WellbeingIn(BaseModel):
    # Subscores are 1.0–5.0 floats (0.1-step slider); legacy integers are valid.
    wellbeing_score: Optional[float] = None  # auto-computed from subscores if absent
    sleep_quality: Optional[float] = None    # 1–5
    energy: Optional[float] = None           # 1–5 (5 = high energy)
    mood: Optional[float] = None             # 1–5
    soreness: Optional[str] = None           # free text; null = none
    stress: Optional[float] = None           # 1–5 (5 = very stressed); NOT in wellbeing avg
    sick: Optional[bool] = None              # sick day — excluded from correlation series
    day_score: Optional[float] = None        # 1–5, evening rating of the whole day
    date: Optional[Date] = None              # MUST be the Date alias — see import note
    notes: Optional[str] = None


@router.post("/wellbeing")
async def log_wellbeing(
    body: WellbeingIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    entry_date = body.date or local_today()

    # Merge against any existing row so a partial submission (e.g. an iOS Shortcut
    # that sends only subscores, or the modal omitting notes) preserves the fields
    # it didn't include rather than nulling them. model_fields_set distinguishes an
    # explicit null ("clear this") from an omitted field ("leave it alone").
    existing = (await db.execute(
        select(SubjectiveLog).where(SubjectiveLog.date == entry_date)
    )).scalar_one_or_none()

    def _pick(attr):
        if attr in body.model_fields_set:
            return getattr(body, attr)            # explicit value, even if None
        return getattr(existing, attr) if existing else None  # preserve / default

    sleep_quality = _pick("sleep_quality")
    energy        = _pick("energy")
    mood          = _pick("mood")
    soreness      = _pick("soreness")
    stress        = _pick("stress")
    sick          = _pick("sick")
    day_score     = _pick("day_score")
    notes         = _pick("notes")

    # wellbeing_score: explicit if provided, else auto-compute from the *merged*
    # subscores. Column is NOT NULL, so fall back to the existing value if neither
    # an explicit score nor any subscore is available.
    if "wellbeing_score" in body.model_fields_set and body.wellbeing_score is not None:
        wellbeing_score = body.wellbeing_score
    else:
        subscores = [s for s in [sleep_quality, energy, mood] if s is not None]
        if subscores:
            wellbeing_score = round(sum(subscores) / len(subscores), 1)
        elif existing is not None:
            wellbeing_score = existing.wellbeing_score
        else:
            # Nullable since 0017 — an evening-only entry (day_score) has no subscores.
            wellbeing_score = body.wellbeing_score

    merged = {
        "wellbeing_score": wellbeing_score,
        "sleep_quality":   sleep_quality,
        "energy":          energy,
        "mood":            mood,
        "soreness":        soreness,
        "stress":          stress,   # deliberately NOT folded into wellbeing_score
        "sick":            sick,
        "day_score":       day_score,
        "notes":           notes,
    }

    # Upsert: replace existing entry for the same date
    stmt = sqlite_insert(SubjectiveLog).values(date=entry_date, **merged)
    stmt = stmt.on_conflict_do_update(index_elements=["date"], set_=merged)
    await db.execute(stmt)

    # Mirror illness into day_flags, the general mechanism the rest of the app reads
    # (note 23). Uses the MERGED value, so a check-in that omits `sick` inherits the
    # day's existing answer rather than silently retracting it. Same transaction as the
    # upsert above: the two records must never disagree because one write succeeded.
    from app.services.day_flags import sync_sick_flag
    action = await sync_sick_flag(db, entry_date, sick)
    await db.commit()
    if action in ("created", "cleared"):
        logger.info("Check-in %s sick flag for %s", action, entry_date)

    result = await db.execute(select(SubjectiveLog).where(SubjectiveLog.date == entry_date))
    row = result.scalar_one()
    return _serialize_subjective(row)


@router.get("/wellbeing")
async def get_wellbeing(
    days: int = Query(default=30, le=365),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    since = local_today() - timedelta(days=days)
    result = await db.execute(
        select(SubjectiveLog)
        .where(SubjectiveLog.date >= since)
        .order_by(SubjectiveLog.date.desc())
    )
    rows = result.scalars().all()
    return [_serialize_subjective(r) for r in rows]
