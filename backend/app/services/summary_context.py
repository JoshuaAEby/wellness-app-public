"""
Shared context builders for the AI summaries (daily / weekly / monthly).

These assemble the *personalized* and *trend* context that turns a generic
"ACWR dropped, rest" summary into "this matches your pattern where…". Each public
builder is fail-safe: any error returns an empty string so an enrichment problem
never blocks summary generation. Blocks are plain text appended to the user message.
"""
import calendar
import logging
import re
import statistics as stats
from datetime import date, datetime, time, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.timeutils import local_today
from app.foodday import food_date_sql, food_day_start, food_day_end

logger = logging.getLogger(__name__)


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return stats.mean(xs) if xs else None


# ── Personalized patterns (correlation engine) ─────────────────────────────────

async def _training_rest_contrast(db: AsyncSession) -> str:
    """Readiness / subjective-energy contrast on training vs rest days (all history)."""
    from app.models import DailyMetric, SubjectiveLog, Workout
    from app.workout_categories import LOW_INTENSITY_TYPES

    from app.timeutils import local_date_sql

    wo = (await db.execute(
        select(local_date_sql(Workout.start_time))
        .where(Workout.activity_type.notin_(LOW_INTENSITY_TYPES))
        .distinct()
    )).all()
    training_dates = {date.fromisoformat(str(r[0])) for r in wo}
    if not training_dates:
        return ""

    parts = []
    r_rows = (await db.execute(
        select(DailyMetric.date, DailyMetric.readiness_score)
        .where(DailyMetric.readiness_score.isnot(None))
    )).all()
    tr = [v for d, v in r_rows if d in training_dates]
    rest = [v for d, v in r_rows if d not in training_dates]
    if len(tr) >= 8 and len(rest) >= 8:
        parts.append(f"readiness {_mean(tr):.0f} on training days vs {_mean(rest):.0f} on rest")

    e_rows = (await db.execute(
        select(SubjectiveLog.date, SubjectiveLog.energy)
        .where(SubjectiveLog.energy.isnot(None))
    )).all()
    etr = [v for d, v in e_rows if d in training_dates]
    erest = [v for d, v in e_rows if d not in training_dates]
    if len(etr) >= 8 and len(erest) >= 8:
        parts.append(f"energy {_mean(etr):.1f} vs {_mean(erest):.1f}/5")

    return "Training vs rest days: " + "; ".join(parts) if parts else ""


async def personal_patterns_block(db: AsyncSession) -> str:
    """
    The headline personalization: confirmed/suggestive levers, a dose-response band,
    and the training-vs-rest contrast — all from the user's own correlation engine.
    """
    try:
        from app.services.correlations import (
            PRIVATE_METRICS, SIGNIFICANCE_THRESHOLD, _SUBJECTIVE_OUTCOME_KEYS,
            build_actionable_insights, compute_all_threshold_bands,
            read_correlation_cache,
        )

        cached = await read_correlation_cache()
        # Drop private metrics BEFORE anything is formatted. This block is the one
        # place correlation output crosses into an LLM prompt, so the filter belongs
        # here rather than in the engine — the findings stay visible in the app.
        cached = [r for r in (cached or [])
                  if r.get("metric_a") not in PRIVATE_METRICS
                  and r.get("metric_b") not in PRIVATE_METRICS]
        lines: list[str] = []

        if cached:
            act = build_actionable_insights(cached, min_abs_r=SIGNIFICANCE_THRESHOLD, min_n=30)

            def fmt(items):
                out = []
                for it in items:
                    if (it.get("input_key") in PRIVATE_METRICS
                            or it.get("outcome_key") in PRIVATE_METRICS):
                        continue
                    ci = ""
                    if it.get("ci_low") is not None and it.get("ci_high") is not None:
                        ci = f", 95%CI [{it['ci_low']:.2f},{it['ci_high']:.2f}]"
                    # Note 30: proxy-only rows now reach the suggestive list; say so, so
                    # the model never presents one as corroborated by how he feels.
                    tag = " [body markers only; nothing he feels corroborates it]" if it.get("proxy_only") else ""
                    out.append(f"  • {it['sentence']} (ρ={it['raw_r']:+.2f}, n={it['n']}{ci}){tag}")
                return out

            confirmed = (act["do_more"] + act["do_less"])
            suggestive = (act["do_more_suggestive"] + act["do_less_suggestive"])
            diag = (act["diagnostic"] + act["diagnostic_suggestive"])
            confirmed.sort(key=lambda x: -x["abs_r"])
            # Non-proxy rows first (note 30): proxy-only rows now reach this list, and
            # sorting on |rho| alone put "less active-calorie burn" and "more screen
            # time" at the top of the three slots — against the weekly-plan coach.
            suggestive.sort(key=lambda x: (bool(x.get("proxy_only")), -x["abs_r"]))
            diag.sort(key=lambda x: -x["abs_r"])

            if confirmed:
                lines.append("Confirmed levers (statistically robust — prioritise):")
                lines += fmt(confirmed[:5])
            if suggestive:
                lines.append("Suggestive (emerging, needs more data):")
                lines += fmt(suggestive[:3])
            if diag:
                lines.append("Diagnostic associations (context, not directives):")
                lines += fmt(diag[:3])
            # CONFOUNDED (reverse-lag, 2026-10-27): still worth knowing, never advice.
            conf = act.get("confounded", [])
            if conf:
                lines.append("CONFOUNDED associations — the same link shows up the day BEFORE "
                             "the input too, so a third factor (or the outcome itself) drives it. "
                             "Context only; NEVER present one as a lever or a recommendation:")
                lines += fmt(conf[:3])

            # One dose-response band (first confirmed subjective pair with enough data)
            confirmed_pairs = [
                (r["metric_a"], r["metric_b"], r["lag_days"])
                for r in cached
                if (r.get("significant") or r.get("suggestive"))
                and abs(r["spearman_r"] or 0) >= SIGNIFICANCE_THRESHOLD
                and (r["n"] or 0) >= 30
                and r["metric_b"] in _SUBJECTIVE_OUTCOME_KEYS
                and r.get("reverse_check") != "confounded"
            ]
            for b in await compute_all_threshold_bands(db, confirmed_pairs[:3]):
                if b.get("sufficient_data"):
                    lo, _mid, hi = b["bins"]
                    lines.append(
                        f"Dose-response — {b['metric_a_label']} → {b['metric_b_label']}: "
                        f"low ({lo['lo']}–{lo['hi']}) avg {lo['avg_outcome']} vs "
                        f"high ({hi['lo']}–{hi['hi']}) avg {hi['avg_outcome']}"
                    )
                    break

        contrast = await _training_rest_contrast(db)
        if contrast:
            lines.append(contrast)

        if not lines:
            return ""
        return (
            "YOUR PERSONAL PATTERNS (from your own correlation engine — these are the "
            "point of this report; ground recommendations in them rather than generic advice):\n"
            + "\n".join(lines) + "\n\n"
        )
    except Exception as exc:
        logger.warning("personal_patterns_block failed: %s", exc)
        return ""


# ── Today's own flags, stated AT the numbers (2026-10-28, note 55 audit) ─────────
# The 14-day NONSTANDARD DAYS list and the FLAGGED DAYS prompt rule were both present on
# 10-21/22 — post-trip recovery days — yet the briefing raised "Alarm WATCH — HRV
# suppressed" without a word about the trip. Like the in-range z note, the fact has to sit
# next to the number and the alarm it explains.
_EXPLAINING_FLAGS = ("sick", "travel", "travel_recovery", "high_stress", "poor_sleep_env")


async def class_result_block(db: AsyncSession, day: date) -> str:
    """Yesterday's class: Rho's board targets vs what Hevy says he lifted (2026-10-29).

    Only classes photographed through the class board and checked after a Hevy sync
    (class_board.check_boards). Gives Rho something REAL to credit; an under is reported
    as a fact, never as a failing."""
    try:
        from app.models import ClassBoard
        from app.services.class_board import summarize_outcome
        row = (await db.execute(
            select(ClassBoard).where(ClassBoard.date == day - timedelta(days=1))
            .where(ClassBoard.checked_at.isnot(None))
            .order_by(ClassBoard.id.desc()).limit(1))).scalar_one_or_none()
        if row is None:
            return ""
        s = summarize_outcome(row.outcome or [])
        if not s["judged"]:
            return ""
        def line(o):
            unit = " band" if o.get("unit") == "lb assist" else ""
            return f"{o['title']} {o['actual_lb']:g} lb{unit} vs target {o['target_lb']:g} — {o['verdict']}"
        judged = [o for o in row.outcome if o.get("verdict") in ("beat", "hit", "under")]
        easy = " (he chose EASY DAY)" if row.easy else ""
        return (f"YESTERDAY'S CLASS VS RHO'S TARGETS ({row.date}){easy}: "
                f"{s['at_or_above']} of {s['judged']} at or above target\n"
                + "".join(f"  - {line(o)}\n" for o in judged) + "\n")
    except Exception as exc:
        logger.warning("class_result_block failed: %s", exc)
        return ""


