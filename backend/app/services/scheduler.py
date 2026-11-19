"""APScheduler jobs for background processing."""
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.database import AsyncSessionLocal
from app.timeutils import local_today

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def _muted(kind: str, *days) -> bool:
    """True if a `kind` push should be skipped because any of `days` (default: today)
    is an away day — or, for kind="recovery", a post-trip recovery day. See
    services/day_flags.push_mute_reason. Logged so a silent day is explainable."""
    from app.services.day_flags import push_mute_reason
    from app.timeutils import local_today
    days = days or (local_today(),)
    async with AsyncSessionLocal() as db:
        for d in days:
            why = await push_mute_reason(db, d, kind)
            if why:
                logger.info("Push muted (%s, %s on %s)", kind, why, d)
                return True
    return False


async def _morning_fallback() -> None:
    """9:30 AM safety net: run the morning pipeline if no qualifying webhook
    (one carrying today's sleep session) has triggered it yet — but only once that
    sleep session exists; otherwise leave the day unclaimed for the webhook or the
    12:30 final fallback (note 57)."""
    from app.services.morning import run_morning_pipeline
    res = await run_morning_pipeline(trigger="cron-fallback", require_sleep=True)
    logger.info("Scheduler: morning fallback → %s", res.get("status"))


async def _morning_final_fallback() -> None:
    """12:30 PM last resort: run the morning pipeline even without last night's
    sleep (watch not worn, sync broken). The briefing then says sleep hasn't synced
    rather than borrowing an older night."""
    from app.services.morning import run_morning_pipeline
    res = await run_morning_pipeline(trigger="cron-final")
    logger.info("Scheduler: morning final fallback → %s", res.get("status"))


async def _coach_tomorrow() -> None:
    """9 PM: draw tomorrow's coach arm (blind to eligibility — statistically fine,
    the draw is independent of it) and push a provisional call, so gym-first-thing
    mornings already know the plan. Fires EVERY night whether the label comes from
    the random draw or the metric-based rec, so push timing never reveals the
    mechanism. The morning pipeline confirms eligibility at wake."""
    from datetime import timedelta as _td

    from app.services.coach import (
        _ARM_TO_LABEL, draw_for_date, get_active_experiment, get_coach_recommendation,
    )
    from app.timeutils import local_today

    tomorrow = local_today() + _td(days=1)
    # Away today or (pre-planned) tomorrow: no call to make. The draw is skipped for a
    # planned away day too — see coach.draw_for_date.
    if await _muted("nag", local_today(), tomorrow):
        return
    label = None
    async with AsyncSessionLocal() as db:
        # Draws for EVERY running experiment; only the training one has an arm that
        # maps to a coach label, so the notification reports that one.
        exp = await get_active_experiment(db)
        results = await draw_for_date(db, tomorrow)
        if exp is not None:
            arm = next((r.get("arm") for r in results
                        if r.get("experiment_id") == exp.id), None)
            if arm:
                label = _ARM_TO_LABEL.get(arm, arm.upper())
        detail = None
        if label is None:
            # No active training experiment: tomorrow's weekly-plan call (note 29).
            # Tonight only week position and hard stops can shape it — overnight HRV,
            # RHR and sleep don't exist yet, hence "provisional".
            from app.services.coach_plan import plan_for_day
            plan = await plan_for_day(db, tomorrow, morning=True)
            label, detail = plan["label"], plan.get("detail")
            if plan.get("extra"):
                detail = f"{detail} Also: {plan['extra']['detail']}"

    if label and settings.ntfy_url:
        alert = await _presleep_alert()
        body = (f"{detail} " if detail else "") + "Provisional — confirmed at wake with overnight data."
        if alert:
            body = f"⚠ Recovery: {alert[0]}\n\n{body}"
        await _ntfy_post(
            f"Rho for tomorrow: {label}" + (" · recovery alert" if alert else ""),
            body,
            alert[1] if alert else "default",
            tags="warning,crystal_ball" if alert else "crystal_ball",
        )


async def _tv_collect() -> None:
    """Hourly: save finished TV sessions for the Today card (note 85), and Switch minutes."""
    from app.config import settings
    from app.services.switch_play import collect_switch
    from app.services.tv import collect_sessions
    if not (settings.ha_url and settings.ha_token):
        return
    async with AsyncSessionLocal() as db:
        try:
            if settings.ha_tv_entity:
                await collect_sessions(db)
        except Exception as exc:
            logger.warning("Scheduler: TV session collection failed: %s", exc)
        try:
            if settings.ha_switch_entity:
                await collect_switch(db)
        except Exception as exc:
            logger.warning("Scheduler: Switch play time collection failed: %s", exc)


