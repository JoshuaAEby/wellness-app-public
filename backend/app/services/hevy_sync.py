"""Sync workouts from the Hevy public API (https://api.hevyapp.com).

Two modes:
  - run_backfill():          page through /v1/workouts once (pageSize max 10) — initial import
  - run_incremental_sync():  poll /v1/workouts/events?since=<cursor> for updates + deletes

Set-level data lands in hevy_workouts / hevy_sets. Each Hevy workout is linked to the
overlapping Apple Health session (workouts.apple_uuid) so TRIMP stays the load currency
and Hevy adds the external-load detail. Disabled (no-op) unless HEVY_API_KEY is set.

The Hevy API self-describes as early-stage with no stability guarantees — parse
defensively, log unknown shapes, never let a sync failure crash the scheduler.
"""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import HevyExerciseTemplate, HevySet, HevySyncState, HevyWorkout, Workout

logger = logging.getLogger(__name__)

HEVY_BASE = "https://api.hevyapp.com/v1"
PAGE_SIZE = 10                      # documented maximum
REQUEST_DELAY_S = 0.5               # politeness delay between paged requests (rate limits undocumented)
LINK_TOLERANCE = timedelta(minutes=10)

# e1RM (Epley) is only computed for sets at or below this rep count — the formula
# degrades badly at high reps. Used by the strength-metrics endpoints, defined here
# so sync-side aggregates (avg_top_set_rpe's "top set") use the same convention.
E1RM_MAX_REPS = 10

# One-dumbbell lifts logged as if both dumbbells counted (2026-11-16: "those must be
# combined"). Hevy still holds the doubled numbers, so every sync of a workout before the
# date halves them here — a backfill or an edit of that workout can't bring them back.
# If he ever corrects those sets IN HEVY, remove the entry or they get halved twice.
# template id → (first date logged correctly, reps multiplier). The multiplier is for a set
# logged as one side's reps: "40 × 13" logged for 13 per arm on one 20 lb
# dumbbell is really 20 × 26 (2026-11-16).
WEIGHT_LOGGED_COMBINED_BEFORE = {
    "724CDE60": (date(2026, 11, 16), 2),    # Concentration Curl, reps were per arm
    "6127A3AD": (date(2026, 11, 16), 2),    # Triceps Kickback (Dumbbell), reps were per arm
}


def _correct_weights(exercises: list[dict], start: datetime) -> list[dict]:
    """Halve set weights (and scale reps) for WEIGHT_LOGGED_COMBINED_BEFORE templates
    before their date."""
    out = []
    for ex in exercises:
        fix = WEIGHT_LOGGED_COMBINED_BEFORE.get(ex.get("exercise_template_id"))
        if fix and start.date() < fix[0]:
            mult = fix[1]
            ex = {**ex, "sets": [{**st,
                                  "weight_kg": (st["weight_kg"] / 2 if st.get("weight_kg") else st.get("weight_kg")),
                                  "reps": (st["reps"] * mult if st.get("reps") else st.get("reps"))}
                                 for st in ex.get("sets") or []]}
        out.append(ex)
    return out


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=HEVY_BASE,
        headers={"api-key": settings.hevy_api_key},
        timeout=30,
    )