async def today_flag_note(db: AsyncSession, day: date) -> str:
    """'' unless today or yesterday carries a flag that explains recovery markers, or a
    health event (a recent vaccine, say) that may contribute to them."""
    parts = []
    try:
        from app.services.day_flags import REASON_LABELS, flags_in_range
        flags = await flags_in_range(db, day - timedelta(days=1), day)
        reasons = set(flags.get(day, [])) | set(flags.get(day - timedelta(days=1), []))
        hits = [REASON_LABELS.get(r, r) for r in _EXPLAINING_FLAGS if r in reasons]
        if hits:
            parts.append(f"FLAGGED today/yesterday: {', '.join(hits)} — the leading explanation "
                         f"for any change in HRV, RHR, readiness or the alarm. Say so; do not "
                         f"name a lever.")
        from app.services.health_events import recent_event_note
        event = await recent_event_note(db, day)
        if event:
            parts.append(event)
    except Exception as exc:  # noqa: BLE001
        logger.warning("today_flag_note failed: %s", exc)
    return " ".join(parts)


# ── Recovery / illness alarm (daily) ───────────────────────────────────────────

async def recovery_alarm_block(db: AsyncSession) -> str:
    try:
        from app.services.recovery_alarm import assess_recovery_alarm
        a = await assess_recovery_alarm(db)
        if not a:
            return ""
        flagged = [m for m in a["markers"] if m["flagged"]]
        detail = "; ".join(f"{m['label']} ({m['detail']})" for m in flagged) if flagged else "none flagged"
        note = await today_flag_note(db, local_today())
        return (
            # The level's meaning is spelled out: a briefing called a 1-marker WATCH "elevated"
            # (2026-11-04 dry run); the marker labels themselves say "elevated".
            f"ILLNESS/OVERTRAINING ALARM: {a['level'].upper()} "
            f"({a['flagged_count']}/4 overnight markers; ELEVATED needs 2+, WATCH is 1) — {detail}"
            + (f"\n  [{note}]" if note else "") + "\n\n"
        )
    except Exception as exc:
        logger.warning("recovery_alarm_block failed: %s", exc)
        return ""


# ── Energy balance (daily) ─────────────────────────────────────────────────────

async def energy_block(db: AsyncSession) -> str:
    try:
        from app.services.energy import latest_adaptive_tdee

        a = await latest_adaptive_tdee(db)
        if not a:
            return ""
        wk = a.get("weekly_weight_change_kg")
        wk_str = f"{wk * 2.20462:+.2f} lb/wk" if wk is not None else "n/a"
        return (
            f"ENERGY BALANCE (adaptive, weight-calibrated, {a.get('span_days')}d window): "
            f"TDEE ≈ {a['tdee']} kcal/day · mean intake {a.get('mean_intake')} kcal · "
            f"weight trend {wk_str} · confidence {a.get('confidence')}\n\n"
        )
    except Exception as exc:
        logger.warning("energy_block failed: %s", exc)
        return ""


# ── Multi-window trend + biggest movers (daily) ────────────────────────────────

async def daily_trend_block(db: AsyncSession) -> str:
    try:
        from app.models import DailyMetric
        rows = list(reversed((await db.execute(
            select(DailyMetric).order_by(DailyMetric.date.desc()).limit(90)
        )).scalars().all()))
        if len(rows) < 14:
            return ""

        def mean_last(attr, n):
            return _mean([getattr(r, attr) for r in rows[-n:]])

        lines = ["MULTI-WINDOW TREND (the actual trajectory — not a one-day blip):"]
        for attr, label, spec in [("readiness_score", "Readiness", ".0f"), ("hrv_value", "HRV (ms)", ".0f")]:
            m7, m28, m90 = mean_last(attr, 7), mean_last(attr, 28), mean_last(attr, 90)
            if None not in (m7, m28, m90):
                lines.append(f"  {label}: 7d {m7:{spec}} · 28d {m28:{spec}} · 90d {m90:{spec}}")

        # Biggest movers: today's value vs its own 90-day distribution (in SDs).
        today = rows[-1]
        movers = []
        for attr, label, spec in [
            ("readiness_score", "Readiness", ".0f"), ("hrv_value", "HRV", ".0f"),
            ("rhr_value", "RHR", ".0f"), ("acwr", "ACWR", ".2f"),
        ]:
            vals = [getattr(r, attr) for r in rows if getattr(r, attr) is not None]
            cur = getattr(today, attr)
            if cur is not None and len(vals) >= 10:
                mu, sd = stats.mean(vals), stats.pstdev(vals)
                if sd > 0:
                    movers.append((abs((cur - mu) / sd), label, cur, (cur - mu) / sd, spec))
        movers.sort(reverse=True)
        if movers:
            lines.append(
                "  Most unusual vs 90-day norm: "
                + "; ".join(f"{l} {c:{sp}} ({z:+.1f} SD)" for _, l, c, z, sp in movers[:2])
            )
        return "\n".join(lines) + "\n\n"
    except Exception as exc:
        logger.warning("daily_trend_block failed: %s", exc)
        return ""


# ── Period vs longer baseline (weekly / monthly) ───────────────────────────────

async def baseline_block(db: AsyncSession, win_start: date, win_end: date,
                         base_days: int, label: str) -> str:
    """Compare the window's averages to a trailing baseline of `base_days` before it."""
    try:
        from app.models import DailyMetric, SleepSession
        base_start = win_start - timedelta(days=base_days)
        dm = (await db.execute(
            select(DailyMetric).where(DailyMetric.date >= base_start).where(DailyMetric.date <= win_end)
        )).scalars().all()
        sl = (await db.execute(
            select(SleepSession).where(SleepSession.date >= base_start).where(SleepSession.date <= win_end)
        )).scalars().all()

        win = [r for r in dm if win_start <= r.date <= win_end]
        base = [r for r in dm if r.date < win_start]
        if len(win) < 3 or len(base) < 7:
            return ""

        def avg(rows, attr):
            return _mean([getattr(r, attr) for r in rows])

        def savg(rows):
            return (_mean([s.total_min for s in rows]) or 0) / 60 if rows else None

        lines = [f"VS {label} BASELINE (is this period above or below your recent norm?):"]
        for attr, lab, spec in [
            ("readiness_score", "Readiness", ".0f"),
            ("hrv_z_score", "HRV z", "+.2f"),
            ("rhr_z_score", "RHR z", "+.2f"),
        ]:
            w, b = avg(win, attr), avg(base, attr)
            if w is not None and b is not None:
                lines.append(f"  {lab}: {w:{spec}} vs {b:{spec}} baseline (Δ {w - b:+.2f})")
        sw = savg([s for s in sl if win_start <= s.date <= win_end])
        sb = savg([s for s in sl if s.date < win_start])
        if sw and sb:
            lines.append(f"  Sleep: {sw:.1f}h vs {sb:.1f}h baseline (Δ {sw - sb:+.1f}h)")
        return "\n".join(lines) + "\n\n" if len(lines) > 1 else ""
    except Exception as exc:
        logger.warning("baseline_block failed: %s", exc)
        return ""


# ── Prior summaries (anti-repetition, Tier 3) ──────────────────────────────────

# ── Prior summaries: CLAIMS ONLY, not the whole essay ─────────────────────────
#
# TRIMMED 2026-09-09. These blocks used to paste each prior summary in FULL — 1,353
# tokens for two weeks, the single largest input the weekly received, against 60 tokens
# of actual baseline. Two costs:
#   1. It crowded out data. See period_series_block for what replaced it.
#   2. It ANCHORED the model to its own narrative. Given 1,300 tokens of its own prose
#      and told "don't repeat", it continued the story instead — "the Thursday cascade"
#      compounded across three consecutive weeks, each summary building on the last
#      one's claim rather than re-testing it.
#
# The prompt's REVISITING line needs the prior CLAIM, not the prior essay. So: headline
# and the one-thing only. ~150 tokens instead of ~1,350, and what survives is exactly
# the part that can be checked against new data.

_CLAIM_LABELS = ("**HEADLINE:**", "**THE ONE THING:**")


def _claims_only(text: str) -> str:
    """The HEADLINE and THE ONE THING lines, dropping the rest of the report."""
    if not text:
        return ""
    out = []
    for raw in text.split("\n"):
        line = raw.strip()
        if any(line.startswith(lbl) for lbl in _CLAIM_LABELS):
            out.append(line)
    # Fall back to the first non-empty line rather than returning nothing if a past
    # summary predates this format — an unparsed claim is better than a silent gap.
    return "\n".join(out) if out else next(
        (l.strip() for l in text.split("\n") if l.strip()), "")


async def prior_weekly_block(db: AsyncSession, before: date, n: int = 2) -> str:
    try:
        from app.models import WeeklySummary
        rows = (await db.execute(
            select(WeeklySummary).where(WeeklySummary.week_start < before)
            .order_by(WeeklySummary.week_start.desc()).limit(n)
        )).scalars().all()
        if not rows:
            return ""
        parts = [f"--- week of {r.week_start} ---\n{_claims_only(r.summary_text)}"
                 for r in reversed(rows)]
        return (
            "WHAT YOU CLAIMED IN PRIOR WEEKS (headline + recommendation only). TEST the "
            "strongest of these against this week's data rather than continuing it — a "
            "claim that has stopped holding is the most useful thing you can report:\n"
            + "\n\n".join(parts) + "\n\n"
        )
    except Exception as exc:
        logger.warning("prior_weekly_block failed: %s", exc)
        return ""