async def _weather_refresh() -> None:
    """Hourly: refresh outdoor conditions and re-upsert today's weather_daily row.
    Re-upserting means the row converges from forecast-inclusive to actuals as the
    day progresses. No-op unless WEATHER_LAT/LON are set."""
    from app.services.weather import is_configured, refresh_weather

    if not is_configured():
        return
    async with AsyncSessionLocal() as db:
        try:
            await refresh_weather(db)
        except Exception as exc:
            logger.error("Scheduler: weather refresh crashed: %s", exc)


async def _screentime_staleness_check() -> None:
    """Daily: warn if the iPhone's screen-time stream has stopped arriving.

    iPhone data reaches us only when iOS syncs its Biome stream to the Mac, on a
    cadence Apple does not document (measured ~13 h behind on 2026-07-19). That
    means "a bit stale" is NORMAL and must not alert — only a stall should.

    A 2026-07-14→16 outage went unnoticed for two days ("Share Across Devices"
    had switched off on the Mac); this is the detector for that class of failure.

    Deliberately iPhone-only: not touching the Mac for 36 h is ordinary (a
    weekend away), so Mac staleness would be a false-positive generator. Going
    36 h without picking up your phone is not plausible, which is what makes the
    signal meaningful for that device.

    Runs server-side rather than in the collector so it still fires when the Mac
    is asleep or off — precisely the situation where collection has stopped.
    """
    if not settings.ntfy_url:
        return
    # iPhone data only reaches us via the Mac at HOME — it stalls for the whole of any
    # trip by construction, and needs a day back for the Mac to catch up.
    from app.timeutils import local_today
    if await _muted("nag", local_today(), local_today() - timedelta(days=1)):
        return
    from sqlalchemy import func, select

    from app.models import ScreentimeEvent

    threshold_h = 36
    async with AsyncSessionLocal() as db:
        newest = (await db.execute(
            select(func.max(ScreentimeEvent.start_time))
            .where(ScreentimeEvent.device == "iPhone")
        )).scalar()

    if newest is None:
        return                              # nothing ever collected — not a stall
    lag_h = (datetime.utcnow() - newest).total_seconds() / 3600
    if lag_h < threshold_h:
        logger.info("Screen-time staleness check OK (iPhone lag %.1f h)", lag_h)
        return

    # The collector now reports WHY it is failing (routers/screentime, note 79) — name
    # that cause instead of the old generic checklist.
    from app.routers.screentime import describe_status
    await _ntfy_post(
        "Screen Time sync stalled",
        f"No new iPhone screen-time data for {lag_h:.0f}h. {describe_status()}",
        "default",
        tags="warning",
    )
    logger.warning("Screen-time staleness alert sent (iPhone lag %.1f h)", lag_h)


async def _input_freshness_check() -> None:
    """Daily: alert on any INPUT that has stopped arriving, not just screen time.

    The screen-time check above is the same idea with a hardcoded 36-hour
    threshold for one stream. This covers the whole frame, with each metric's
    threshold derived from its own observed gaps (services/metric_freshness) —
    `steps` is late after 3 days, `hevy_workout` after 28, and neither number
    had to be chosen by hand.

    Two states are worth waking someone for, and they are not the same problem:
      • `never` — declared and producing NOTHING. A configuration failure, and
        the loudest case: `partner_present` sat at zero rows for six weeks.
      • `stale` — was arriving, has stopped.

    Acknowledged streams are silent by construction (their state is
    `acknowledged`, not `stale`), so confirming a real silence stops the
    notification without stopping the monitoring.
    """
    if not settings.ntfy_url:
        return
    if await _muted("nag"):
        return
    from app.services.metric_freshness import compute_freshness

    async with AsyncSessionLocal() as db:
        rows = await compute_freshness(db)

    never = [f for f in rows if f.state == "never"]
    stale = [f for f in rows if f.state == "stale"]
    if not never and not stale:
        logger.info("Input freshness OK (%d streams live)", len(rows))
        return

    lines = []
    if never:
        lines.append("Never arrived: " + ", ".join(f.label for f in never))
    for f in stale:
        lines.append(f"{f.label}: quiet {f.days_since}d (usually every "
                     f"{f.typical_gap_days:g}d)")
    lines.append("Acknowledge in the app under Insights > Data health "
                 "if the silence is real.")

    await _ntfy_post(
        f"{len(never) + len(stale)} input(s) not arriving",
        "\n".join(lines),
        "default",
        tags="warning",
    )
    logger.warning("Input freshness alert sent — never=%s stale=%s",
                   [f.metric for f in never], [f.metric for f in stale])


