import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api.js";

// Psychomotor Vigilance Task — brief version (PVT-B), 2 min, protocol v2.
//
// This component is a STOPWATCH ONLY. It captures raw per-trial timing and posts it;
// every summary metric (lapses, reciprocal RT, deciles) is derived server-side in
// services/pvt.py. That keeps scoring in one place and means a scoring change never
// requires re-collecting data.
//
// PROTOCOL v2 (2026-08-12) — collection parameters changed, so v1 and v2 sessions are
// NOT comparable and the server segments them by the `protocol` field below.
//
// TIMING NOTES — the whole validity of this rests on these:
//  * performance.now() throughout, never Date.now() (wall clock can jump).
//  * The response timestamp comes from the EVENT, not from calling performance.now()
//    inside the handler — see eventNow().
//  * The stimulus timestamp is taken inside a requestAnimationFrame callback AFTER
//    the paint that shows it, not when state is set — otherwise every RT is short by
//    up to one frame plus React's render time.
//  * pointerdown, never click: click fires materially later on touch devices.
//  * Touch + display latency adds roughly 30-80 ms. That is a near-constant offset
//    for a given device, so within-person comparison is unaffected — but it does mean
//    absolute lapse counts should not be read against published norms.

const PROTOCOL = 3;

// v3 (2026-09-23, note 52): 90 s. The v2 reasoning below assumed a 15-25 ms day-to-day
// spread and a 2.7 ms SE; measured on 11 real sessions they are 7.9 ms and 4.7 ms, so
// shortening does cost precision (mean 1/RT reliability 0.75 at 120 s → 0.66 at 90 s).
// It was chosen anyway because adherence was the binding constraint (~1.6 runs/week) —
// full numbers in services/pvt.py. The v2 text follows, kept as the record.
//
// 2 minutes, not Basner's 3. This is a deliberate, measured trade against adherence:
// the test is one component of a <5 min daily battery, and a test that gets taken
// beats a better one that gets skipped (v1 managed 2 runs in its first 6 days).
//
// The precision cost is negligible. At the corrected ISI, 2 min gives ~42 trials and
// SE(median) 2.7 ms against 3 min's ~64 and 2.2 ms — and BOTH are ~10x smaller than
// the 15-25 ms day-to-day spread of the median, which is what actually limits
// detection. Note this still collects MORE trials than v1 did in 3 minutes (~37).
//
// What it does cost: lapse counts scale with trials (~6.7 +/- 2.6 at 2 min vs
// ~10.2 +/- 3.2 at 3 min), so the already-poor lapse precision gets worse, and
// time-on-task has less time to develop. Both were aggregate-only already.
const TEST_MS = 90_000;

// Basner, Mollicone & Dinges 2011: "The inter-stimulus intervals varied randomly from
// 1 to 4 s (including a 1 s RT feedback interval)."
//
// So the ISI runs from the RESPONSE to the next stimulus and the 1 s feedback is
// INSIDE it, not additional to it. v1 read this as a 2-5 s blank wait *plus* a
// separate 1 s feedback (~3-6 s response-to-stimulus), which cost ~40% of the trials:
// ~37 per 3 min where the correct interval yields ~64, inflating the SE of every
// metric by sqrt(64/37) ~ 1.32x.
const ISI_MIN_MS = 1000;
const ISI_MAX_MS = 4000;
const FEEDBACK_MS = 1000;     // the leading, feedback-showing part of the ISI

const FALSE_START_MS = 100;   // faster than this = anticipation, not reaction
const COUNTER_TICK_MS = 50;   // stimulus counter is displayed in 50 ms steps

// A trial with no response ends here and is recorded as a non-response rather than
// hanging. v1 had NO timeout at all: the only exit from the stimulus phase was a tap,
// so a genuine microsleep stalled the test indefinitely (TEST_MS is only checked
// between trials). Basner used 30 s, but 30 s inside a 180 s test would consume a
// sixth of the session, so this deviates deliberately.
const STIMULUS_TIMEOUT_MS = 10_000;

const randomIsi = () => ISI_MIN_MS + Math.random() * (ISI_MAX_MS - ISI_MIN_MS);

// Trusted UI events carry a DOMHighResTimeStamp set by the user agent when the event
// was CREATED — before it was queued and dispatched — on the same timeline as
// performance.now(). Reading it removes main-thread scheduling jitter from the
// measurement, which calling performance.now() inside the handler cannot do.
// Guarded because a few engines historically reported epoch milliseconds here: only
// accept a value that is on this timeline, in the past, and recent.
const eventNow = (e) => {
  const now = performance.now();
  const ts = e?.timeStamp;
  if (typeof ts === "number" && ts > 0 && ts <= now && now - ts < 1000) return ts;
  return now;
};

