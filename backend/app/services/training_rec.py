"""Rule-based training recommendation + strength progression hint.

Moved out of routers/metrics.py (2026-10-21). `training_rec` no longer drives the coach
card (docs/COACH.md) but experiment eligibility and /today still read it.
"""


def _training_rec(dm, sleep_quality_pct: float | None = None) -> dict | None:
    """
    Rule-based training recommendation from objective recovery metrics only.
    Returns {label, rationale, color, signals} or None if insufficient data.
    sleep_quality_pct: (deep_min + rem_min) / total_min * 100 from today's sleep session.
    """
    if dm is None:
        return None

    hrv_z  = dm.hrv_z_score
    rhr_z  = dm.rhr_z_score
    debt   = dm.sleep_debt_hours
    acwr   = dm.acwr

    if all(v is None for v in [hrv_z, rhr_z, debt, acwr]):
        return None

    # ── Hard red flags → REST ─────────────────────────────────────────────────
    # rhr_z > 0 means RHR is *above* baseline (elevated = bad); < 0 means suppressed (good).
    if ((hrv_z  is not None and hrv_z  < -1.5) or
        (debt   is not None and debt   >  2.5) or
        (acwr   is not None and acwr   >  1.5) or
        (rhr_z  is not None and rhr_z  >  1.5)):

        if   hrv_z is not None and hrv_z < -1.5:
            reason = f"HRV is {abs(hrv_z):.1f}σ below baseline"
        elif debt  is not None and debt  > 2.5:
            reason = f"{debt:.1f}h of accumulated sleep debt"
        elif acwr  is not None and acwr  > 1.5:
            reason = f"training load is {acwr:.2f}× your rolling average"
        else:
            reason = "RHR is significantly elevated"
        result = {"label": "REST", "rationale": f"Rest today — {reason}.", "color": "#ef4444"}

    # ── Soft caution → EASY ───────────────────────────────────────────────────
    elif ((hrv_z is not None and hrv_z < -0.5) or
          (debt  is not None and debt  >  1.0) or
          (acwr  is not None and acwr  >  1.3) or
          (rhr_z is not None and rhr_z >  0.5)):

        if   hrv_z is not None and hrv_z < -0.5:
            reason = f"HRV is {abs(hrv_z):.1f}σ below baseline"
        elif debt  is not None and debt  > 1.0:
            reason = f"{debt:.1f}h sleep debt"
        elif acwr  is not None and acwr  > 1.3:
            reason = f"ACWR at {acwr:.2f}"
        else:
            reason = "RHR above baseline"
        result = {"label": "EASY", "rationale": f"Easy effort — {reason}. Zone 2 or mobility.", "color": "#f59e0b"}

    # ── Green zone ────────────────────────────────────────────────────────────
    elif acwr is not None and acwr < 0.8:
        result = {"label": "MODERATE",
                  "rationale": f"Moderate session — ACWR at {acwr:.2f} is below optimal; load can increase.",
                  "color": "#10b981"}

    elif hrv_z is not None and hrv_z >= 0.5:
        acwr_str = f", ACWR {acwr:.2f}" if acwr is not None else ""
        result = {"label": "HARD",
                  "rationale": f"Push today — HRV is {hrv_z:.1f}σ above baseline{acwr_str}.",
                  "color": "#818cf8"}

    else:
        # Baseline — moderate
        if   hrv_z is not None: reason = f"HRV near baseline ({hrv_z:+.1f}σ)"
        elif debt  is not None: reason = f"sleep debt low ({debt:.1f}h)"
        else:                   reason = "recovery metrics look adequate"
        result = {"label": "MODERATE",
                  "rationale": f"Moderate effort — {reason}. A steady session is well-timed.",
                  "color": "#10b981"}

    # ── Contributing signal chips ─────────────────────────────────────────────
    signals = []

    # HRV — show if meaningfully above or below baseline
    if hrv_z is not None:
        if hrv_z >= 0.5:
            signals.append({"chip": "↑ HRV", "color": "#10b981"})
        elif hrv_z < -0.5:
            signals.append({"chip": "↓ HRV", "color": "#f59e0b" if hrv_z > -1.0 else "#ef4444"})

    # ACWR — show if outside optimal band
    if acwr is not None:
        if acwr > 1.3:
            signals.append({"chip": "↑ ACWR", "color": "#f59e0b" if acwr <= 1.5 else "#ef4444"})
        elif acwr < 0.8:
            signals.append({"chip": "↓ ACWR", "color": "#6ee7b7"})

    # Sleep quality — prefer deep+REM %, fall back to sleep debt
    if sleep_quality_pct is not None:
        if sleep_quality_pct < 15:
            signals.append({"chip": "↓ Deep+REM", "color": "#f59e0b"})
        elif sleep_quality_pct >= 25:
            signals.append({"chip": "↑ Deep+REM", "color": "#10b981"})
    elif debt is not None:
        if debt > 1.0:
            signals.append({"chip": f"+{debt:.1f}h debt", "color": "#ef4444"})
        elif debt > 0.5:
            signals.append({"chip": f"+{debt:.1f}h debt", "color": "#f59e0b"})

    # RHR — show if meaningfully deviated
    if rhr_z is not None:
        if rhr_z > 0.5:
            signals.append({"chip": "↑ RHR", "color": "#f59e0b" if rhr_z <= 1.0 else "#ef4444"})
        elif rhr_z < -0.5:
            signals.append({"chip": "↓ RHR", "color": "#6ee7b7"})

    result["signals"] = signals
    return result