async def _nutrition_completeness_check() -> None:
    """Daily: auto-flag past days with incomplete meal logs as `incomplete_data`
    (excluded from analysis) and clear the flag on days since completed. Runs
    over a trailing window so a late backfill re-includes the day."""
    from app.services.nutrition_completeness import sync_recent

    async with AsyncSessionLocal() as db:
        try:
            counts = await sync_recent(db, days_back=14)
            if counts.get("flagged") or counts.get("cleared"):
                logger.info("Nutrition completeness: %s", counts)
        except Exception as exc:
            logger.error("Scheduler: nutrition completeness crashed: %s", exc)


async def _hevy_sync() -> None:
    """Hourly: pull workout updates/deletes from the Hevy API (no-op without HEVY_API_KEY)."""
    if not settings.hevy_api_key:
        return
    from app.services.hevy_sync import run_incremental_sync

    async with AsyncSessionLocal() as db:
        try:
            result = await run_incremental_sync(db)
            if result.get("status") == "error":
                logger.error("Scheduler: Hevy sync failed: %s", result.get("detail"))
        except Exception as exc:
            logger.error("Scheduler: Hevy sync crashed: %s", exc)
    # Class board: compare recent targets with what was just logged (+ learn name aliases).
    try:
        from app.services.class_board import check_boards
        async with AsyncSessionLocal() as db:
            await check_boards(db)
    except Exception as exc:
        logger.error("Scheduler: class board check crashed: %s", exc)


async def _weekly_summary() -> None:
    """Generate AI weekly summary, refresh correlation cache, and push via ntfy on Sunday."""
    from app.services.ai_summaries import generate_weekly_summary
    from app.services.correlations import refresh_correlation_cache
    text = await generate_weekly_summary()
    if text:
        await _send_ntfy_weekly(text)
    try:
        newly_significant = await refresh_correlation_cache(days=365)
        logger.info("Scheduler: correlation cache refreshed")
        if newly_significant:
            await _send_ntfy_correlation_alert(newly_significant)
    except Exception as exc:
        logger.error("Scheduler: correlation cache refresh failed: %s", exc)


async def _monthly_summary() -> None:
    """Generate AI monthly summary on the 1st of each month."""
    from app.services.ai_summaries import generate_monthly_summary
    text = await generate_monthly_summary()
    if text:
        logger.info("Monthly summary generated and stored")
        # Monthly never pushed (note 88): only the log line above knew it existed.
        await _send_ntfy_monthly(text)


async def _afternoon_nudge() -> None:
    """3 PM: push steps progress if still below daily goal and gap is closeable."""
    if not settings.ntfy_url:
        return
    if await _muted("nag"):
        return
    from sqlalchemy import func, select
    from app.models import HealthMetric, UserSettings
    from app.timeutils import local_day_bounds_utc

    async with AsyncSessionLocal() as db:
        user_settings = (await db.execute(select(UserSettings).limit(1))).scalars().first()
        steps_goal = (user_settings.steps_goal if user_settings and user_settings.steps_goal else 10_000)

        today = local_today()
        today_start, tomorrow_start = local_day_bounds_utc(today)
        steps_raw = (await db.execute(
            select(func.sum(HealthMetric.value))
            .where(HealthMetric.metric_type == "steps")
            .where(HealthMetric.timestamp >= today_start)
            .where(HealthMetric.timestamp < tomorrow_start)
        )).scalar()

    steps = round(steps_raw or 0)
    remaining = steps_goal - steps

    if steps >= steps_goal:
        logger.info("Afternoon nudge: steps goal already met (%d), skipping", steps)
        return
    if remaining > 5_500:
        logger.info("Afternoon nudge: gap too large (%d remaining), skipping", remaining)
        return

    pct = int(steps / steps_goal * 100)
    title = f"{steps:,} / {steps_goal:,} steps — {pct}%"
    body = f"{remaining:,} steps to go. An easy 10-15 min walk gets you there."
    await _ntfy_post(title, body, "default", tags="walking")
    logger.info("Afternoon nudge sent: %d steps, %d remaining", steps, remaining)


