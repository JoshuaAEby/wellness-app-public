"""Event-driven morning pipeline.

Replaces the fixed 08:00/08:02/08:05/08:15 cron stack (which was late on early
wakes and useless on gym-first-thing days): the pipeline now fires on the FIRST
qualifying webhook of the morning — the one that delivers today's finalized
sleep session, i.e. the earliest moment the day's inputs actually exist — with
two safety nets: 09:30 (runs only if the sleep has arrived) and 12:30 (runs
regardless). Steps, in order:

  1. recompute the last 7 days of daily_metrics (fresh overnight data)
  2. confirm the coach's night-before draw (eligibility check → ITT inclusion)
  3. generate + push the daily summary, its ntfy title carrying the coach call
  4. illness/overtraining check (pushes only when elevated)

Idempotent per local day via user_settings.last_morning_pipeline_date, which is
claimed BEFORE the slow steps so overlapping triggers (webhook + fallback, or
two webhooks) can't double-run. Each step is individually fail-safe: a summary
failure must not block the illness check.
"""
import logging
from datetime import timedelta

from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import UserSettings
from app.timeutils import local_today

logger = logging.getLogger(__name__)


async def run_morning_pipeline(trigger: str, force: bool = False,
                               require_sleep: bool = False) -> dict:
    """`require_sleep`: do nothing (and do NOT claim the day) unless last night's
    sleep session has arrived. The 09:30 fallback uses it: on 9/24 and 9/25 it ran
    before a late wake had synced, and the one-run-per-day claim then locked in a
    briefing built on the previous night's sleep and pre-sync HRV (note 57). The
    12:30 final fallback runs without it, for days the sleep never syncs."""
    today = local_today()

    # Claim the day (dedupe concurrent triggers) before doing anything slow.
    async with AsyncSessionLocal() as db:
        us = (await db.execute(select(UserSettings).limit(1))).scalar_one_or_none()
        if us is None:
            return {"status": "no_settings"}
        if us.last_morning_pipeline_date == today and not force:
            return {"status": "already_ran", "date": today.isoformat()}
        if require_sleep:
            from app.models import SleepSession
            has_sleep = (await db.execute(
                select(SleepSession.id).where(SleepSession.date == today).limit(1)
            )).scalar_one_or_none()
            if has_sleep is None:
                return {"status": "waiting_for_sleep", "date": today.isoformat()}
        us.last_morning_pipeline_date = today
        await db.commit()

    logger.info("Morning pipeline: starting (trigger=%s)", trigger)
    result: dict = {"status": "ran", "trigger": trigger, "date": today.isoformat()}

    # 1. Recompute recent daily metrics with the fresh overnight data
    try:
        from app.services.metrics_engine import recompute_dates
        async with AsyncSessionLocal() as db:
            await recompute_dates(db, [today - timedelta(days=i) for i in range(7, -1, -1)])
        result["recompute"] = "ok"
    except Exception as exc:
        logger.error("Morning pipeline: recompute failed: %s", exc)
        result["recompute"] = f"error: {exc}"

    # 1.2 Refit the personal activity factor k. Self-throttling: the service enforces
    #     its own weekly cadence and 1-SE hysteresis, so calling it daily is free and
    #     writes a row only when the fit actually moved.
    try:
        from app.services.energy import maybe_refit_activity_factor
        async with AsyncSessionLocal() as db:
            fit = await maybe_refit_activity_factor(db)
        result["activity_factor"] = f"refit k={fit['k']}" if fit else "unchanged"
    except Exception as exc:
        logger.error("Morning pipeline: activity-factor refit failed: %s", exc)
        result["activity_factor"] = f"error: {exc}"

    # 1.5 Environment sensors (HA bridge) — last night's bedroom aggregates.
    #     No-ops unless HA_URL/HA_TOKEN/HA_ENV_ENTITIES are configured.
    try:
        from app.services.env_sensors import ingest_env_sensors
        env_res = await ingest_env_sensors()
        result["env_sensors"] = env_res.get("status")
    except Exception as exc:
        logger.error("Morning pipeline: env-sensor ingest failed: %s", exc)
        result["env_sensors"] = f"error: {exc}"

    # 1.55 Post-trip buffer: a trip (>= 3 travel days) that ended in the last few days
    #      gets TRAVEL_RECOVERY_DAYS `travel_recovery` flags. After 1.5 because the env
    #      bridge is what materialises last night's auto `travel` flag.
    try:
        from app.services.day_flags import sync_travel_recovery
        async with AsyncSessionLocal() as db:
            made = await sync_travel_recovery(db, today)
        result["travel_recovery"] = [d.isoformat() for d in made]
    except Exception as exc:
        logger.error("Morning pipeline: travel-recovery sync failed: %s", exc)
        result["travel_recovery"] = f"error: {exc}"

    # 1.6 Early-night HR elevation (late-digestion/arousal signature) for the
    #     last 3 nights — same catch-up pattern as the env bridge, idempotent.
    try:
        from app.services.sleep_processing import compute_sleep_hr_elevation
        async with AsyncSessionLocal() as db:
            n = await compute_sleep_hr_elevation(
                db, [today - timedelta(days=i) for i in range(3)])
        result["sleep_hr_elevation"] = n
    except Exception as exc:
        logger.error("Morning pipeline: sleep-HR elevation failed: %s", exc)
        result["sleep_hr_elevation"] = f"error: {exc}"
    # 1.7 Its fixed-window variant (first 2 h after onset − night p10), stored alongside.
    try:
        from app.services.sleep_processing import compute_sleep_hr_early_fixed
        async with AsyncSessionLocal() as db:
            n = await compute_sleep_hr_early_fixed(
                db, [today - timedelta(days=i) for i in range(3)])
        result["sleep_hr_early_fixed"] = n
    except Exception as exc:
        logger.error("Morning pipeline: fixed-window early-night HR failed: %s", exc)
        result["sleep_hr_early_fixed"] = f"error: {exc}"

    # 2. Confirm the coach draw (or draw now if the 21:00 job missed) and get
    #    the blinded recommendation for the day
    coach_label = coach_detail = None
    home_blocks = []
    try:
        from app.services.coach import confirm_today, get_coach_recommendation
        from app.services.coach_plan import due_home_blocks
        async with AsyncSessionLocal() as db:
            await confirm_today(db)
            rec = await get_coach_recommendation(db, today)
            coach_label = rec["label"] if rec else None
            coach_detail = rec.get("detail") if rec else None
            # The day's one extra line rides along into the briefing + push (note 44).
            if rec and rec.get("extra"):
                coach_detail = f"{coach_detail} Also: {rec['extra']['detail']}"
            home_blocks = due_home_blocks(rec)
        result["coach"] = coach_label
    except Exception as exc:
        logger.error("Morning pipeline: coach confirm failed: %s", exc)
        result["coach"] = f"error: {exc}"

    # 3. Daily summary — the push title carries the coach call so the day's
    #    guidance and briefing land as ONE notification at wake
    try:
        from app.services.ai_summaries import generate_daily_summary
        text = await generate_daily_summary(coach_label=coach_label, coach_detail=coach_detail,
                                            home_blocks=home_blocks)
        from app.services.scheduler import _muted
        # Still generated and stored (the in-app card) while away — just not pushed.
        if text and settings.ntfy_url and not await _muted("nag", today):
            from app.services.scheduler import _ntfy_post
            title = f"Rho: {coach_label}" if coach_label else "Daily briefing"   # Rho = the coach (2026-10-29)
            # The push is PLAIN TEXT: ntfy showed the briefing's **Call:** markers as raw
            # asterisks (note 55). The stored text keeps its Markdown for the in-app card.
            import re
            plain = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
            plain = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", plain)
            await _ntfy_post(title, plain, "default", tags="clipboard")
        result["summary"] = "ok" if text else "skipped"
    except Exception as exc:
        logger.error("Morning pipeline: summary failed: %s", exc)
        result["summary"] = f"error: {exc}"

    # 4. Illness / overtraining early-warning (pushes only when elevated)
    try:
        from app.services.scheduler import _illness_check
        await _illness_check()
        result["illness_check"] = "ok"
    except Exception as exc:
        logger.error("Morning pipeline: illness check failed: %s", exc)
        result["illness_check"] = f"error: {exc}"

    # 5. Gamification stats (docs/GAMIFICATION.md). LAST on purpose: it is a
    #    read-only mirror over everything computed above, so it must run after the
    #    recompute, and nothing else may depend on it. Fail-safe like every other
    #    step — a game error must never affect the real pipeline.
    try:
        from app.services.game import recompute_and_store
        async with AsyncSessionLocal() as db:
            res = await recompute_and_store(db)
        result["game_stats"] = res.get("stats")
    except Exception as exc:
        logger.error("Morning pipeline: game stats failed: %s", exc)
        result["game_stats"] = f"error: {exc}"

    logger.info("Morning pipeline: done (%s)", result)
    return result