def _parse_iso(ts: str | None) -> datetime | None:
    """Hevy ISO 8601 timestamp → naive UTC (matches workouts storage convention)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        logger.warning("Hevy: unparseable timestamp %r", ts)
        return None


def epley_e1rm(weight_kg: float | None, reps: int | None,
               max_reps: int = E1RM_MAX_REPS) -> float | None:
    """Estimated 1RM via Epley. None outside the validity range (reps 1..max_reps)."""
    if not weight_kg or not reps or reps < 1 or reps > max_reps:
        return None
    return weight_kg * (1 + reps / 30.0)


def _workout_aggregates(exercises: list[dict]) -> tuple[float | None, int, float | None]:
    """(tonnage_kg, working_sets, avg_top_set_rpe) from a Hevy exercises[] payload.

    Working set = any non-warmup set. Tonnage counts only sets with weight AND reps
    (bodyweight work contributes working_sets but not tonnage — a deliberate choice:
    imputing bodyweight adds noise for no analytical gain). Top set per exercise =
    highest e1RM (fallback: heaviest weight); its RPE feeds avg_top_set_rpe.
    """
    tonnage = 0.0
    has_tonnage = False
    working = 0
    top_rpes: list[float] = []

    for ex in exercises or []:
        best_key = None
        best_rpe = None
        for s in ex.get("sets") or []:
            if s.get("type") == "warmup":
                continue
            working += 1
            w, r = s.get("weight_kg"), s.get("reps")
            if w and r:
                tonnage += w * r
                has_tonnage = True
            key = epley_e1rm(w, r) or (w or 0.0)
            if key and (best_key is None or key > best_key):
                best_key = key
                best_rpe = s.get("rpe")
        if best_rpe is not None:
            top_rpes.append(float(best_rpe))

    avg_rpe = round(sum(top_rpes) / len(top_rpes), 1) if top_rpes else None
    return (round(tonnage, 1) if has_tonnage else None), working, avg_rpe


async def _get_sync_state(db: AsyncSession) -> HevySyncState:
    state = (await db.execute(select(HevySyncState).limit(1))).scalars().first()
    if state is None:
        state = HevySyncState(backfill_done=False)
        db.add(state)
        await db.flush()
    return state


async def _link_apple_workout(db: AsyncSession, start: datetime, end: datetime | None) -> str | None:
    """Find the Apple Health session overlapping this Hevy workout (± LINK_TOLERANCE).

    Returns workouts.apple_uuid or None. Picks the largest overlap when several match
    (e.g. a walk logged the same evening).
    """
    end = end or start
    rows = (await db.execute(
        select(Workout)
        .where(Workout.start_time < end + LINK_TOLERANCE)
        .where(Workout.end_time > start - LINK_TOLERANCE)
    )).scalars().all()
    if not rows:
        return None

    def overlap(w: Workout) -> float:
        w_end = w.end_time or w.start_time
        return (min(end, w_end) - max(start, w.start_time)).total_seconds()

    return max(rows, key=overlap).apple_uuid


async def _autofill_session_rpe(db: AsyncSession, apple_uuid: str, avg_top_set_rpe: float | None) -> None:
    """Fill workouts.rpe from Hevy per-set RPE — only when no manual value exists."""
    if avg_top_set_rpe is None:
        return
    workout = (await db.execute(
        select(Workout).where(Workout.apple_uuid == apple_uuid)
    )).scalars().first()
    if workout is not None and workout.rpe is None:
        workout.rpe = min(10.0, max(1.0, round(avg_top_set_rpe, 1)))


async def _ensure_templates(db: AsyncSession, client: httpx.AsyncClient, template_ids: set[str]) -> None:
    """Fetch any exercise templates we haven't cached (muscle-group attribution)."""
    if not template_ids:
        return
    cached = set((await db.execute(
        select(HevyExerciseTemplate.id).where(HevyExerciseTemplate.id.in_(template_ids))
    )).scalars().all())
    for tid in template_ids - cached:
        try:
            resp = await client.get(f"/exercise_templates/{tid}")
            resp.raise_for_status()
            t = resp.json()
            db.add(HevyExerciseTemplate(
                id=t["id"],
                title=t.get("title"),
                exercise_type=t.get("type"),
                primary_muscle_group=t.get("primary_muscle_group"),
                secondary_muscle_groups=t.get("secondary_muscle_groups") or [],
                is_custom=t.get("is_custom"),
            ))
            await asyncio.sleep(REQUEST_DELAY_S)
        except Exception as exc:
            logger.warning("Hevy: failed to fetch template %s: %s", tid, exc)


_templates_checked_on: date | None = None