async def _illness_check() -> None:
    """8:15 AM: composite illness/overtraining early-warning from overnight markers.
    Pushes only when ≥2 of {HRV suppressed, RHR elevated, skin temp elevated, resp rate
    elevated} fire together — co-occurrence is the signal, not any single marker."""
    if not settings.ntfy_url:
        return
    if await _muted("recovery"):
        return
    from app.services.recovery_alarm import assess_recovery_alarm

    async with AsyncSessionLocal() as db:
        alarm = await assess_recovery_alarm(db)

    if alarm["level"] != "elevated":
        logger.info("Illness check: level=%s (%d flagged), no alert",
                    alarm["level"], alarm["flagged_count"])
        return

    flagged = [m for m in alarm["markers"] if m["flagged"]]
    detail = "; ".join(f"{m['label']} ({m['detail']})" for m in flagged)
    title = f"Recovery alert — {alarm['flagged_count']} markers elevated"
    body = f"{detail}. Consider easier training and extra rest; watch for illness onset."
    await _ntfy_post(title, body, "high", tags="warning,thermometer")
    logger.info("Illness check: ELEVATED alert sent — %s", detail)


# Sustained-suppression rule for the pre-sleep HRV alert.
#
# WHAT THIS REPLACED, AND WHY (2026-09-07)
# ----------------------------------------
# The old rule was `hrv_vals[-1] < hrv_vals[0] - 1.5`, in raw milliseconds, over the
# last three rows that HAD an HRV value (not necessarily consecutive days). Three
# things were wrong with it:
#
#   1. It never checked for a decline. It compared the newest reading against the
#      one from three readings ago and ignored the middle value entirely, despite
#      the docstring claiming "3 consecutive days". Only 36% of its fires were
#      actually monotonic.
#   2. The 1.5 ms threshold was about 8% of the noise (the mean absolute
#      night-to-night change in HRV).
#   3. Non-consecutive rows meant a watch gap could silently compare readings a week
#      apart.
#
# Measured over 581 days of history it fired on 47% of nights (53% over the last 60)
# with a fixed piece of advice attached — a coin flip on a channel that also carries
# the 2-of-4-marker illness alert. Alert fatigue there was the real cost.
#
# The replacement uses `hrv_z_score` (the day's HRV against its own trailing 30-day
# series, already computed in metrics_engine) over three CONSECUTIVE calendar days.
# Thresholds were tuned against the full history rather than picked:
#
#     mean z <= -0.5                     19.7%   ~5.4/month
#     all 3 negative AND mean <= -0.75    5.4%   ~1.5/month   <- chosen
#     all 3 below -0.75                   1.3%   ~0.4/month
#
# Requiring every day to be below baseline (not just the average) is what lets the
# message honestly say "three nights running"; a mean-only rule fires on one bad
# night dragging two ordinary ones down.
HRV_SUPPRESSED_DAYS = 3
HRV_SUPPRESSED_MEAN_Z = -0.75

# ACWR alert threshold, raised 1.5 → 2.2 on 2026-09-07 for the same reason as above.
#
# 1.5 is the textbook "danger zone" edge, but it assumes a smoothed 7:28 ratio. This
# user's ACWR has mean 0.99 and SD 0.69, so 1.5 sits near p80 and fired on 19.6% of
# nights (115 of 588, ~6/month). At 2.2 it fires 6.5% (~2/month), just above p90.
#
# UNRESOLVED, AND WORTH A DECISION: ACWR was REMOVED from the readiness score on
# 2026-07-12 partly because it was empirically WRONG-SIGNED for this user — higher load
# predicted BETTER next-day energy (2026-07-10 validation). If that still holds, a
# "consider a rest day" push driven by high ACWR is pointed the wrong way, and the right
# fix is deletion rather than a higher threshold. Tuned rather than removed because
# removing it was not the ask; raise it with the user.
ACWR_ALERT_THRESHOLD = 2.2