def _strength_hint(dm, lifted_yesterday: bool) -> dict | None:
    """
    Strength-specific progression hint shown alongside the training rec (only when
    Hevy history exists). Deliberately reads the HRV/RHR/sleep components, NOT the
    blended readiness score (the composite showed no validity against subjective
    energy at n=75, 2026-07-10) and NOT ACWR (its component pointed the wrong way
    for this user). Post-lift context matters: a strength day suppresses this
    user's next-morning HRV by ~0.35σ on average, so a moderate dip the morning
    after lifting is expected adaptation, not a warning.
    """
    if dm is None:
        return None
    hrv_z, rhr_z, debt = dm.hrv_z_score, dm.rhr_z_score, dm.sleep_debt_hours
    if hrv_z is None and rhr_z is None and debt is None:
        return None

    red = ((hrv_z is not None and hrv_z < -1.5 and not lifted_yesterday)
           or (hrv_z is not None and hrv_z < -2.0)   # deep dip is red even post-lift
           or (rhr_z is not None and rhr_z > 1.5)
           or (debt is not None and debt > 2.5))
    green = ((hrv_z is not None and hrv_z >= 0.3)
             and (rhr_z is None or rhr_z < 0.5)
             and (debt is None or debt < 1.0))

    # NO AUTOMATIC LOAD HOLD (2026-10-29). These used to say "hold loads" on
    # MAINTAIN / EXPECTED DIP days and "repeat last loads" on HOLD days. None of his morning
    # signals predicted how he lifted (54 sessions since 04-02: readiness ρ −0.20, HRV
    # −0.12, sleep h −0.16, readiness < 50 / sleep < 6 h days no worse).
    # Targets now stand every day; easing off is his call (the class board's Easy day
    # switch) or the in-session rule: first working set misses by 2+ reps → one step down.
    if red:
        return {"label": "YOUR CALL", "color": "#f59e0b",
                "detail": "Recovery signals are well off this morning. Your lifting hasn't "
                          "dropped on days like this so far, so the targets stand — but if "
                          "you're not up to pushing, take an easy day and keep last "
                          "session's weights."}
    if (lifted_yesterday and hrv_z is not None and -2.0 <= hrv_z < -0.5
            and (rhr_z is None or rhr_z <= 1.0)):
        return {"label": "EXPECTED DIP", "color": "#38bdf8",
                "detail": f"HRV is {abs(hrv_z):.1f}σ down the morning after lifting — that's "
                          "your normal post-strength response, not a red flag. Train to "
                          "your targets."}
    if green:
        return {"label": "PROGRESS", "color": "#10b981",
                "detail": "Recovery signals are green — train to your targets."}
    return {"label": "ON TRACK", "color": "#94a3b8",
            "detail": "Train to your targets."}
