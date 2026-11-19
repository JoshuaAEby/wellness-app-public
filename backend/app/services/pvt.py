"""Psychomotor Vigilance Task — scoring.

The browser CAPTURES timing; this module SCORES it. That split is deliberate: it
keeps one source of truth for every metric, so a scoring change never requires
re-collecting data, and the client stays a dumb (therefore reliable) stopwatch.

## Protocol versions

Collection parameters changed on 2026-08-12, so every session records the `protocol`
it was collected under and is scored by that protocol's rules. Sessions are NOT
comparable across protocols and must not be pooled — segment by `protocol` in any
trend or correlation.

  v1 (2026-08-06 → 2026-08-12, 2 sessions) — 3 min
      Blank inter-stimulus wait 2-5 s PLUS a separate 1 s feedback, i.e. ~3-6 s
      response-to-stimulus. This was a misreading of the source: it yielded ~37
      trials where the correct interval yields ~64. No per-trial absolute offsets,
      and no stimulus timeout — a non-response hung the test indefinitely.

  v2 (2026-08-12 →) — 2 min
      Basner 2011 PVT-B interval as specified: "inter-stimulus intervals varied
      randomly from 1 to 4 s (including a 1 s RT feedback interval)". So response →
      1 s feedback → 0-3 s blank → stimulus, giving ~42 trials in 2 min — still more
      than v1 collected in 3. Per-trial absolute offsets from test start are
      recorded, and a non-response times out.

      The 2 min duration deviates from Basner's 3 deliberately, traded for adherence
      (this is one component of a <5 min daily battery). SE(median) is 2.7 ms vs
      2.2 ms at 3 min — both an order of magnitude under the 15-25 ms day-to-day
      spread that actually limits detection. Lapse counts scale with trials, so
      their already-poor precision degrades further; they were aggregate-only
      anyway.
      ** The 15-25 ms / 2.7 ms figures above were ASSUMED and are WRONG for this
      user — see v3. Kept as the record of why v2 was chosen. **

  v3 (2026-09-23 →) — 90 s, same interval/timeout rules as v2 (note 52)
      Measured on the 11 v2 sessions by truncating each to its first L seconds:
      day-to-day SD of the median is 7.9 ms (not 15-25) and the 2-min session's own
      SE(median) is 4.7 ms (bootstrap, not 2.7). Reliability of a single session
      (true-var / (true-var + SE^2)):
                       30 s   45 s   60 s   90 s   120 s
          mean 1/RT    0.38   0.49   0.58   0.66   0.75
          median RT    0.11    —     0.38   0.45   0.64
      No time-on-task decrement exists to lose (RT slope −7.6 ms/min, SD 18.6).
      The binding constraint was adherence — 11 sessions in 47 days, ~1.6/wk — and
      detectability of a sleep→PVT relation scales roughly with n × reliability:
      2 min at 1.6/wk ≈ 1.2 vs 90 s at 4/wk ≈ 2.6. 90 s pays only if it is taken
      more often; 60 s was rejected (median RT becomes mostly noise).
      v2 history stays usable: rescore_first() re-scores a v2 session's first 90 s
      as v3, and GET /api/pvt returns those alongside the originals.

## Parameters and why they are not hardcoded into collection

Raw trials are stored, so lapse thresholds stay a REPORTING choice. Basner 2011
(PVT-B, the 3-minute version) lowered the lapse threshold from 500 ms to 355 ms to
preserve sensitivity at the shorter duration; the classic 10-minute PVT uses 500 ms.
Both are computed rather than picking one and losing the other.

Note the 355 ms threshold was calibrated under "TSD, time on task <3 min, ISI between
2 and 4 s". v2's 1-4 s ISI is faster than that window, which biases the 355 ms lapse
count downward relative to Basner's norms. Irrelevant for within-person trend, which
is all this app claims; relevant if you ever compare to a published mean.

Worth knowing: the 3-minute PVT's equivalence to the 10-minute version is contested.
Basner's own validation found it tracked the full test well, but a later independent
study (Frontiers in Neuroscience, 2022) reported inadequate convergent validity. For
this app the risk is mild, because every metric is scored against the user's own
history rather than population norms — but it argues against treating an absolute
lapse count as meaningful in isolation.

## Precision — measured, not assumed

Bootstrapped within-session SE on real sessions (n≈37 trials, v1). This is what any
display of these numbers has to respect:

    median RT          SE 3.4 ms    CV  1.1%    <- usable per session
    fastest10 RT       SE 3.6 ms    CV  1.3%    <- usable per session
    mean 1/RT          SE 0.06      CV  1.9%    <- usable per session
    mean RT            SE 8.0 ms    CV  2.4%
    slowest10 RT       SE 38.5 ms   CV  8.6%    <- aggregate only
    performance score  SE 6.0 pp    CV  7.1%    <- aggregate only
    lapses >355        SE 2.2       CV 37.3%    <- aggregate only
    time-on-task slope |t| = 0.45, 0.30         <- aggregate only, |t|>2 to claim

Only median RT, fastest10 and mean 1/RT have the precision to be read off a single
session. Lapse count, performance score and the time-on-task slope are Poisson- or
regression-noise dominated at ~42 trials; they are computed and stored because
they aggregate usefully over weeks, NOT because a single day's value means anything.
Do not put them in a headline.
"""
import statistics as st
from typing import Optional