async def _presleep_alert() -> "tuple[str, str] | None":
    """The 9 PM recovery check: (message, priority) on sustained HRV suppression or high
    training load, else None. Since 2026-10-21 it is folded into the "Coach tomorrow" push
    (both fired at 21:00 — two notifications a minute apart) instead of pushing alone.

    Sustained = every one of the last HRV_SUPPRESSED_DAYS consecutive calendar days
    sits below the rolling baseline AND the mean z is at or below
    HRV_SUPPRESSED_MEAN_Z. Anchored on today: if today's row is missing the job
    stays quiet rather than alerting on stale data.
    """
    if await _muted("recovery"):
        return None
    from sqlalchemy import select
    from app.models import DailyMetric
    from app.timeutils import local_today

    today = local_today()
    window = [today - timedelta(days=i) for i in range(HRV_SUPPRESSED_DAYS - 1, -1, -1)]

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(DailyMetric)
            .where(DailyMetric.date >= window[0])
            .where(DailyMetric.date <= today)
            .order_by(DailyMetric.date)
        )).scalars().all()

    by_date = {r.date: r for r in rows}
    latest = by_date.get(today)

    # Every day in the window must be present with a z-score, or we cannot claim a
    # sustained anything. A gap is missing data, not a negative finding.
    zs = [by_date[d].hrv_z_score if d in by_date else None for d in window]
    have_full_window = all(z is not None for z in zs)

    suppressed = (
        have_full_window
        and all(z < 0 for z in zs)
        and (sum(zs) / len(zs)) <= HRV_SUPPRESSED_MEAN_Z
    )
    overreaching = (latest is not None and latest.acwr is not None
                    and latest.acwr > ACWR_ALERT_THRESHOLD)

    if not suppressed and not overreaching:
        logger.info(
            "Pre-sleep nudge: no sustained HRV suppression (z=%s) and ACWR nominal (%s), skipping",
            [None if z is None else round(z, 2) for z in zs],
            f"{latest.acwr:.2f}" if latest is not None and latest.acwr is not None else "n/a",
        )
        return None

    parts = []
    if suppressed:
        mean_z = sum(zs) / len(zs)
        z_series = ", ".join(f"{z:+.1f}" for z in zs)
        ms = [by_date[d].hrv_value for d in window]
        ms_str = (" → ".join(f"{v:.0f}" for v in ms) + " ms"
                  if all(v is not None for v in ms) else "")
        baseline = latest.hrv_baseline if latest is not None else None
        vs_base = f" vs {baseline:.0f} baseline" if baseline else ""
        # No generic "sleep more" line: it was static, and 8 h is already the norm
        # here. Report the magnitude and let the training call follow from it.
        parts.append(
            f"HRV below baseline {HRV_SUPPRESSED_DAYS} nights running "
            f"(z {z_series}, mean {mean_z:+.1f}σ"
            + (f"; {ms_str}{vs_base}" if ms_str else "")
            + "). Consider an easier session tomorrow."
        )
    if overreaching:
        parts.append(f"ACWR {latest.acwr:.2f} — consider a rest day tomorrow.")

    logger.info("Pre-sleep alert raised (suppressed=%s, overreaching=%s)", suppressed, overreaching)
    return " ".join(parts), ("high" if overreaching else "default")


# (daily summary push now lives in services/morning.py with the coach-call title)


async def _send_ntfy_weekly(summary_text: str) -> None:
    if not settings.ntfy_url:
        return
    title = "Wellness Weekly Review"
    # Chart markers (note 66) mean nothing in a push — drawn only in the app.
    from app.services.summary_charts import strip_markers
    summary_text = strip_markers(summary_text)
    body = summary_text if len(summary_text) <= 500 else summary_text[:497] + "..."
    await _ntfy_post(title, body, "default", tags="bar_chart")


async def _send_ntfy_monthly(summary_text: str) -> None:
    if not settings.ntfy_url:
        return
    from app.services.summary_charts import strip_markers
    summary_text = strip_markers(summary_text)
    body = summary_text if len(summary_text) <= 500 else summary_text[:497] + "..."
    await _ntfy_post("Wellness Monthly Review", body, "default", tags="calendar")


async def _send_ntfy_correlation_alert(insights: list[dict]) -> None:
    """Push a nudge when correlations newly cross the significance threshold."""
    if not settings.ntfy_url or not insights:
        return
    count = len(insights)
    title = f"{count} new wellness insight{'s' if count > 1 else ''} found"
    lines = [item["sentence"] + f"  (r={item['abs_r']:.2f}, n={item['n']})"
             for item in insights[:5]]
    body = "\n".join(lines) + "\n\nCheck the Trends tab for details."
    await _ntfy_post(title, body, "default", tags="chart_with_upwards_trend")
    logger.info("Correlation alert sent: %d insight(s)", count)