async def refresh_template_titles(db: AsyncSession, client: httpx.AsyncClient) -> list[tuple[str, str]]:
    """Pick up renames of his own (custom) exercises, once a day.

    Templates are cached once and every set keeps its exercise title, so a rename in Hevy
    (2026-11-16: "Tate Press (tricep)" → "Floor Tricep Press", because the real Tate press
    is a different lift) never reached the app: old sets kept the old name, and a new
    exercise given that name would have merged with them everywhere titles are matched.
    Renames the template, its sets and any learned class-board alias. Only custom
    templates that have sets are checked (Hevy's own names don't change).
    """
    from app.models import ClassAlias
    ids = (await db.execute(
        select(HevyExerciseTemplate.id, HevyExerciseTemplate.title)
        .where(HevyExerciseTemplate.is_custom.is_(True))
        .where(HevyExerciseTemplate.id.in_(select(HevySet.exercise_template_id).distinct()))
    )).all()
    renamed = []
    for tid, old in ids:
        try:
            resp = await client.get(f"/exercise_templates/{tid}")
            resp.raise_for_status()
            new = (resp.json().get("title") or "").strip()
        except Exception as exc:
            logger.warning("Hevy: template %s refresh failed: %s", tid, exc)
            continue
        finally:
            await asyncio.sleep(REQUEST_DELAY_S)
        if new and new != old:
            await db.execute(update(HevyExerciseTemplate).where(HevyExerciseTemplate.id == tid).values(title=new))
            await db.execute(update(HevySet).where(HevySet.exercise_template_id == tid).values(exercise_title=new))
            if old:
                await db.execute(update(ClassAlias).where(ClassAlias.title == old).values(title=new))
            renamed.append((old, new))
            logger.info("Hevy: exercise renamed %r → %r", old, new)
    await db.commit()
    return renamed


async def _upsert_workout(db: AsyncSession, client: httpx.AsyncClient, wjson: dict) -> None:
    """Replace one Hevy workout + its sets from an API payload."""
    wid = wjson["id"]
    start = _parse_iso(wjson.get("start_time"))
    if start is None:
        logger.warning("Hevy: workout %s has no parseable start_time, skipping", wid)
        return
    end = _parse_iso(wjson.get("end_time"))
    exercises = _correct_weights(wjson.get("exercises") or [], start)

    tonnage, working, avg_rpe = _workout_aggregates(exercises)
    apple_uuid = await _link_apple_workout(db, start, end)

    # Preserve the PR-notification marker across the delete/re-add (re-syncs and
    # edits must not re-fire a PR push for the same workout).
    prior_pr_notified = (await db.execute(
        select(HevyWorkout.pr_notified_at).where(HevyWorkout.id == wid)
    )).scalar_one_or_none()

    await db.execute(delete(HevySet).where(HevySet.hevy_workout_id == wid))
    await db.execute(delete(HevyWorkout).where(HevyWorkout.id == wid))
    db.add(HevyWorkout(
        id=wid,
        title=wjson.get("title"),
        start_time=start,
        end_time=end,
        hevy_updated_at=_parse_iso(wjson.get("updated_at")),
        apple_uuid=apple_uuid,
        tonnage_kg=tonnage,
        working_sets=working,
        avg_top_set_rpe=avg_rpe,
        synced_at=datetime.utcnow(),
        pr_notified_at=prior_pr_notified,
    ))

    template_ids: set[str] = set()
    for ex in exercises:
        tid = ex.get("exercise_template_id")
        if tid:
            template_ids.add(tid)
        for s in ex.get("sets") or []:
            reps = s.get("reps")
            db.add(HevySet(
                hevy_workout_id=wid,
                exercise_index=ex.get("index"),
                exercise_title=ex.get("title"),
                exercise_template_id=tid,
                superset_id=ex.get("supersets_id"),
                set_index=s.get("index"),
                set_type=s.get("type"),
                weight_kg=s.get("weight_kg"),
                reps=int(reps) if reps is not None else None,
                distance_meters=s.get("distance_meters"),
                duration_seconds=s.get("duration_seconds"),
                rpe=s.get("rpe"),
            ))

    await _ensure_templates(db, client, template_ids)
    if apple_uuid:
        await _autofill_session_rpe(db, apple_uuid, avg_rpe)


# ── PR detection (self-arming) ────────────────────────────────────────────────