# The protocol the CURRENT client collects under. Bump this whenever a collection
# parameter changes (ISI, duration, timeout, stimulus), because doing so makes
# sessions incomparable — the client stamps every submission with the protocol it
# actually used, and scoring branches on it rather than assuming the latest.
PROTOCOL = 3

# v3 duration. v2 sessions are re-scored on their first V3_DURATION_MS to join the
# v3 series — valid because v3 changed ONLY the duration (same ISI, timeout, stimulus).
V3_DURATION_MS = 90_000

# Responses faster than this are anticipations, not reactions — the participant
# tapped before processing the stimulus. Standard PVT practice excludes them from
# the RT distribution and counts them separately. Basner: "Responses without a
# stimulus or RTs <100 ms were counted as false starts."
FALSE_START_MS = 100

# A response slower than this is a "lapse" — the headline sleep-sensitive measure.
LAPSE_355 = 355   # Basner 2011 PVT-B
LAPSE_500 = 500   # classic 10-minute PVT

# v2: the client stops waiting after this and records the trial as a non-response.
# Basner used 30 s for both PVT versions; 30 s inside a 180 s test would consume a
# sixth of the session, so this deviates deliberately. Timed-out trials are KEPT and
# scored at the timeout value (as Basner does) rather than discarded — a microsleep
# is the most diagnostic event the PVT can capture, and v1 threw exactly those away.
TIMEOUT_MS = 10_000

# v1 only. Trials slower than this were treated as broken.
#
# Tightened from 30 s to 5 s on 2026-08-06 after a real corruption: a throttled
# requestAnimationFrame left the client's stimulus timestamp stale, so RTs came back
# as elapsed-since-test-start (24-148 s). A 30 s ceiling let one such trial through
# and it became the session "median".
#
# This used RT MAGNITUDE as a proxy for clock health, which conflates "the browser
# broke" with "you briefly fell asleep". v2 replaces it with a structural check on
# per-trial absolute offsets (_timing_integrity) and no longer needs a magnitude
# ceiling. Retained unchanged so v1 sessions keep the scores they were stored with.
MAX_RT_MS_V1 = 5_000


def _timing_integrity(trials: list[dict]) -> list[str]:
    """Structural validation of the client's timeline (v2 only).

    v1 detected clock corruption by RT magnitude, which cannot distinguish a broken
    stopwatch from a genuine microsleep. v2 records each trial's stimulus and response
    as absolute offsets from test start, which makes corruption detectable from the
    SHAPE of the timeline instead:

      * stimulus onsets must strictly increase — the observed failure mode was a stale
        `stimulusAt` carried over from an earlier trial, which shows up here directly
      * a response must follow its own stimulus
      * rt_ms must equal t_resp_ms - t_stim_ms; a mismatch means the two clocks
        disagree, which is the signature of the rAF-throttling bug

    Returns human-readable problems; an empty list means the timeline is self-consistent.
    """
    problems: list[str] = []
    prev_stim = -1.0
    for i, t in enumerate(trials):
        ts, tr = t.get("t_stim_ms"), t.get("t_resp_ms")
        if ts is None:
            continue
        if ts <= prev_stim:
            problems.append(f"trial {i}: stimulus at {ts} ms not after previous ({prev_stim} ms)")
        prev_stim = ts
        if tr is None:
            continue
        if tr < ts:
            problems.append(f"trial {i}: response ({tr} ms) precedes its stimulus ({ts} ms)")
        rt = t.get("rt_ms")
        if rt is not None and abs((tr - ts) - rt) > 2:
            problems.append(
                f"trial {i}: rt_ms {rt} disagrees with offsets ({tr} - {ts} = {tr - ts})"
            )
    return problems


def _tot_slope(speeds: list[float]) -> Optional[float]:
    """OLS slope of response speed (1/RT) against trial index, per 10 trials.

    Time-on-task decrement is a real PVT signature — Basner: "These effects increase
    with time on task" — and it is free to compute because raw trials are stored.

    Negative = slowing across the session. Measured |t| on rested sessions is 0.3-0.5,
    far under the ~2 needed to claim a trend, so a SINGLE session's slope is noise.
    It is stored for aggregation across sessions, not for display.
    """
    n = len(speeds)
    if n < 8:
        return None
    xm = (n - 1) / 2.0
    ym = st.mean(speeds)
    denom = sum((j - xm) ** 2 for j in range(n))
    if denom == 0:
        return None
    return round(10.0 * sum((j - xm) * (speeds[j] - ym) for j in range(n)) / denom, 4)