async def prior_monthly_block(db: AsyncSession, before_year_month: str, n: int = 2) -> str:
    try:
        from app.models import MonthlySummary
        rows = (await db.execute(
            select(MonthlySummary).where(MonthlySummary.year_month < before_year_month)
            .order_by(MonthlySummary.year_month.desc()).limit(n)
        )).scalars().all()
        if not rows:
            return ""
        parts = [f"--- {r.year_month} ---\n{_claims_only(r.summary_text)}"
                 for r in reversed(rows)]
        return (
            "WHAT YOU CLAIMED IN PRIOR MONTHS (headline + recommendation only). TEST the "
            "strongest of these against this month's data rather than continuing it:\n"
            + "\n\n".join(parts) + "\n\n"
        )
    except Exception as exc:
        logger.warning("prior_monthly_block failed: %s", exc)
        return ""


# ── Yesterday's actual inputs (note 55, 2026-09-23) ──────────────────────────
#
# The 2026-09-20 briefing was handed "New: caffeine at bedtime -> sleep quality" with
# NO caffeine data, so it wrote "if taken late, may explain..." — the dose had been
# logged at 11:37, twelve hours before bed. This block gives the briefing yesterday's
# real value for every controllable input, against the athlete's own usual range, so
# a finding can be checked instead of guessed at.

# Inputs keyed to the WAKE date in the correlation series (the night that ended this
# morning) — read at today's key, reported as "last night". Everything else is read at
# yesterday's key. caffeine/alcohol_onboard_bed are keyed to the DRINKING day (they
# look forward to that night's bedtime), so they are yesterday's.
_LAST_NIGHT_KEYS = ("screen_evening", "tv_prebed_min")
_SKIP_INPUTS = {"acwr", "zone2_pct_7d", "zone3_pct_7d", "high_intensity_pct_7d"}  # in TODAY already
USUAL_DAYS = 90


def _fmt_val(v: float) -> str:
    if v is None:
        return "—"
    return f"{v:.0f}" if abs(v) >= 10 else f"{v:.1f}" if abs(v) >= 1 else f"{v:.2f}"


async def yesterday_inputs(db: AsyncSession, today: date) -> dict:
    """{metric: {label, value, p10, p50, p90, unusual, when}} for yesterday's controllable
    inputs, plus {"_flags": [...]} for yesterday's day flags. Private and context
    (weather, partner presence...) inputs are never included."""
    from app.models import DayFlag
    from app.services.correlations import (
        CONTEXT_INPUTS, METRIC_LABELS, PRIVATE_METRICS, _DAY_INPUTS,
        _fetch_daily_series, _fetch_daily_series_uncached)

    yday = today - timedelta(days=1)
    usual, _ = await _fetch_daily_series(db, today - timedelta(days=USUAL_DAYS), yday - timedelta(days=1))
    recent, _ = await _fetch_daily_series_uncached(db, yday, today, keep_excluded=True)
    keys = [k for k in list(_DAY_INPUTS) + ["caffeine_onboard_bed", "alcohol_onboard_bed"]
            + list(_LAST_NIGHT_KEYS)
            if k not in CONTEXT_INPUTS and k not in PRIVATE_METRICS and k not in _SKIP_INPUTS
            and not k.startswith("ctrl_")]
    out: dict = {}
    for k in dict.fromkeys(keys):
        at = today if k in _LAST_NIGHT_KEYS else yday
        v = (recent.get(at) or {}).get(k)
        hist = sorted(float(r[k]) for r in usual.values() if r.get(k) is not None)
        entry = {"label": METRIC_LABELS.get(k, k), "value": v,
                 "when": "last night" if k in _LAST_NIGHT_KEYS else "yesterday"}
        if len(hist) >= 10:
            q = lambda f: hist[min(len(hist) - 1, int(f * (len(hist) - 1) + 0.5))]
            entry.update(p10=q(0.1), p50=q(0.5), p90=q(0.9))
            if v is not None:
                binary = set(hist) <= {0.0, 1.0}
                entry["binary"] = binary
                if binary:
                    entry["yes_share"] = sum(1 for x in hist if x == 1.0) / len(hist)
                    share = sum(1 for x in hist if x == float(v)) / len(hist)
                    entry["unusual"] = share < 0.2
                else:
                    entry["unusual"] = v < entry["p10"] or v > entry["p90"]
        out[k] = entry
    flags = (await db.execute(select(DayFlag.reason).where(DayFlag.date == yday))).scalars().all()
    out["_flags"] = sorted(set(flags))
    return out


def _link_note(links: Optional[dict], key: str, direction: int = 0) -> str:
    """`direction`: +1 if yesterday was ABOVE usual (or yes), −1 if below (or no), 0 unknown.

    Each finding is printed with what YESTERDAY'S value implies for its outcome (2026-10-28):
    a dry run explained a raised RHR with "a calorie deficit" — the finding says
    MORE calories go with HIGHER RHR, so a deficit points the other way.
    """
    if links is None:
        return ""
    found = links.get(key)
    if not found:
        return "\n      no finding links this input to any outcome — state it as a fact, never as a cause"
    parts = []
    for text, rho, outcome in found[:4]:
        if direction and rho:
            parts.append(f"{text} → yesterday's value points to {'higher' if direction * rho > 0 else 'lower'} {outcome}")
        else:
            parts.append(text)
    return "\n      findings for this input: " + "; ".join(parts)


async def yesterday_block(db: AsyncSession, today: date) -> str:
    """YESTERDAY'S INPUTS for the daily briefing: unusual values first, the rest as a
    compact typical list, and what was not recorded — so nothing has to be guessed."""
    try:
        yi = await yesterday_inputs(db, today)
    except Exception as exc:
        logger.warning("yesterday_block failed: %s", exc)
        return ""
    flags = yi.pop("_flags", [])
    # Which findings exist for each input, stated ON the unusual line. One briefing saw
    # an UNUSUAL sodium day, borrowed "emerging" from an unrelated screen→stress pattern
    # and invented "Known: high sodium → lower energy" (the real ρ was about zero). A local "no finding links this" is harder to talk past (note 55).
    links: dict[str, list[str]] = {}
    try:
        from app.services.correlations import (OUTCOMES, PRIVATE_METRICS, descriptive_phrase,
                                               direction_phrase, read_correlation_cache)
        for r in await read_correlation_cache():
            if not (r.get("significant") or r.get("suggestive")) or r.get("spearman_r") is None:
                continue
            if r["metric_b"] in PRIVATE_METRICS:
                continue
            # Direction in words (note 55), not "<outcome> ρ=-0.32" left to interpret.
            # A CONFOUNDED finding is described, never phrased as advice.
            outcome = OUTCOMES.get(r["metric_b"], {}).get("label") or r["metric_b"]
            if r.get("reverse_check") == "confounded":
                links.setdefault(r["metric_a"], []).append((
                    f"{descriptive_phrase(r['metric_a'], r['metric_b'], r['spearman_r'])} "
                    f"ρ={r['spearman_r']:+.2f} [CONFOUNDED: same link the day before — not a lever]",
                    None, outcome))
                continue
            links.setdefault(r["metric_a"], []).append((
                f"{direction_phrase(r['metric_a'], r['metric_b'], r['spearman_r'])} "
                f"ρ={r['spearman_r']:+.2f}", r["spearman_r"], outcome))
    except Exception as exc:  # noqa: BLE001
        logger.warning("yesterday_block: finding links unavailable: %s", exc)
        links = None
    unusual, typical, missing = [], [], []
    for k, e in yi.items():
        if e["value"] is None:
            missing.append(e["label"])
        elif e.get("binary"):
            yn = "yes" if e["value"] >= 0.5 else "no"
            line = f"{e['label']} {yn}"
            if e.get("unusual"):
                unusual.append(f"  - {e['label']} ({e['when']}): {yn} — UNUSUAL "
                               f"(yes on {e['yes_share']:.0%} of days)"
                               + _link_note(links, k, 1 if e["value"] >= 0.5 else -1))
            else:
                typical.append(line)
        elif e.get("unusual"):
            # The difference is printed, not left to the model: given only "106 (usual
            # 46)" it wrote "+106 min" (2026-09-23 regeneration test).
            diff = e['value'] - e['p50'] if e.get('p50') is not None else None
            unusual.append(f"  - {e['label']} ({e['when']}): {_fmt_val(e['value'])} "
                           f"— UNUSUAL, {'+' if diff is not None and diff >= 0 else ''}{_fmt_val(diff)} vs your usual "
                           f"{_fmt_val(e.get('p50'))} (normal range {_fmt_val(e.get('p10'))}–{_fmt_val(e.get('p90'))})"
                           + _link_note(links, k, 0 if diff is None else (1 if diff > 0 else -1 if diff < 0 else 0)))
        else:
            typical.append(f"{e['label']} {_fmt_val(e['value'])}")
    yday = today - timedelta(days=1)
    head = (f"YESTERDAY'S INPUTS ({yday}{', flagged: ' + ', '.join(flags) if flags else ''}) — "
            f"actual values vs your own last {USUAL_DAYS} days. Use these; never guess what an "
            "input might have been.\n")
    body = ("  UNUSUAL:\n" + "\n".join(unusual) + "\n") if unusual else "  UNUSUAL: none\n"
    body += ("  Within your usual range: " + ", ".join(typical) + "\n") if typical else ""
    body += ("  Not recorded yesterday (say nothing about these): " + ", ".join(missing) + "\n") if missing else ""
    return head + body + "\n"