PR_MIN_SESSIONS = 5          # prior sessions of a lift before its PRs are "armed"
PR_MIN_HISTORY_DAYS = 28     # and at least this much history on that lift
PR_RECENT_WINDOW_H = 48      # only notify for workouts this recent (old edits stay silent)
PR_MIN_IMPROVEMENT = 1.005   # must beat prior best e1RM by >0.5% (float/rounding noise)

LB_PER_KG = 2.20462


async def _check_prs(db: AsyncSession, workout_ids: list[str]) -> None:
    """e1RM PR detection over just-synced workouts, pushed via ntfy.

    Self-arming per lift: silent until ≥PR_MIN_SESSIONS prior sessions AND
    ≥PR_MIN_HISTORY_DAYS of history exist for that exercise, so early sessions
    don't fire a "PR" on every visit. One push per workout (all PRs batched);
    hevy_workouts.pr_notified_at dedupes re-syncs/edits. Never raises — a notify
    failure must not break the sync path. Deliberately NOT run during backfill.
    """
    if not settings.ntfy_url or not workout_ids:
        return
    from app.services.scheduler import _ntfy_post

    now = datetime.utcnow()
    for wid in workout_ids:
        wo = (await db.execute(
            select(HevyWorkout).where(HevyWorkout.id == wid)
        )).scalar_one_or_none()
        if wo is None or wo.pr_notified_at is not None:
            continue
        if wo.start_time < now - timedelta(hours=PR_RECENT_WINDOW_H):
            continue

        sets = (await db.execute(
            select(HevySet).where(HevySet.hevy_workout_id == wid)
        )).scalars().all()

        # Best e1RM per exercise within THIS workout
        best_now: dict[str, tuple[float, HevySet]] = {}
        for s in sets:
            if s.set_type == "warmup":
                continue
            e = epley_e1rm(s.weight_kg, s.reps)
            if e is None:
                continue
            key = s.exercise_template_id or s.exercise_title
            if key and (key not in best_now or e > best_now[key][0]):
                best_now[key] = (e, s)

        prs: list[str] = []
        for _, (e_now, s_now) in best_now.items():
            id_col = (HevySet.exercise_template_id if s_now.exercise_template_id
                      else HevySet.exercise_title)
            match_val = s_now.exercise_template_id or s_now.exercise_title
            prior_rows = (await db.execute(
                select(HevySet.weight_kg, HevySet.reps,
                       HevySet.hevy_workout_id, HevyWorkout.start_time)
                .join(HevyWorkout, HevyWorkout.id == HevySet.hevy_workout_id)
                .where(id_col == match_val)
                .where(HevySet.hevy_workout_id != wid)
                .where(HevyWorkout.start_time < wo.start_time)
                .where(HevySet.set_type != "warmup")
            )).all()
            prior_sessions = {r.hevy_workout_id for r in prior_rows}
            if len(prior_sessions) < PR_MIN_SESSIONS:
                continue  # not armed yet
            first_seen = min(r.start_time for r in prior_rows)
            if (wo.start_time - first_seen).days < PR_MIN_HISTORY_DAYS:
                continue  # not armed yet
            prior_best = max(
                (epley_e1rm(r.weight_kg, r.reps) or 0.0) for r in prior_rows
            )
            if prior_best <= 0 or e_now < prior_best * PR_MIN_IMPROVEMENT:
                continue
            pct = (e_now / prior_best - 1) * 100
            prs.append(
                f"{s_now.exercise_title}: e1RM {e_now * LB_PER_KG:.0f} lb "
                f"({s_now.weight_kg * LB_PER_KG:.0f} lb × {s_now.reps}) — "
                f"was {prior_best * LB_PER_KG:.0f}, +{pct:.1f}%"
            )

        if prs:
            title = f"New PR{'s' if len(prs) > 1 else ''} — {wo.title or 'strength session'}"
            try:
                await _ntfy_post(title, "\n".join(prs), "default", tags="trophy")
                wo.pr_notified_at = now
                await db.commit()
                logger.info("PR push sent for workout %s: %d PR(s)", wid, len(prs))
            except Exception as exc:  # notify failure must never break sync
                logger.error("PR notify failed for %s: %s", wid, exc)