async def _ntfy_post(title: str, body: str, priority: str, tags: str = "muscle") -> None:
    """Post a notification via ntfy using HTTP headers (ASCII-only title/tags).

    Deliberately sets no Click action: tapping a notification should open it in ntfy,
    not deep-link into the app.
    """
    import httpx
    url = settings.ntfy_url.rstrip("/") + "/" + settings.ntfy_topic
    try:
        headers = {
            "Title": title.encode("ascii", errors="ignore").decode(),
            "Priority": priority,
            "Tags": tags,
        }
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            resp = await client.post(
                url,
                content=body.encode("utf-8"),
                headers=headers,
            )
            resp.raise_for_status()
            logger.info("ntfy push sent → %s (HTTP %s)", url, resp.status_code)
    except Exception as exc:
        logger.error("ntfy push failed: %s", exc)


async def _tag_new_foods() -> None:
    """Classify any newly-logged food items.

    Runs on a schedule rather than inline at log time so a slow or failing API call
    can never block or delay logging a meal. An item missed today is simply picked
    up tomorrow; until then it counts as untagged, which the views report honestly
    rather than silently omitting.
    """
    from app.database import AsyncSessionLocal
    from app.services import food_tags
    try:
        async with AsyncSessionLocal() as db:
            result = await food_tags.run_tagging(db)
        if result["tagged"]:
            logger.info("Food tagging: %s", result)
    except Exception as exc:
        logger.error("Food tagging job failed: %s", exc)


async def _metric_rollup() -> None:
    """Collapse settled high-frequency metrics: resting_calories to one row per day,
    active calories / steps / distance / exercise minutes to one row per hour.

    Storage housekeeping only — every number the app computes is unchanged. Runs well
    after the morning pipeline so it never competes with ingestion, and only touches days
    older than ROLLUP_LAG_DAYS. Scans the last 60 days (re-sends are recent); after a big
    historical HAE backfill run `rollup_old_metrics(db)` once by hand for a full pass.
    """
    from datetime import timedelta as _td
    from app.database import AsyncSessionLocal
    from app.services.metric_rollup import rollup_old_metrics
    from app.timeutils import local_today
    try:
        async with AsyncSessionLocal() as db:
            res = await rollup_old_metrics(db, since=local_today() - _td(days=60))
        removed = sum(m["rows_removed"] for m in res["metrics"].values())
        if removed:
            logger.info("Metric rollup: %s", res)
    except Exception as exc:
        logger.error("Metric rollup job failed: %s", exc)


_tz_prompt_pushed_on = None


async def _tz_refresh() -> None:
    """Every 15 min (and once at boot): carry an answered trip zone forward, re-resolve
    the current zone (moves "today", re-registers cron jobs on a change), and — at most
    once a day — push "where are you?" when services/tz.prompt_state says to ask.
    The push is deliberately NOT muted while away: being away is when it matters."""
    global _tz_prompt_pushed_on
    from app.services.tz import extend_open_periods, prompt_state, refresh_current_zone
    from app.timeutils import local_now, local_today
    async with AsyncSessionLocal() as db:
        try:
            await extend_open_periods(db)
        except Exception as exc:
            logger.error("TZ: extending periods failed: %s", exc)
        await refresh_current_zone(db)
        try:
            st = await prompt_state(db)
        except Exception as exc:
            logger.error("TZ: prompt state failed: %s", exc)
            return
    today = local_today()
    # 08:00–22:00 local only, once per day (in-memory: a restart may re-ask once).
    if (st.get("needed") and settings.ntfy_url and _tz_prompt_pushed_on != today
            and 8 <= local_now().hour < 22):
        off = st.get("offset_min")
        where = (f"Your phone is on UTC{off / 60:+g}. " if off is not None else
                 "No time-zone signal from your phone today. ")
        await _ntfy_post("Traveling? Where are you?",
                         where + "Open the app and pick your time zone so today is "
                         "logged on the right day.", "default", tags="globe_with_meridians")
        _tz_prompt_pushed_on = today
        logger.info("TZ: 'where are you?' push sent (%s)", st.get("reason"))