# ── News events + novelty ledger (daily summary redesign, 2026-07-11) ─────────

async def events_block(db: AsyncSession) -> str:
    """Discrete NEWS since ~yesterday: strength PRs, finding-ledger crossings,
    fresh changepoints, coach-experiment milestones. The daily prompt leads with
    these — and an explicitly empty block licenses a SHORT summary, which is the
    antidote to daily-report filler. Fail-safe ("" on any error)."""
    try:
        from app.models import Experiment, ExperimentAssignment, FindingLedger, HevyWorkout
        from app.services.correlations import METRIC_LABELS

        lines: list[str] = []
        today = local_today()

        # Strength PRs pushed in the last ~36h
        pr_cutoff = datetime.utcnow() - timedelta(hours=36)
        for w in (await db.execute(
            select(HevyWorkout).where(HevyWorkout.pr_notified_at >= pr_cutoff)
        )).scalars().all():
            lines.append(f"- New strength PR in \"{w.title or 'workout'}\"")

        # Correlation findings that crossed a tier this week.
        #
        # Gated by is_newsworthy (effect size + n + shared-drift), NOT by the ledger
        # tier alone. The tiers are p-value-only, so at current n they admit effects
        # around ρ=0.2 — ~4% of variance, which cannot support an action. Feeding
        # those here was the root of the daily summary's filler: the prompt makes
        # every correlation carry a New/Firming/Known label, so anything that lands
        # in this block gets reported as news whether or not it is any.
        #
        # Checked against the pair's CURRENT cached stats rather than its stats at
        # crossing time: a finding that crossed at ρ=0.35 and has since decayed to
        # 0.15 is not today's news, and the ledger stores only the crossing snapshot.
        from app.services.correlations import (
            PRIVATE_METRICS, is_controllable, is_newsworthy, read_correlation_cache)
        cache = {(r["metric_a"], r["metric_b"], r["lag_days"]): r
                 for r in await read_correlation_cache()}

        # "New" means new SINCE THE LAST BRIEFING, not "crossed this week". A 7-day
        # window re-announced the same finding as New for a week of mornings (note 55).
        since = today - timedelta(days=1)
        try:
            yi = await yesterday_inputs(db, today)
        except Exception as exc:  # noqa: BLE001
            logger.error("yesterday_inputs failed (briefing loses yesterday's context): %s", exc)
            yi = {}
        for led in (await db.execute(select(FindingLedger))).scalars().all():
            # Private metrics never reach a prompt — personal_patterns_block enforced
            # this; this loop did not (found 2026-09-23).
            if led.metric_a in PRIVATE_METRICS or led.metric_b in PRIVATE_METRICS:
                continue
            cur = cache.get((led.metric_a, led.metric_b, led.lag_days))
            if cur is None or not is_newsworthy(
                    cur.get("spearman_r"), cur.get("n"), cur.get("n_eff")):
                continue
            a = METRIC_LABELS.get(led.metric_a, led.metric_a)
            b = METRIC_LABELS.get(led.metric_b, led.metric_b)
            # [lever] vs [context]: whether the INPUT is something he can act on.
            # Without this the briefing turned barometric pressure into "watch for
            # thermal drift today" — a real correlation rendered as an instruction.
            kind = "lever" if is_controllable(led.metric_a) else "context"
            if cur.get("reverse_check") == "confounded":
                kind = "CONFOUNDED: same link the day before — context, not a lever"
            stat = f"(ρ={cur['spearman_r']:+.2f}, n={cur['n']})"
            e = yi.get(led.metric_a)
            if e is None:
                ctx = ""
            elif e["value"] is None:
                ctx = f" · {e['when']}: not recorded — irrelevant today, omit"
            elif e.get("binary"):
                ctx = (f" · {e['when']}: {'yes' if e['value'] >= 0.5 else 'no'}"
                       f" (yes on {e.get('yes_share', 0):.0%} of days)")
            else:
                ctx = (f" · {e['when']}: {_fmt_val(e['value'])}"
                       + (f" (usual {_fmt_val(e['p50'])}{', UNUSUAL' if e.get('unusual') else ', typical'})"
                          if e.get("p50") is not None else ""))
            # Direction in WORDS, not just the sign of rho (note 55) — see direction_phrase.
            from app.services.correlations import direction_phrase
            rel = direction_phrase(led.metric_a, led.metric_b, cur["spearman_r"])
            if cur.get("reverse_check") == "confounded":
                from app.services.correlations import descriptive_phrase
                rel = descriptive_phrase(led.metric_a, led.metric_b, cur["spearman_r"])
            # A crossing caused by redefining the outcome is not news (OUTCOME_REDEFINED);
            # the finding still reaches the prompt through personal_patterns_block as Known.
            from app.services.correlations import is_redefinition_crossing
            if led.first_confirmed_at and led.first_confirmed_at >= since:
                if not is_redefinition_crossing(led.metric_b, led.first_confirmed_at):
                    lines.append(f"- [{kind}] Finding CONFIRMED since the last briefing: {rel} {stat}{ctx}")
            elif led.first_suggestive_at and led.first_suggestive_at >= since:
                if not is_redefinition_crossing(led.metric_b, led.first_suggestive_at):
                    lines.append(f"- [{kind}] New suggestive finding: {rel} {stat}{ctx}")

        # Step changes detected within the last 10 days
        from app.services.trends import compute_trends
        for t in await compute_trends(db, days=90):
            cp = t.get("changepoint")
            if cp and (today - date.fromisoformat(cp["date"])).days <= 10:
                lines.append(f"- Step change in {t['label']}: shifted {cp['shift']} "
                             f"around {cp['date']} ({cp['sentiment']})")

        # New blood panel saved in the last ~36h
        from app.models import BloodMarker
        new_markers = (await db.execute(
            select(BloodMarker).where(BloodMarker.created_at >= pr_cutoff)
        )).scalars().all()
        if new_markers:
            from app.services.blood_markers import assess
            newest_draw = max(m.date for m in new_markers)
            # Direction-aware: an HDL above the printed range is not "out of range"
            # in any sense worth reporting as news.
            n_out = sum(1 for m in new_markers
                        if assess(m.marker, m.value, m.ref_low, m.ref_high)["concern"])
            lines.append(f"- New blood panel logged ({len(new_markers)} markers, drawn "
                         f"{newest_draw}{f', {n_out} needing attention' if n_out else ''})")

        # Coach experiment milestone (every 10th randomized day)
        exp = (await db.execute(
            select(Experiment).where(Experiment.ended_at.is_(None)).limit(1)
        )).scalar_one_or_none()
        if exp is not None:
            n_conf = (await db.execute(
                select(func.count(ExperimentAssignment.id))
                .where(ExperimentAssignment.experiment_id == exp.id)
                .where(ExperimentAssignment.eligible_confirmed.is_(True))
            )).scalar() or 0
            if n_conf and n_conf % 10 == 0:
                lines.append(f"- Coach experiment milestone: {n_conf} randomized days collected")

        if not lines:
            return ("WHAT'S NEW: nothing — there is NO news today. Keep the briefing "
                    "to the Recommendation line plus at most one sentence.\n\n")
        return "WHAT'S NEW (lead with these):\n" + "\n".join(lines) + "\n\n"
    except Exception as exc:
        logger.warning("events_block failed: %s", exc)
        return ""


async def recent_summaries_block(db: AsyncSession, n: int = 3,
                                 before: date | None = None) -> str:
    """The last n daily briefings — the NOVELTY LEDGER. The model is instructed
    to never restate a fact, number, or correlation already present here unless
    it materially changed. Fail-safe ("" on any error).

    n was 5 (~560 tokens, the daily's largest single input). Cut to 3 on 2026-09-09 to
    make room for `period_series_block`, which gives the same "what changed" signal as
    DATA rather than as the model's own prose. Unlike the weekly/monthly prior blocks
    this one is NOT reduced to claims: its job is repetition-avoidance, which needs the
    actual sentences, and it was already capped at 400 chars each."""
    try:
        from app.models import DailyMetric
        rows = (await db.execute(
            select(DailyMetric)
            .where(DailyMetric.ai_summary.isnot(None))
            .where(DailyMetric.date < (before or local_today()))
            .order_by(DailyMetric.date.desc())
            .limit(n)
        )).scalars().all()
        if not rows:
            return ""
        parts = [f"[{r.date}] {(r.ai_summary or '')[:400]}" for r in rows]
        return ("YOUR LAST BRIEFINGS (novelty ledger — do NOT repeat facts, numbers, or "
                "correlations already stated below unless they materially CHANGED; "
                "repetition is the failure mode):\n" + "\n".join(parts) + "\n\n")
    except Exception as exc:
        logger.warning("recent_summaries_block failed: %s", exc)
        return ""


# Generous — a full panel is ~40 markers and each line is short. This is a guard
# against a pathologically large panel, not a curation step.
MAX_BLOOD_MARKERS_IN_CONTEXT = 80