export default function PvtTest({ onClose, onDone }) {
  const [phase, setPhase] = useState("intro");   // intro | waiting | stimulus | feedback | done
  const [shownMs, setShownMs] = useState(0);     // counter shown during stimulus
  const [lastRt, setLastRt] = useState(null);
  const [flash, setFlash] = useState(null);      // "false-start" | "timeout" | null
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState(null);

  const trials = useRef([]);
  const testStart = useRef(0);
  const stimulusAt = useRef(0);
  const isiRef = useRef(0);
  const blankRef = useRef(0);
  const timers = useRef([]);
  const rafRef = useRef(0);
  const phaseRef = useRef("intro");
  const firstRef = useRef(true);
  const shownRef = useRef(-1);
  const interrupted = useRef(false);

  const setPhaseBoth = (p) => { phaseRef.current = p; setPhase(p); };

  const clearTimers = () => {
    timers.current.forEach(clearTimeout);
    timers.current = [];
    cancelAnimationFrame(rafRef.current);
  };

  // ── one trial ───────────────────────────────────────────────────────────────
  const nextTrial = useCallback(() => {
    if (performance.now() - testStart.current >= TEST_MS) {
      setPhaseBoth("done");
      return;
    }
    const isi = randomIsi();
    // The feedback that just ran is the leading part of the ISI, so only the
    // remainder is spent blank. The very first trial has no preceding feedback, so it
    // waits the whole interval (a 0 ms opening blank would fire the stimulus the
    // instant the test began).
    const blank = firstRef.current ? isi : Math.max(0, isi - FEEDBACK_MS);
    firstRef.current = false;
    isiRef.current = isi;
    blankRef.current = blank;
    setPhaseBoth("waiting");
    shownRef.current = -1;
    setShownMs(0);

    timers.current.push(setTimeout(() => {
      setPhaseBoth("stimulus");
      // Timestamp SYNCHRONOUSLY first. An earlier version took it only inside the
      // requestAnimationFrame callback (to get the true post-paint moment), which
      // silently produced garbage whenever rAF was throttled — backgrounded tab,
      // low-power mode, app switch mid-test. `stimulusAt` then kept a stale value
      // and every RT came out as elapsed-since-test-start (observed: 24-148 s).
      // rAF still refines it to post-paint when it does run; the difference is one
      // frame (~16 ms) and near-constant, whereas the failure mode was total.
      stimulusAt.current = performance.now();
      rafRef.current = requestAnimationFrame(() => {
        stimulusAt.current = performance.now();
        const tick = () => {
          if (phaseRef.current !== "stimulus") return;
          const el = performance.now() - stimulusAt.current;
          // Only re-render when the DISPLAYED value actually changes. The counter is
          // shown in 50 ms steps but rAF fires ~60x/s, so setting state every frame
          // made ~2/3 of renders no-ops — needless main-thread work in exactly the
          // window where input latency is being measured.
          const step = Math.round(el / COUNTER_TICK_MS) * COUNTER_TICK_MS;
          if (step !== shownRef.current) {
            shownRef.current = step;
            setShownMs(step);
          }
          rafRef.current = requestAnimationFrame(tick);
        };
        rafRef.current = requestAnimationFrame(tick);
      });

      // Non-response guard. Recorded at the timeout value and kept, not discarded:
      // the server scores it as a lapse (as Basner does), because a microsleep is the
      // most diagnostic event this test can capture.
      timers.current.push(setTimeout(() => {
        if (phaseRef.current !== "stimulus") return;
        clearTimers();
        trials.current.push({
          isi_ms: Math.round(isiRef.current),
          blank_ms: Math.round(blankRef.current),
          rt_ms: STIMULUS_TIMEOUT_MS,
          t_stim_ms: Math.round(stimulusAt.current - testStart.current),
          t_resp_ms: Math.round(stimulusAt.current - testStart.current) + STIMULUS_TIMEOUT_MS,
          false_start: false,
          timed_out: true,
        });
        setFlash("timeout");
        setPhaseBoth("feedback");
        timers.current.push(setTimeout(() => { setFlash(null); nextTrial(); }, FEEDBACK_MS));
      }, STIMULUS_TIMEOUT_MS));
    }, blank));
  }, []);

  const respond = useCallback((e) => {
    const p = phaseRef.current;
    const at = eventNow(e);

    if (p === "waiting") {
      // Tapped before the stimulus — an anticipation. Record it and restart the
      // interval so the participant can't game the ISI by tapping continuously.
      // The elapsed wait is kept: tapping 200 ms into a blank and tapping 2.9 s in
      // are different behaviours, and v1 stored them identically.
      clearTimers();
      trials.current.push({
        isi_ms: Math.round(isiRef.current),
        blank_ms: Math.round(blankRef.current),
        rt_ms: null,
        t_stim_ms: null,
        t_resp_ms: Math.round(at - testStart.current),
        false_start: true,
        timed_out: false,
      });
      setFlash("false-start");
      timers.current.push(setTimeout(() => { setFlash(null); nextTrial(); }, 1200));
      return;
    }

    if (p === "stimulus") {
      const rt = at - stimulusAt.current;
      clearTimers();
      const tStim = Math.round(stimulusAt.current - testStart.current);
      trials.current.push({
        isi_ms: Math.round(isiRef.current),
        blank_ms: Math.round(blankRef.current),
        rt_ms: Math.round(rt),
        // Absolute offsets from test start. These are what let the server verify the
        // timeline structurally — monotonic onsets, rt == t_resp - t_stim — instead
        // of guessing at clock health from RT magnitude, which cannot tell a broken
        // stopwatch from a genuine microsleep.
        t_stim_ms: tStim,
        t_resp_ms: tStim + Math.round(rt),
        false_start: rt < FALSE_START_MS,
        timed_out: false,
      });
      setLastRt(Math.round(rt));
      setPhaseBoth("feedback");
      timers.current.push(setTimeout(nextTrial, FEEDBACK_MS));
    }
  }, [nextTrial]);

  const start = () => {
    trials.current = [];
    testStart.current = performance.now();
    firstRef.current = true;
    interrupted.current = false;
    setLastRt(null);
    nextTrial();
  };

  // Backgrounding the tab throttles setTimeout to >=1 s and can "show" a stimulus the
  // participant never sees. The run is flagged rather than silently scored.
  useEffect(() => {
    const onHide = () => {
      const p = phaseRef.current;
      if (document.hidden && (p === "waiting" || p === "stimulus" || p === "feedback")) {
        interrupted.current = true;
      }
    };
    document.addEventListener("visibilitychange", onHide);
    return () => document.removeEventListener("visibilitychange", onHide);
  }, []);

  // ── submit ──────────────────────────────────────────────────────────────────
  useEffect(() => {
    if (phase !== "done" || saving || result) return;
    setSaving(true);
    const durationS = (performance.now() - testStart.current) / 1000;
    api.submitPvt({
      trials: trials.current,
      duration_s: Math.round(durationS * 10) / 10,
      protocol: PROTOCOL,
      interrupted: interrupted.current,
      device: navigator.userAgent.includes("iPhone") ? "iPhone"
            : navigator.userAgent.includes("Mac") ? "Mac" : "other",
      started_at: new Date(Date.now() - durationS * 1000).toISOString(),
    })
      .then((r) => { setResult(r); onDone?.(r); })
      .catch(() => setResult({ error: true }))
      .finally(() => setSaving(false));
  }, [phase, saving, result, onDone]);

  useEffect(() => () => clearTimers(), []);

  const running = phase === "waiting" || phase === "stimulus" || phase === "feedback";
  const remaining = running
    ? Math.max(0, Math.ceil((TEST_MS - (performance.now() - testStart.current)) / 1000))
    : null;

  return (
    <div
      className="fixed inset-0 z-[60] flex select-none flex-col bg-[#0f172a] touch-none"
      onPointerDown={running ? respond : undefined}
      style={{ WebkitUserSelect: "none", WebkitTouchCallout: "none" }}
    >
      {phase === "intro" && (
        <div className="flex flex-1 flex-col justify-center px-8">
          <p className="text-xs font-semibold uppercase tracking-widest text-indigo-400">
            Vigilance test
          </p>
          <h2 className="mt-2 text-2xl font-bold text-white">90 seconds</h2>
          <p className="mt-4 text-sm leading-relaxed text-slate-300">
            A counter appears at random intervals. Tap the screen the moment you see it —
            as fast as you can.
          </p>
          <p className="mt-3 text-xs leading-relaxed text-slate-500">
            Don't tap early: taps before the counter are recorded as false starts.
            Take it at a consistent time of day — performance swings with your body
            clock, and comparing tests taken at different hours mostly measures the
            clock, not you.
          </p>
          <button
            onClick={start}
            className="mt-8 rounded-xl bg-indigo-600 py-3.5 text-base font-semibold text-white active:bg-indigo-500"
          >
            Start
          </button>
          <button onClick={onClose} className="mt-3 py-2 text-sm text-slate-500">
            Cancel
          </button>
        </div>
      )}

      {running && (
        <div className="relative flex flex-1 items-center justify-center">
          <span className="absolute top-6 right-6 text-xs tabular-nums text-slate-600">
            {remaining}s
          </span>

          {phase === "waiting" && !flash && (
            <div className="h-28 w-44 rounded-2xl border-2 border-slate-800" />
          )}

          {flash === "false-start" && (
            <p className="text-lg font-semibold text-amber-400">Too early</p>
          )}

          {flash === "timeout" && (
            <p className="text-lg font-semibold text-rose-400">No response</p>
          )}

          {phase === "stimulus" && (
            <div className="flex h-28 w-44 items-center justify-center rounded-2xl border-2 border-rose-500 bg-rose-500/10">
              <span className="text-4xl font-bold tabular-nums text-amber-300">
                {shownMs}
              </span>
            </div>
          )}

          {phase === "feedback" && !flash && (
            <div className="flex h-28 w-44 items-center justify-center rounded-2xl border-2 border-slate-700">
              <span className="text-4xl font-bold tabular-nums text-slate-300">{lastRt}</span>
            </div>
          )}

          <p className="absolute bottom-16 text-[11px] text-slate-600">
            tap anywhere the moment the counter appears
          </p>
        </div>
      )}

      {phase === "done" && (
        <div className="flex flex-1 flex-col justify-center px-8">
          <p className="text-xs font-semibold uppercase tracking-widest text-emerald-400">Done</p>
          {saving && <p className="mt-4 text-sm text-slate-400">Saving…</p>}
          {result && !result.error && (
            <>
              <h2 className="mt-2 text-2xl font-bold text-white">
                {result.median_rt} ms <span className="text-base font-normal text-slate-400">median</span>
              </h2>
              {/* Ordered by measured precision. Median and fastest-10% have a within-
                  session CV around 1%; lapses are at 37% and belong below the fold,
                  not in the headline they used to occupy. */}
              <div className="mt-5 space-y-1.5 text-sm text-slate-300">
                <Row label="Fastest 10%" value={`${result.fastest10_mean} ms`} />
                <Row label="Response speed" value={`${result.mean_reciprocal_rt} /s`} />
                <Row label="Valid trials" value={result.n_trials} />
                <Row label="False starts" value={result.false_starts} />
              </div>
              <p className="mt-4 text-[11px] leading-relaxed text-slate-500">
                Lapses {result.lapses_355} · slowest 10% {result.slowest10_mean} ms
                {result.timeouts ? ` · ${result.timeouts} no-response` : ""} — these are
                noisy in any single run and are worth reading only as a trend.
              </p>
              {(result.timing_suspect || result.interrupted) && (
                <p className="mt-3 rounded-lg bg-amber-500/10 p-2 text-[11px] leading-relaxed text-amber-400">
                  {result.interrupted
                    ? "The app was backgrounded mid-test, so the intervals may have been stretched. "
                    : ""}
                  {result.timing_suspect
                    ? "The timing checks on this run didn't come out clean. "
                    : ""}
                  Treat this session as unreliable.
                </p>
              )}
              <p className="mt-4 text-[11px] leading-relaxed text-slate-500">
                Taken at {result.local_time} — {result.circadian_note}. Time of day is
                stored so it can be controlled for.
              </p>
            </>
          )}
          {result?.error && (
            <p className="mt-4 text-sm text-rose-400">Couldn't save that run.</p>
          )}
          <button
            onClick={onClose}
            className="mt-8 rounded-xl bg-slate-700 py-3 text-base font-semibold text-white active:bg-slate-600"
          >
            Close
          </button>
        </div>
      )}
    </div>
  );
}

function Row({ label, value }) {
  return (
    <div className="flex justify-between border-b border-slate-800 pb-1.5">
      <span className="text-slate-400">{label}</span>
      <span className="font-semibold tabular-nums text-white">{value}</span>
    </div>
  );
}