def follow_zone(tz) -> None:
    """Re-register every cron job in `tz` (time-zone awareness, phase 2). Called when
    the user's current zone changes, so the 09:30 fallback, 21:00 coach call, 15:00
    nudge … fire at LOCAL time abroad instead of Eastern. replace_existing keeps job
    ids stable. A job whose local time has already passed today in the new zone simply
    waits for tomorrow; the jobs are idempotent (morning pipeline claims its day)."""
    _register_cron_jobs(tz)
    logger.warning("Scheduler: cron jobs re-registered in %s", getattr(tz, "key", tz))


def _register_cron_jobs(tz) -> None:
    scheduler.add_job(
        _metric_rollup,
        CronTrigger(hour=4, minute=20, timezone=tz),
        id="metric_rollup",
        replace_existing=True,
    )
    # Morning work is event-driven (webhook carrying today's sleep session →
    # services/morning.run_morning_pipeline); this cron is only the safety net.
    scheduler.add_job(
        _morning_fallback,
        CronTrigger(hour=9, minute=30, timezone=tz),
        id="morning_fallback",
        replace_existing=True,
    )
    scheduler.add_job(
        _morning_final_fallback,
        CronTrigger(hour=12, minute=30, timezone=tz),
        id="morning_final_fallback",
        replace_existing=True,
    )
    scheduler.add_job(
        _coach_tomorrow,
        CronTrigger(hour=21, minute=0, timezone=tz),
        id="coach_tomorrow",
        replace_existing=True,
    )
    scheduler.add_job(
        _weekly_summary,
        CronTrigger(day_of_week="sun", hour=12, minute=0, timezone=tz),
        id="weekly_summary",
        replace_existing=True,
    )
    scheduler.add_job(
        _monthly_summary,
        CronTrigger(day=1, hour=8, minute=10, timezone=tz),
        id="monthly_summary",
        replace_existing=True,
    )
    scheduler.add_job(
        _afternoon_nudge,
        CronTrigger(hour=15, minute=0, timezone=tz),
        id="afternoon_nudge",
        replace_existing=True,
    )
    # (presleep_nudge removed 2026-10-21: its alert now rides in the coach_tomorrow push)
    if scheduler.get_job("presleep_nudge"):
        scheduler.remove_job("presleep_nudge")
    # Once daily, not hourly: a stall persists, so re-checking often would only
    # turn one useful warning into a stream of identical ones. Daily re-firing
    # while stalled is intentional — a single missed push shouldn't lose the alert.
    scheduler.add_job(
        _screentime_staleness_check,
        CronTrigger(hour=11, minute=0, timezone=tz),
        id="screentime_staleness",
        replace_existing=True,
    )
    # Ten minutes later so the two never post at once. Kept as a separate job
    # rather than folded in: the screen-time check knows things this one cannot
    # (Share Across Devices, the launchd collector) and its remediation text is
    # worth keeping specific.
    scheduler.add_job(
        _input_freshness_check,
        CronTrigger(hour=11, minute=10, timezone=tz),
        id="input_freshness",
        replace_existing=True,
    )
    # Late morning: yesterday has rolled over, so judge its meal completeness.
    scheduler.add_job(
        _nutrition_completeness_check,
        CronTrigger(hour=10, minute=30, timezone=tz),
        id="nutrition_completeness",
        replace_existing=True,
    )
    # Early morning: yesterday's items exist and nothing else is competing for the API.
    scheduler.add_job(
        _new_error_kinds_push,
        CronTrigger(hour=11, minute=20, timezone=tz),
        id="new_error_kinds",
        replace_existing=True,
    )
    scheduler.add_job(
        _tag_new_foods,
        CronTrigger(hour=4, minute=20, timezone=tz),
        id="food_tagging",
        replace_existing=True,
    )


async def _flush_error_ledger() -> None:
    """Every 5 min: write the in-memory error counts to error_events (app/error_ledger.py)."""
    from app import error_ledger
    async with AsyncSessionLocal() as db:
        try:
            await error_ledger.flush(db)
        except Exception as exc:
            # WARNING, not ERROR: an ERROR here would feed the very ledger that failed.
            logger.warning("Error ledger flush failed: %s", exc)