async def blood_block(db: AsyncSession) -> str:
    """Latest blood panel with deltas vs the prior draw + out-of-range flags —
    the slow ground-truth outcomes, fed to WEEKLY/MONTHLY summaries so lifestyle
    interpretation can anchor to them ("ApoB down 12 since the spring draw").
    Fail-safe ("" on any error or no panels)."""
    try:
        from app.models import BloodMarker
        rows = (await db.execute(
            select(BloodMarker).order_by(BloodMarker.date.desc())
        )).scalars().all()
        if not rows:
            return ""
        from types import SimpleNamespace

        from app.services.blood_markers import (assess_in_panel, classify_delta,
                                                with_absolute_counts)

        # Same derived absolute differential counts as the card (GET /api/blood), so
        # the model never sees "lymphocytes 48% ⚠" that the card no longer flags.
        rows = [SimpleNamespace(**d) for d in with_absolute_counts([
            {"id": r.id, "date": r.date, "marker": r.marker, "label": r.label,
             "value": r.value, "unit": r.unit, "ref_low": r.ref_low,
             "ref_high": r.ref_high, "derived": False} for r in rows])]
        rows.sort(key=lambda r: r.date, reverse=True)
        latest_date = rows[0].date
        latest = [r for r in rows if r.date == latest_date]
        latest_keys = {r.marker for r in latest}
        prior = {}
        for r in rows:
            if r.date < latest_date and r.marker not in prior:
                prior[r.marker] = r

        # Order by what deserves attention, THEN cap. The previous `latest[:30]` cut
        # in insertion order, which on a 38-marker panel silently dropped the entire
        # lipid group plus FIB-4 — i.e. exactly the lifestyle-responsive outcomes this
        # block exists to supply ("ApoB down 12 since the spring draw" would have been
        # truncated away). The cap only exists to bound context on an unusually large
        # panel; at that point it should shed the least interesting rows, not the last
        # ones the lab happened to print.
        assessed = [(r, assess_in_panel(r.marker, r.value, r.unit, r.ref_low, r.ref_high,
                                        latest_keys)) for r in latest]
        assessed.sort(key=lambda ra: (not ra[1]["concern"], not ra[1]["favorable"],
                                      ra[0].marker))

        lines = []
        for r, a in assessed[:MAX_BLOOD_MARKERS_IN_CONTEXT]:
            # Direction-aware: HDL or eGFR above the printed range is the good
            # outcome. Flagging it "⚠ ABOVE RANGE" told the model to treat a
            # favourable result as a problem.
            flag = ""
            if a["concern"]:
                flag = f" ⚠ {a['position'].upper()} RANGE"
            elif a["favorable"]:
                flag = f" ({a['position']} range — favorable for this marker)"
            elif a["read_as"]:
                flag = " (% of white cells — judge by the absolute count)"
            if r.derived:
                flag += " (calculated WBC × %; app's lenient adult range)"
            # Annotate the delta with whether it clears biological + analytical
            # variation. Without this the model reads "Δ+7" as a finding and writes
            # it up as one, when for ALT (RCV ~56%) a +14% move is indistinguishable
            # from the same blood drawn twice.
            p = prior.get(r.marker)
            delta = ""
            if p:
                c = classify_delta(r.marker, p.value, r.value)
                if c["significant"] is True:
                    note = ", real change"
                    if c["trend"]:
                        note += " — " + c["trend"]
                elif c["significant"] is False:
                    note = ", within normal variation"
                else:
                    note = ""          # no published CV: claim nothing
                delta = f"  Δ{r.value - p.value:+g} since {p.date}{note}"
            lines.append(f"  {r.label or r.marker}: {r.value} {r.unit or ''}{delta}{flag}")
        if len(assessed) > MAX_BLOOD_MARKERS_IN_CONTEXT:
            lines.append(f"  (+{len(assessed) - MAX_BLOOD_MARKERS_IN_CONTEXT} further "
                         f"in-range markers not listed)")
        return (f"BLOOD PANEL (drawn {latest_date} — slow outcomes; only comment when "
                f"relevant to a lever or change):\n" + "\n".join(lines) + "\n\n")
    except Exception as exc:
        logger.warning("blood_block failed: %s", exc)
        return ""


# ── Diet composition + food-group coverage (weekly / monthly) ──────────────────

async def diet_block(db: AsyncSession, win_start: date, win_end: date,
                     base_days: int, label: str) -> str:
    """What the diet was MADE OF this period, and whether any food group has lapsed.

    Two things the summary could not previously see. Composition is the calorie
    split across base components (services/food_tags.py) compared against a trailing
    baseline, so the writer can say "grain share rose" rather than only "you ate
    2,400 kcal". Coverage is the tripwire: drift in how often a food group appears,
    measured against the user's own history rather than an RDA.

    The caveats travel WITH the numbers on purpose. Both are estimates with known
    failure modes, and a summary that states them as measured fact would be worse
    than one that omits them:
      * component shares are per-item judgements, not measurements;
      * a group at zero means "no source logged", which can also mean the food
        started being logged through a different channel (this exact artifact once
        made leafy-green coverage look like it halved when it had not).
    """
    try:
        from app.services import food_tags

        win_days = max((win_end - win_start).days + 1, 1)
        cur = await food_tags.composition(db, days=win_days - 1, end=win_end)
        base = await food_tags.composition(db, days=base_days,
                                           end=win_start - timedelta(days=1))
        lines: list[str] = []

        cur_s = {s["key"]: s for s in cur.get("slices", [])}
        base_s = {s["key"]: s for s in base.get("slices", [])}
        if cur_s and cur.get("entries_tagged", 0) >= 10:
            lines.append(
                f"DIET COMPOSITION (share of logged calories this period vs {label} baseline; "
                f"per-item ESTIMATES, not measurements):"
            )
            for key in food_tags.COMPONENT_ORDER:
                c = cur_s.get(key)
                if not c or c.get("kcal_pct") is None:
                    continue
                b = base_s.get(key, {}).get("kcal_pct")
                if b is None:
                    lines.append(f"  {c['label']}: {round(c['kcal_pct'])}%")
                    continue
                # Round BOTH before differencing. Formatting each to 0 dp and then
                # showing a delta taken from the unrounded values prints things like
                # "19% vs 19% baseline, +1", which reads as an error.
                cr, br = round(c["kcal_pct"]), round(b)
                delta = cr - br
                # These are per-item estimates; a 1-point move is not a finding, and
                # a news-style summary will happily narrate one as news.
                note = f"{delta:+d}" if abs(delta) >= 2 else "flat"
                lines.append(f"  {c['label']}: {cr}% (vs {br}% baseline, {note})")
            lines.append("")

        cov = await food_tags.coverage(db)
        if cov.get("reliable") and cov.get("groups"):
            flagged = [g for g in cov["alerts"] if g["status"] in ("dropped", "absent")]
            steady = [g for g in cov["groups"]
                      if g["status"] == "ok" and g["recent_pct"] is not None]
            lines.append(
                f"FOOD-GROUP COVERAGE (% of the last {cov['recent_days']} logged days "
                f"containing a source, vs a {cov['baseline_days']}-day personal baseline):"
            )
            for g in flagged:
                if g["status"] == "dropped":
                    lines.append(f"  FLAGGED {g['label']}: {g['recent_pct']:.0f}% vs "
                                 f"{g['baseline_pct']:.0f}% baseline — sustained drop")
                else:
                    lines.append(f"  FLAGGED {g['label']}: no source logged in "
                                 f"{g['days_since_last']} days")
            if steady:
                lines.append("  Steady: " + ", ".join(
                    f"{g['label']} {g['recent_pct']:.0f}%" for g in steady))
            if not flagged:
                lines.append("  Nothing has drifted against baseline this period.")
            lines.append(
                "  CAVEAT: coverage counts whether a source was LOGGED. A group at or near "
                "zero can mean it moved to a different logging channel rather than left the "
                "diet — say 'no source logged', never 'you stopped eating this'."
            )
            lines.append("")

        return "\n".join(lines) + "\n" if lines else ""
    except Exception as exc:
        logger.warning("diet_block failed: %s", exc)
        return ""


# ── Nonstandard days: sick / travel / etc. ─────────────────────────────────────
#
# ADDED 2026-09-07. `day_flags` drove correlation and experiment exclusion from the
# day it shipped, but no summary builder ever read it — so the briefings had no idea
# the athlete had been sick or travelling, and confidently explained a flat week as a
# training or adherence problem. This block is the missing input.
#
# Consecutive days of the same reason are collapsed into a span: three separate "sick"
# lines read as three unrelated bad days, where "sick Mon-Wed (3 days)" is the thing
# that actually explains the week.

def _collapse_runs(days: list, reason: str) -> list[tuple]:
    """[(start, end, n_days), ...] for consecutive dates sharing a reason."""
    runs: list[tuple] = []
    for d in sorted(days):
        if runs and (d - runs[-1][1]).days == 1:
            runs[-1] = (runs[-1][0], d, runs[-1][2] + 1)
        else:
            runs.append((d, d, 1))
    return runs