async def run_backfill(db: AsyncSession) -> dict:
    """Full import: page through /v1/workouts. Safe to re-run (idempotent upserts)."""
    if not settings.hevy_api_key:
        return {"status": "disabled", "detail": "HEVY_API_KEY not set"}

    imported = 0
    async with _client() as client:
        page = 1
        while True:
            resp = await client.get("/workouts", params={"page": page, "pageSize": PAGE_SIZE})
            resp.raise_for_status()
            data = resp.json()
            workouts = data.get("workouts") or []
            for w in workouts:
                await _upsert_workout(db, client, w)
                imported += 1
            if page >= (data.get("page_count") or 1) or not workouts:
                break
            page += 1
            await asyncio.sleep(REQUEST_DELAY_S)

    state = await _get_sync_state(db)
    state.backfill_done = True
    state.last_event_check = datetime.utcnow()
    state.last_sync_at = datetime.utcnow()
    state.last_error = None
    await db.commit()
    logger.info("Hevy backfill complete: %d workouts imported", imported)
    return {"status": "ok", "imported": imported}


async def run_incremental_sync(db: AsyncSession) -> dict:
    """Poll /v1/workouts/events since the stored cursor; apply updates and deletes."""
    if not settings.hevy_api_key:
        return {"status": "disabled", "detail": "HEVY_API_KEY not set"}

    state = await _get_sync_state(db)
    if not state.backfill_done:
        logger.info("Hevy: backfill not done yet — running backfill instead of incremental sync")
        return await run_backfill(db)

    # Overlap the cursor by a minute so events landing exactly on it aren't missed.
    since = (state.last_event_check or datetime(1970, 1, 1)) - timedelta(minutes=1)
    since_str = since.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    now = datetime.utcnow()

    updated = deleted = 0
    upserted_ids: list[str] = []
    try:
        async with _client() as client:
            page = 1
            while True:
                resp = await client.get(
                    "/workouts/events",
                    params={"page": page, "pageSize": PAGE_SIZE, "since": since_str},
                )
                resp.raise_for_status()
                data = resp.json()
                for ev in data.get("events") or []:
                    if ev.get("type") == "updated" and ev.get("workout"):
                        await _upsert_workout(db, client, ev["workout"])
                        updated += 1
                        if ev["workout"].get("id"):
                            upserted_ids.append(ev["workout"]["id"])
                    elif ev.get("type") == "deleted" and ev.get("id"):
                        await db.execute(delete(HevySet).where(HevySet.hevy_workout_id == ev["id"]))
                        await db.execute(delete(HevyWorkout).where(HevyWorkout.id == ev["id"]))
                        deleted += 1
                    else:
                        logger.warning("Hevy: unrecognised event shape: %s", {k: ev.get(k) for k in ("type", "id")})
                if page >= (data.get("page_count") or 1):
                    break
                page += 1
                await asyncio.sleep(REQUEST_DELAY_S)

        state.last_event_check = now
        state.last_sync_at = now
        state.last_error = None
        await db.commit()
    except Exception as exc:
        await db.rollback()
        # Re-fetch state on the rolled-back session to record the failure.
        state = await _get_sync_state(db)
        state.last_error = str(exc)[:500]
        await db.commit()
        logger.error("Hevy incremental sync failed: %s", exc)
        return {"status": "error", "detail": str(exc)}

    if upserted_ids:
        try:
            await _check_prs(db, upserted_ids)
        except Exception as exc:  # PR detection is best-effort; sync already succeeded
            logger.error("PR check failed: %s", exc)

    global _templates_checked_on
    from app.timeutils import local_today
    if _templates_checked_on != local_today():
        try:
            async with _client() as client:
                await refresh_template_titles(db, client)
            _templates_checked_on = local_today()
        except Exception as exc:  # best-effort, like PR detection
            logger.warning("Hevy template refresh failed: %s", exc)

    if updated or deleted:
        logger.info("Hevy sync: %d updated, %d deleted", updated, deleted)
    return {"status": "ok", "updated": updated, "deleted": deleted}