async def _new_error_kinds_push() -> None:
    """11:20 daily: push once if a KIND of error appeared that wasn't seen in 30 days.
    Routine repeats stay on Data Health; only something new interrupts you."""
    if not settings.ntfy_url or await _muted("nag"):
        return
    from app import error_ledger
    async with AsyncSessionLocal() as db:
        await error_ledger.flush(db)
        kinds = await error_ledger.new_kinds(db)
    if not kinds:
        return
    lines = [f"• {k['logger'].replace('app.', '')}: {k['sample'][:140]}" for k in kinds[:5]]
    await _ntfy_post(f"{len(kinds)} new kind{'s' if len(kinds) != 1 else ''} of error",
                     "\n".join(lines) + "\nDetails: Insights → Signals → Data health.",
                     "default", tags="warning")


async def _wal_checkpoint() -> None:
    """01:45 home time: fold the SQLite WAL back into the main file (TRUNCATE), so the
    02:00 sqlite .backup and the 03:00 restic file copy both see a complete database.
    restic copies raw files, and in WAL mode the main file alone can lag the -wal."""
    from sqlalchemy import text
    async with AsyncSessionLocal() as db:
        try:
            from app.idempotency import prune as _prune_keys
            n = await _prune_keys(db)            # offline-queue keys older than 14 days
            r = (await db.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))).first()
            logger.info("WAL checkpoint: busy=%s log=%s checkpointed=%s (pruned %d idempotency keys)",
                        *r, n)
        except Exception as exc:
            logger.error("WAL checkpoint failed: %s", exc)


async def _resume_parse_jobs() -> None:
    """At boot: re-run background food parses a restart interrupted (2026-10-21)."""
    from sqlalchemy import select
    from app.models import ParseJob
    from app.routers.food_log import run_parse_job
    async with AsyncSessionLocal() as db:
        ids = [r[0] for r in (await db.execute(
            select(ParseJob.id).where(ParseJob.status == "pending"))).all()]
    for i in ids:
        await run_parse_job(i)
    if ids:
        logger.info("Resumed %d interrupted parse job(s)", len(ids))


def start_scheduler() -> None:
    from app.timeutils import current_tz
    _register_cron_jobs(current_tz())
    scheduler.add_job(_resume_parse_jobs, id="resume_parse_jobs", replace_existing=True,
                      next_run_time=datetime.now(ZoneInfo(settings.tz)))
    # Pinned to the HOME zone (not re-registered by follow_zone): it serves the server's
    # cron backups, which run on the server's clock wherever the user is.
    from apscheduler.triggers.interval import IntervalTrigger as _Interval
    scheduler.add_job(_tv_collect, _Interval(hours=1), id="tv_collect", replace_existing=True)
    scheduler.add_job(_flush_error_ledger, _Interval(minutes=5), id="error_ledger_flush",
                      replace_existing=True)
    scheduler.add_job(
        _wal_checkpoint,
        CronTrigger(hour=1, minute=45, timezone=ZoneInfo(settings.tz)),
        id="wal_checkpoint",
        replace_existing=True,
    )
    from apscheduler.triggers.interval import IntervalTrigger
    scheduler.add_job(
        _tz_refresh,
        IntervalTrigger(minutes=15),
        id="tz_refresh",
        replace_existing=True,
        next_run_time=datetime.now(ZoneInfo(settings.tz)),   # resolve the zone on boot
    )
    if settings.hevy_api_key:
        scheduler.add_job(
            _hevy_sync,
            IntervalTrigger(hours=1),
            id="hevy_sync",
            replace_existing=True,
        )
    from app.services.weather import is_configured as _weather_configured
    if _weather_configured():
        scheduler.add_job(
            _weather_refresh,
            IntervalTrigger(hours=1),
            id="weather_refresh",
            replace_existing=True,
            next_run_time=datetime.now(ZoneInfo(settings.tz)),   # populate immediately on boot
        )
    scheduler.start()
    logger.info(
        "APScheduler started — morning pipeline: webhook-triggered (fallbacks 09:30 if sleep synced, 12:30 always), "
        "coach draw 21:00, weekly summary Sun 12:00, monthly summary 1st 08:10, "
        "afternoon nudge 15:00 (pre-sleep alert folded into coach 21:00), screentime staleness 11:00, "
        "nutrition completeness 10:30, hevy sync %s, weather %s (%s)",
        "hourly" if settings.hevy_api_key else "disabled (no key)",
        "hourly" if _weather_configured() else "disabled (no lat/lon)",
        settings.tz,
    )


def stop_scheduler() -> None:
    scheduler.shutdown(wait=False)
    logger.info("APScheduler stopped")