async def day_flags_block(db: AsyncSession, win_start: date, win_end: date) -> str:
    try:
        from app.services.day_flags import REASON_LABELS, flags_in_range

        flags = await flags_in_range(db, win_start, win_end)
        if not flags:
            return ""

        by_reason: dict = {}
        for d, reasons in flags.items():
            for r in reasons:
                by_reason.setdefault(r, []).append(d)

        span_days = (win_end - win_start).days + 1
        lines = []
        for reason, days in sorted(by_reason.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            label = REASON_LABELS.get(reason, reason.replace("_", " "))
            parts = []
            for s, e, n in _collapse_runs(days, reason):
                if n == 1:
                    parts.append(f"{s:%b %-d}")
                elif s.month == e.month:
                    parts.append(f"{s:%b %-d}-{e:%-d} ({n} days)")
                else:
                    parts.append(f"{s:%b %-d}-{e:%b %-d} ({n} days)")
            lines.append(f"  {label}: {', '.join(parts)}")

        flagged_total = len(flags)
        return (
            f"NONSTANDARD DAYS ({flagged_total} of {span_days} days in this window carry a flag):\n"
            + "\n".join(lines) + "\n"
            "  These are the athlete's own flags. Treat them as the FIRST candidate\n"
            "  explanation for anything unusual in the window, and say so plainly — a\n"
            "  flat week during an illness is not an adherence problem. Days flagged\n"
            "  anything other than 'Incomplete data' are already excluded from the\n"
            "  correlation engine and experiments, so they cannot be driving those.\n"
            "  'Incomplete data' means the meal log is partial: intake is UNDERSTATED\n"
            "  on those days, never call it a deficit.\n\n"
        )
    except Exception as exc:
        logger.warning("day_flags_block failed: %s", exc)
        return ""


# ── Goal trajectory: fixed targets, moving performance ────────────────────────
#
# ADDED 2026-09-07 at the user's request. The briefings kept restating the same shortfall
# every period ("protein 46 g below target") with no sense of whether he was closing the
# gap or widening it. A target he has missed for five straight weeks while climbing
# toward it is a different situation from one he has missed while drifting away, and
# only the second deserves to be called a problem.
#
# TWO THINGS ARE TRUE AT ONCE and the block has to carry both:
#   * The GOAL IS FIXED. It is not rebased to what he actually did — that would launder
#     a miss into a success and is the failure mode this must not introduce.
#   * PERFORMANCE IS A TRAJECTORY. Direction against the target is the news; the static
#     distance from it, repeated verbatim each period, is not.
#
# Days flagged `incomplete_data` are dropped: a partial meal log understates intake, so
# leaving them in would manufacture misses and could invent a decline that is really a
# logging gap.

GOAL_TRAJECTORY_WEEKS = 6
# A week with fewer logged days than this has no mean and no vote on the direction. Once,
# a 3-day trip week called protein CLOSING while the two real weeks were falling.
GOAL_TRAJECTORY_MIN_DAYS = 4


def gap_direction(means: list, goal: float, higher_is_better: bool = True) -> Optional[tuple]:
    """(direction, delta) comparing the latest counted week with the mean of the earlier
    ones, or None with fewer than 2 counted weeks. Two endpoints were not enough: one
    unusual first week set the verdict."""
    seen = [m for m in means if m is not None]
    if len(seen) < 2:
        return None
    delta = seen[-1] - stats.mean(seen[:-1])
    if abs(delta) < 0.02 * goal:
        return "flat", delta
    return ("closing" if (delta > 0) == higher_is_better else "widening"), delta


_GAP_METRICS = {"protein": "Protein", "fiber": "Fiber", "fibre": "Fiber",
                "calorie": "Calories", "calories": "Calories"}
_GAP_WORDS = {"closing": ("closing", False), "closed": ("closing", True),
              "narrowing": ("closing", False), "narrowed": ("closing", True),
              "shrinking": ("closing", False), "shrank": ("closing", True),
              "widening": ("widening", False), "widened": ("widening", True),
              "growing": ("widening", False), "grew": ("widening", True),
              "flat": ("flat", False)}
_GAP_SAY = {"closing": ("closing", "closed"), "widening": ("widening", "widened"),
            "flat": ("flat", "flat")}
_GAP_LABEL_RE = re.compile(r"^\s*(Protein|Fiber|Calories): GAP (WIDENING|CLOSING|FLAT)\b", re.M)
_GAP_TEXT_RE = re.compile(
    r"\b(protein|fibre|fiber|calories|calorie)\b"
    r"((?:(?!protein|fibre|fiber|calorie)[^.\n•:;]){0,30}?)"
    r"\b(" + "|".join(_GAP_WORDS) + r")\b", re.I)


def enforce_gap_direction(text: str, trajectory_block: str) -> str:
    """Make any stated gap direction match goal_trajectory_block's label. The label is
    computed; the model has contradicted it (10-30: "protein gap closing" against GAP
    WIDENING). A prompt rule did not stop that, so this runs on the finished text."""
    labels = {m.group(1): m.group(2).lower() for m in _GAP_LABEL_RE.finditer(trajectory_block or "")}
    if not text or not labels:
        return text

    def fix(m: re.Match) -> str:
        middle, word = m.group(2), m.group(3)
        said, past = _GAP_WORDS[word.lower()]
        want = labels.get(_GAP_METRICS[m.group(1).lower()])
        # Only a statement about the gap: "calories flat at 2500" is about the value.
        about_gap = "gap" in middle.lower() or word.lower() in ("closing", "closed", "widening", "widened")
        if want is None or want == said or not about_gap or re.search(r"\b(not|never|no)\b|n't", middle, re.I):
            return m.group(0)
        logger.warning("Gap direction corrected: %r -> %s", m.group(0), want)
        say = _GAP_SAY[want][past]
        return m.group(0)[:m.start(3) - m.start(0)] + (say.capitalize() if word[0].isupper() else say)

    return _GAP_TEXT_RE.sub(fix, text)


def _week_label(start: date, end: date) -> str:
    """'Nov 2–8', 'Oct 27–Nov 2', or 'Nov 15' for a one-day span."""
    if end <= start:
        return f"{start:%b} {start.day}"
    if end.month == start.month:
        return f"{start:%b} {start.day}–{end.day}"
    return f"{start:%b} {start.day}–{end:%b} {end.day}"


async def goal_trajectory_block(db: AsyncSession, win_end: date,
                                weeks: int = GOAL_TRAJECTORY_WEEKS) -> str:
    try:

        from app.models import FoodLog, UserSettings
        from app.services.day_flags import nutrition_excluded_dates
        from app.services.energy import effective_calorie_goal
        from app.services.nutrition_goals import fiber_goal_for, resolve_protein_goal
        from app.timeutils import local_today

        settings = (await db.execute(select(UserSettings).limit(1))).scalar_one_or_none()
        start = win_end - timedelta(days=weeks * 7 - 1)

        rows = (await db.execute(
            select(
                food_date_sql(FoodLog.timestamp).label("d"),
                func.sum(FoodLog.calories).label("kcal"),
                func.sum(FoodLog.protein_g).label("protein"),
                func.sum(FoodLog.fiber_g).label("fiber"),
            )
            .where(FoodLog.timestamp >= food_day_start(start))
            .where(FoodLog.timestamp < food_day_end(win_end))
            .where((FoodLog.skipped == False) | (FoodLog.skipped.is_(None)))  # noqa: E712
            .group_by(food_date_sql(FoodLog.timestamp))
        )).all()
        # Today is still being logged — never judge it. The weekly summary fires at
        # noon Sunday with win_end = today, so the breakfast-only day sat in the
        # current bucket and dragged the mean down: "protein fell" by ~15% against a
        # flat Mon–Sat (note 61). Dropped here rather than by moving win_end, so
        # the 7-day buckets stay aligned with the caller's Mon–Sun weeks.
        today = local_today()
        by_day = {d: r for d, r in ((date.fromisoformat(str(r.d)), r) for r in rows)
                  if d < today}
        if not by_day:
            return ""

        excluded = await nutrition_excluded_dates(db, start, win_end)
        protein_goal = await resolve_protein_goal(db, settings)
        fiber_goal = fiber_goal_for(settings)

        cal_goal_by_day: dict = {}
        if settings is not None:
            d = start
            while d <= win_end:
                g = (await effective_calorie_goal(db, settings, d))["goal"]
                if g:
                    cal_goal_by_day[d] = g
                d += timedelta(days=1)

        # Trailing 7-day blocks, oldest first, so the last entry is the current week.
        buckets: list[list] = []
        week_labels: list[str] = []
        for w in range(weeks - 1, -1, -1):
            b_end = win_end - timedelta(days=w * 7)
            b_start = b_end - timedelta(days=6)
            days = [by_day[d] for d in by_day
                    if b_start <= d <= b_end and d not in excluded]
            buckets.append(days)
            week_labels.append(_week_label(b_start, min(b_end, today - timedelta(days=1))))

        def _line(label: str, attr: str, goal, unit: str, higher_is_better=True,
                  goal_by_day: Optional[dict] = None, goal_note: str = "FIXED") -> Optional[str]:
            """`goal_by_day` overrides `goal` per day. The calorie target MOVES with
            activity in pace/balance mode, so scoring it against a window average would
            judge each day by a target that was never in force on it; `goal` is then just
            the headline figure and `goal_note` says so."""
            if not goal:
                return None
            means, hits = [], []
            for days in buckets:
                vals = [(row, getattr(row, attr) or 0) for row in days]
                if not vals:
                    means.append(None)
                    hits.append(None)
                    continue
                means.append(sum(v for _, v in vals) / len(vals)
                             if len(vals) >= GOAL_TRAJECTORY_MIN_DAYS else None)
                # 90% of target counts as hit — the same bar the weekly nutrition
                # paragraph already uses, so the two cannot disagree.
                ok = 0
                for row, v in vals:
                    g = goal if goal_by_day is None else goal_by_day.get(
                        date.fromisoformat(str(row.d)), goal)
                    if (v >= g * 0.9) if higher_is_better else (v <= g * 1.1):
                        ok += 1
                hits.append((ok, len(vals)))
            verdict = gap_direction(means, goal, higher_is_better)
            if verdict is None:
                return None
            direction, delta = verdict
            # Each week carries its dates and its own days-at-goal (note 104). As two
            # undated arrow rows, weeklies with trip weeks (--) counted to the wrong one:
            # "down from X two weeks ago" (it was last week), and a days-at-goal
            # count lifted from the week before the latest.
            weeks_str = "\n".join(
                f"      {wl}{' (LATEST)' if i == len(means) - 1 else ''}: "
                + ("--" if m is None else f"{m:.0f} {unit}")
                + ("" if h is None else f", {h[0]}/{h[1]} days at goal")
                for i, (wl, m, h) in enumerate(zip(week_labels, means, hits))
            )
            # Direction FIRST and in caps (2026-10-29). As a trailing "(…, gap widening)"
            # the briefing misread it under both the old and the Rho prompt — "protein gap
            # closing" on series that were falling away from the target.
            return (f"  {label}: GAP {direction.upper()} ({delta:+.0f} {unit}, latest week vs "
                    f"the earlier weeks; target {goal:.0f} {unit} — {goal_note})\n"
                    f"    weekly mean, days at goal:\n{weeks_str}")

        lines = [
            _line("Protein", "protein", protein_goal, "g"),
            _line("Fiber", "fiber", fiber_goal, "g"),
        ]
        if cal_goal_by_day:
            avg_cal_goal = sum(cal_goal_by_day.values()) / len(cal_goal_by_day)
            lines.append(_line(
                "Calories", "kcal", avg_cal_goal, "kcal", higher_is_better=False,
                goal_by_day=cal_goal_by_day,
                goal_note="window avg of a goal that MOVES daily; each day scored on its own",
            ))
        lines = [ln for ln in lines if ln]
        if not lines:
            return ""

        return (
            f"GOAL TRAJECTORY (last {weeks} weeks, oldest first, each week dated; days with\n"
            f"an incomplete meal log are excluded so a logging gap cannot look like a miss;\n"
            f"-- = under {GOAL_TRAJECTORY_MIN_DAYS} logged days, no mean). Name a week by its\n"
            f"dates or as 'last week' only if it is the one just before LATEST; never count\n"
            f"weeks back across -- rows:\n"
            + "\n".join(lines) + "\n"
            "  HOW TO USE THIS. The targets above are FIXED — never restate one as though\n"
            "  it had moved, and never rebase it to what was actually eaten. (The calorie\n"
            "  goal is the stated exception: it is dynamic by design and the figure shown\n"
            "  is a window average.) But do not\n"
            "  report the same shortfall every period either: the DIRECTION is the news.\n"
            "  A target missed for weeks while the gap CLOSES is progress and should be\n"
            "  reported as progress, naming the movement. A gap that is WIDENING, or flat\n"
            "  after repeated mention, is the one that earns a line. If the trajectory is\n"
            "  good, say so plainly even though the goal is still unmet.\n\n"
        )
    except Exception as exc:
        logger.warning("goal_trajectory_block failed: %s", exc)
        return ""


# ── Per-period series: the thing that was actually missing ────────────────────
#
# ADDED 2026-09-09. The weekly summary received **1,353 tokens of its own previous prose
# and 60 tokens of actual 12-week baseline** — and that baseline was four aggregate
# numbers with no per-week breakdown at all. So when the prompt asked it to compare week
# over week, its ONLY source of week-over-week information was its own past narrative.
# That is why a claim like "the Thursday cascade" compounded across three consecutive
# summaries: the model was reading its own story because nothing else let it see across
# weeks. Not a prompt failure — a data failure.
#
# This is the fix: one compact row per period, so a trend is visible as a SERIES rather
# than inferred from prose. Roughly 250 tokens for 12 weeks, which is paid for several
# times over by trimming the prior-summary blocks alongside it.
#
# Sessions are reported with the lifting/conditioning split (2026-08-19), not the raw
# Apple label — every gym class logs as "High Intensity Interval Training", so the raw
# count says "31 HIIT" where the truth is a mix of arm days and conditioning. Since
# 2026-09-27 an explicit Strength label wins over HR (workout_categories).
# Threshold and rationale live in services/correlations (CONDITIONING_HI_MIN).

PERIOD_SERIES_ROWS = {"day": 10, "week": 12, "month": 6}


def _period_key(d: date, level: str) -> str:
    if level == "day":
        return d.isoformat()
    if level == "week":
        return (d - timedelta(days=d.weekday())).isoformat()   # ISO Monday
    return f"{d.year}-{d.month:02d}"


async def period_series_block(db: AsyncSession, level: str, end: date,
                              n: Optional[int] = None) -> str:
    """One row per day/week/month, most recent last. `level` sets the granularity."""
    try:

        from app.models import DailyMetric, FoodLog, SleepSession, Workout
        from app.timeutils import local_date_sql
        from app.workout_categories import categorize_activity, classify_gym_session

        n = n or PERIOD_SERIES_ROWS.get(level, 12)
        span = {"day": n, "week": n * 7, "month": n * 31}[level]
        start = end - timedelta(days=span)

        buckets: dict[str, dict] = {}

        def slot(d: date) -> dict:
            k = _period_key(d, level)
            return buckets.setdefault(k, {"readiness": [], "hrv_z": [], "sleep": [],
                                          "load": [], "kcal": [], "protein": [],
                                          "lift": 0, "cond": 0, "other": 0})

        for r in (await db.execute(
            select(DailyMetric).where(DailyMetric.date >= start).where(DailyMetric.date <= end)
        )).scalars().all():
            s = slot(r.date)
            if r.readiness_score is not None:
                s["readiness"].append(r.readiness_score)
            if r.hrv_z_score is not None:
                s["hrv_z"].append(r.hrv_z_score)
            if r.daily_load is not None:
                s["load"].append(r.daily_load)

        for d_, tm in (await db.execute(
            select(SleepSession.date, SleepSession.total_min)
            .where(SleepSession.date >= start).where(SleepSession.date <= end)
            .where(SleepSession.total_min.is_not(None))
        )).all():
            slot(d_)["sleep"].append(tm / 60.0)

        # Food never counts today (still being logged) or a day flagged incomplete_data —
        # the same rule goal_trajectory_block follows. Without it the week/month rows
        # averaged in the breakfast-only Sunday, so the weekly prompt carried a protein
        # "dip" from this table alongside the corrected trajectory —
        # note 61's bug, second copy. (The day level blanks today's row separately.)
        from app.services.day_flags import nutrition_excluded_dates
        from app.timeutils import local_today
        _food_skip = await nutrition_excluded_dates(db, start, end)
        _food_skip.add(local_today())
        for r in (await db.execute(
            select(food_date_sql(FoodLog.timestamp).label("d"),
                   func.sum(FoodLog.calories).label("kcal"),
                   func.sum(FoodLog.protein_g).label("protein"))
            .where(FoodLog.timestamp >= food_day_start(start))
            .where(FoodLog.timestamp < food_day_end(end))
            .where((FoodLog.skipped == False) | (FoodLog.skipped.is_(None)))  # noqa: E712
            .group_by(food_date_sql(FoodLog.timestamp))
        )).all():
            if date.fromisoformat(str(r.d)) in _food_skip:
                continue
            s = slot(date.fromisoformat(str(r.d)))
            if r.kcal:
                s["kcal"].append(r.kcal)
            if r.protein:
                s["protein"].append(r.protein)

        wo_day = local_date_sql(Workout.start_time)
        for r in (await db.execute(
            select(wo_day.label("d"), Workout.activity_type, Workout.high_intensity_min)
            .where(wo_day >= str(start)).where(wo_day <= str(end))
        )).all():
            s = slot(date.fromisoformat(str(r.d)))
            kind = classify_gym_session(r.activity_type, r.high_intensity_min)
            if kind == "conditioning":
                s["cond"] += 1
            elif kind == "lifting":
                s["lift"] += 1
            elif categorize_activity(r.activity_type) not in ("walk", None):
                # Unclassifiable (no HR) is NOT silently a lifting day — counted apart.
                s["other"] += 1

        keys = sorted(buckets)[-n:]
        if len(keys) < 2:
            return ""

        def avg(xs):
            return sum(xs) / len(xs) if xs else None

        def f(v, spec=".0f"):
            return "  -- " if v is None else format(v, spec)

        # TODAY'S ROW IS IN PROGRESS (fixed 2026-09-15). The daily briefing runs at ~7 am,
        # when today's row already has its overnight readiness/HRV/sleep but load 0 and
        # almost no food. As the LAST row it was read as "yesterday": one briefing
        # said "yesterday 0 kcal load" and "yesterday 0 kcal logged" after a full training
        # day. Its day-so-far columns are blanked and it is named.
        from zoneinfo import ZoneInfo
        from app.config import settings as _settings
        today_key = (_period_key(local_today(), level)
                     if level == "day" else None)

        label = {"day": "PER-DAY", "week": "PER-WEEK", "month": "PER-MONTH"}[level]
        lines = [f"{label} SERIES (most recent LAST — read this for trends across "
                 f"{'days' if level == 'day' else level + 's'}):",
                 "  period      rdy   hrvz  slp   load   kcal  prot  sessions(L/C)"]
        for k in keys:
            b = buckets[k]
            sess = f"{b['lift']}L/{b['cond']}C" + (f"+{b['other']}?" if b["other"] else "")
            if k == today_key:
                lines.append(
                    f"  {'TODAY':11s} {f(avg(b['readiness'])):>4s}  {f(avg(b['hrv_z']), '+.2f'):>5s}  "
                    f"{f(avg(b['sleep']), '.1f'):>4s}   (in progress — load/kcal/prot not yet in)"
                )
                continue
            lines.append(
                f"  {k:11s} {f(avg(b['readiness'])):>4s}  {f(avg(b['hrv_z']), '+.2f'):>5s}  "
                f"{f(avg(b['sleep']), '.1f'):>4s}  {f(avg(b['load'])):>5s}  "
                f"{f(avg(b['kcal'])):>5s}  {f(avg(b['protein'])):>4s}  {sess}"
            )
        lines.append("  (load = TRIMP, a heart-rate training score — NOT calories; kcal/prot = "
                     "food eaten. sessions: L=lifting, C=conditioning, ?=no HR to classify. A "
                     "Strength-labelled session is lifting; a HIIT-labelled one is split by "
                     "heart rate, because the gym class label covers both."
                     + (" YESTERDAY is the row above TODAY." if today_key in keys else "") + ")")
        return "\n".join(lines) + "\n\n"
    except Exception as exc:
        logger.warning("period_series_block failed: %s", exc)
        return ""


# ── Intake vs guidelines: fluids, caffeine, alcohol (note 78, 2026-10-25) ────────────
#
# The user asked "is my water / caffeine / alcohol in line with good health?". Answered in
# code, per month, against FIXED published references and against the prior two
# months — the model words it, it does not compute or choose a threshold. The
# references are stated in the block so the model never has to recall one:
#   fluids    services/fluids: flat target + wine/spirits from 2026-11-01, before
#             that weight-based drinks target + 16 oz/training hour;
#             68 oz = EFSA's drinks share, the "clearly low" floor.
#   caffeine  ≤400 mg/day (FDA / EFSA, healthy adults). Bedtime residual is the
#             app's own decay model (services/caffeine.onboard_at_bed), reported as a
#             distribution with NO threshold — none is established, and an invented one
#             would read like a finding.
#   alcohol   US standard drinks (SDU, 14 g). NIAAA heavy drinking for men: >14/week
#             or ≥5 on any day. Canada's 2023 guidance: ≤2/week low risk, 3–6 moderate,
#             ≥7 increasingly high. Both shown — they disagree, and that is the point.
# A partial month (the current one) is computed through yesterday and labelled.

async def _intake_month(db: AsyncSession, start: date, end: date) -> dict:
    from collections import defaultdict
    from app.models import HealthMetric
    from app.foodday import food_day_bounds_utc, food_day_of_utc
    from app.services import fluids
    from app.services.bedtime import bedtime_for_day
    from app.services.caffeine import day_instances, onboard_at_bed

    n_days = (end - start).days + 1
    out: dict = {"days": n_days}

    fl = await fluids.fluid_days(db, start, end)
    tg = await fluids.targets(db, start, end)
    if fl:
        tot = [v["total"] for v in fl.values()]
        out["fluids"] = {
            "days": len(fl),
            "mean_oz": stats.mean(tot),
            "mean_target": stats.mean(tg[d]["target"] for d in fl),
            "met": sum(1 for d, v in fl.items() if v["total"] >= tg[d]["target"]),
            "under_floor": sum(1 for v in tot if v < fluids.FLOOR_OZ),
            "share": {k: sum(v[k] for v in fl.values()) / max(sum(tot), 1)
                      for k in ("water", "caffeine", "other")},
            "unknown_n": sum(v["unknown_n"] for v in fl.values()),
        }

    mg, bed = [], []
    d = start
    while d <= end:
        rows = await day_instances(db, d)
        if rows:
            mg.append(sum(r.mg for r in rows))
            b, actual = await bedtime_for_day(db, d)
            if b and actual:
                ob = onboard_at_bed(rows, b)
                if ob is not None:
                    bed.append(ob)
        d += timedelta(days=1)
    if mg:
        out["caffeine"] = {"days": len(mg), "mean": stats.mean(mg), "max": max(mg),
                           "over400": sum(1 for x in mg if x > 400),
                           "bed_median": stats.median(bed) if bed else None,
                           "bed_max": max(bed) if bed else None, "bed_n": len(bed)}

    lo, hi = food_day_bounds_utc(start)[0], food_day_bounds_utc(end)[1]
    sdu: dict = defaultdict(float)
    for ts, v in (await db.execute(
        select(HealthMetric.timestamp, HealthMetric.value)
        .where(HealthMetric.metric_type == "alcohol")
        .where(HealthMetric.timestamp >= lo).where(HealthMetric.timestamp < hi)
    )).all():
        dd = food_day_of_utc(ts)
        if start <= dd <= end:
            sdu[dd] += v or 0.0
    total = sum(sdu.values())
    # Alcohol is logged COMPLETELY, travel included (2026-10-25), so an absent day
    # is a real zero and the rate is over every day of the month. Food-log coverage was
    # tried as a completeness proxy and was wrong: during the Oct trip food and caffeine
    # went unlogged while every drink was. Caffeine and fluids, which DO go unlogged,
    # are averaged over their logged days and say how many there were.
    out["alcohol"] = {"total": total, "per_week": total / n_days * 7,
                      "drink_days": sum(1 for v in sdu.values() if v > 0),
                      "max_day": max(sdu.values(), default=0.0),
                      "days_ge5": sum(1 for v in sdu.values() if v >= 5)}
    return out


def _intake_line(label: str, m: dict) -> str:
    parts = [f"  {label} ({m['days']} days):"]
    f = m.get("fluids")
    if f:
        sh = f["share"]
        parts.append(
            f"    FLUIDS {f['mean_oz']:.0f} oz/day vs target {f['mean_target']:.0f} (logged {f['days']}/{m['days']} d) · "
            f"target met {f['met']}/{f['days']} d · under 68 oz floor {f['under_floor']} d · "
            f"from water {sh['water']:.0%} / caffeinated drinks {sh['caffeine']:.0%} / other {sh['other']:.0%}"
            + (f" · {f['unknown_n']} drinks of unknown volume not counted" if f["unknown_n"] else ""))
    else:
        parts.append("    FLUIDS no drinks logged")
    c = m.get("caffeine")
    if c:
        bed = (f" · still on board at bedtime: median {c['bed_median']:.0f} mg, worst {c['bed_max']:.0f} mg "
               f"({c['bed_n']} nights)" if c["bed_median"] is not None else "")
        parts.append(f"    CAFFEINE {c['mean']:.0f} mg/day on {c['days']}/{m['days']} logged d · max {c['max']:.0f} · "
                     f"days >400 mg {c['over400']}{bed}")
    else:
        parts.append("    CAFFEINE none logged")
    a = m["alcohol"]
    parts.append(f"    ALCOHOL {a['per_week']:.1f} SDU/week ({a['total']:.1f} total) · "
                 f"{a['drink_days']} drinking days · max {a['max_day']:.1f} SDU in a day · "
                 f"days ≥5 SDU {a['days_ge5']} (complete log — a day without an entry is a real zero)")
    return "\n".join(parts)


async def intake_block(db: AsyncSession, month_start: date, month_end: date) -> str:
    """Fluids / caffeine / alcohol for the month and the two before it, vs references."""
    try:
        yesterday = local_today() - timedelta(days=1)
        months = []
        s, e = month_start, min(month_end, yesterday)
        for i in range(3):
            if e >= s:
                full_end = s.replace(day=calendar.monthrange(s.year, s.month)[1])
                lbl = s.strftime("%b %Y") + (f" (partial, through {e:%b %-d})" if e < full_end else "")
                months.append((("THIS MONTH " if i == 0 else "") + lbl, await _intake_month(db, s, e)))
            e = s - timedelta(days=1)
            s = e.replace(day=1)
        if not months:
            return ""
        return (
            "INTAKE vs REFERENCES (computed in code; cite these numbers, never others):\n"
            "  References — fluids: daily target from ALL drinks (water + caffeinated drinks +\n"
            "  other liquids; alcohol excluded) = 100 oz + 4 oz per SDU of wine or spirits from\n"
            "  2026-11-01, weight-based base + 16 oz per training hour before that;\n"
            "  68 oz is the clearly-low floor. Caffeine: ≤400 mg/day for healthy adults; the\n"
            "  bedtime figure has NO established threshold — describe it, don't grade it.\n"
            "  Alcohol (US standard drinks): NIAAA heavy drinking for men = >14/week or ≥5 in a\n"
            "  day; Canada 2023 = ≤2/week low risk, 3-6 moderate, ≥7 increasingly high.\n"
            "  Logging: ALCOHOL is logged completely, travel included. CAFFEINE and FLUIDS go\n"
            "  unlogged on some days (e.g. while travelling) — their figures are averages over\n"
            "  logged days only; never read an unlogged day as zero intake.\n"
            + "\n".join(_intake_line(lbl, m) for lbl, m in months) + "\n\n"
        )
    except Exception as exc:
        logger.warning("intake_block failed: %s", exc)
        return ""