def score(trials: list[dict], protocol: int = 1) -> dict:
    """Derive every summary metric from raw trials.

    `trials` is a list of dicts:
        isi_ms      int          response-to-stimulus interval (v2: feedback inclusive)
        blank_ms    int|None     v2: the blank portion of that interval
        rt_ms       int|None     reaction time; None if no valid reaction
        t_stim_ms   int|None     v2: stimulus onset, offset from test start
        t_resp_ms   int|None     v2: response, offset from test start
        false_start bool         responded before the stimulus
        timed_out   bool         v2: never responded; rt_ms is TIMEOUT_MS
    """
    ceiling = TIMEOUT_MS if protocol >= 2 else MAX_RT_MS_V1

    valid = [
        t["rt_ms"] for t in trials
        if not t.get("false_start")
        and t.get("rt_ms") is not None
        and FALSE_START_MS <= t["rt_ms"] <= ceiling
    ]
    false_starts = sum(
        1 for t in trials
        if t.get("false_start") or (t.get("rt_ms") is not None and t["rt_ms"] < FALSE_START_MS)
    )
    timeouts = sum(1 for t in trials if t.get("timed_out"))
    # Trials discarded as implausible. Surfaced rather than silently dropped: a
    # nonzero count means the CLIENT clock misbehaved, and the session should be
    # distrusted as a whole rather than scored on whatever survived the filter.
    # In v2 the client enforces the timeout, so exceeding it is by construction a bug.
    invalid = sum(
        1 for t in trials
        if not t.get("false_start") and t.get("rt_ms") is not None and t["rt_ms"] > ceiling
    )
    problems = _timing_integrity(trials) if protocol >= 2 else []

    base = {
        "protocol": protocol,
        "timeouts": timeouts,
        "invalid_trials": invalid,
        "timing_suspect": bool(problems) or invalid > 0,
        "timing_problems": problems[:10] or None,
    }

    if not valid:
        return {
            **base,
            "n_trials": 0, "median_rt": None, "mean_rt": None,
            "mean_reciprocal_rt": None, "slowest10_mean": None, "slowest10_speed": None,
            "fastest10_mean": None, "lapses_355": 0, "lapses_500": 0,
            "false_starts": false_starts, "perf_score": None, "tot_slope": None,
        }

    ordered = sorted(valid)
    k = max(1, round(len(ordered) * 0.10))
    lapses_355 = sum(1 for v in valid if v > LAPSE_355)

    return {
        **base,
        "n_trials": len(valid),
        "median_rt": round(st.median(valid), 1),
        "mean_rt": round(st.mean(valid), 1),
        # Reciprocal (response speed, 1/s). The standard transform in sleep research:
        # RT distributions are right-skewed, and 1/RT is far closer to normal, which
        # makes it the better input for the correlation engine's parametric steps.
        # It also bounds the leverage of a timed-out trial, which is why timeouts can
        # be kept in the distribution rather than discarded.
        "mean_reciprocal_rt": round(st.mean([1000.0 / v for v in valid]), 3),
        # Slowest and fastest deciles separate the two things sleep loss does:
        # it inflates the slow tail (lapses) more than it shifts optimum responding.
        # Basner reports the slow decile as 1/RT and the fast decile as raw ms; both
        # forms of the slow decile are kept — the reciprocal is the norm-comparable
        # one and is far better behaved when a timeout lands in the decile, though on
        # real data it is no less noisy (CV 8.7% vs 8.6%).
        "slowest10_mean": round(st.mean(ordered[-k:]), 1),
        "slowest10_speed": round(st.mean([1000.0 / v for v in ordered[-k:]]), 3),
        "fastest10_mean": round(st.mean(ordered[:k]), 1),
        "lapses_355": lapses_355,
        "lapses_500": sum(1 for v in valid if v > LAPSE_500),
        "false_starts": false_starts,
        # Basner's composite: 100% minus lapses and false starts as a share of valid
        # stimuli plus false starts. Inherits the lapse count's Poisson noise
        # (measured CV 7.1%) — aggregate-only, same as lapses.
        "perf_score": round(
            100.0 * (1 - (lapses_355 + false_starts) / (len(valid) + false_starts)), 1
        ),
        "tot_slope": _tot_slope([1000.0 / v for v in valid]),
    }


def rescore_first(trials: list[dict], ms: int = V3_DURATION_MS, protocol: int = PROTOCOL) -> dict:
    """Score only the trials whose stimulus (or, for a false start, response) fell in
    the first `ms` of the session — how a v2 session joins the v3 series."""
    def t(tr: dict) -> float:
        v = tr.get("t_stim_ms")
        return v if v is not None else (tr.get("t_resp_ms") or 0)
    return score([tr for tr in trials if t(tr) <= ms], protocol=protocol)


def circadian_note(local_minute: Optional[int]) -> Optional[str]:
    """Human label for when the test was taken.

    Not a judgement — PVT is strongly circadian, so the time of day is a covariate
    to CONTROL for, not a thing to be scored on. Sleep inertia dominates roughly the
    first 30-60 min after waking, which is why very early sessions are worth flagging
    for exclusion rather than silently mixing in.
    """
    if local_minute is None:
        return None
    h = local_minute // 60
    if h < 6:
        return "pre-dawn — sleep inertia likely, treat with caution"
    if h < 9:
        return "early morning — sleep inertia may still be present"
    if h < 12:
        return "mid-morning"
    if h < 17:
        return "afternoon"
    if h < 21:
        return "evening"
    return "late evening — circadian trough approaching"
