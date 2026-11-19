import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api.js";

// Maximum-rate finger tapping — 15 s block.
//
// This is a STOPWATCH ONLY, same split as PvtTest: it records raw tap offsets and posts
// them; every metric is derived server-side in services/tapping.py.
//
// Its role in the battery is as a CONTROL for the PVT, not a headline. Sleep loss slows
// PVT responses — but so do motor and peripheral factors (cold hands, a stiff shoulder,
// a different phone case). If PVT slows and tapping doesn't, the slowing is attentional.
// If both slow, it isn't specific to vigilance.
//
// TIMING: the same care as PvtTest applies, but the stakes are lower — this measures
// intervals BETWEEN taps, so a constant per-device input latency cancels out entirely.
// What matters is jitter, not offset.

const DURATION_MS = 15_000;

// Mirrors the helper in PvtTest.jsx. Deliberately duplicated rather than extracted:
// PvtTest is deployed and collecting, and pulling a shared module out of it is a
// refactor that doesn't belong in this change. Worth unifying if a third test needs it.
//
// Trusted UI events carry a DOMHighResTimeStamp set by the user agent when the event was
// CREATED, before dispatch, on the same timeline as performance.now(). Reading it removes
// main-thread scheduling jitter — which matters more here than in PVT, because at ~6 taps
// per second the handler is competing with its own re-renders.
const eventNow = (e) => {
  const now = performance.now();
  const ts = e?.timeStamp;
  if (typeof ts === "number" && ts > 0 && ts <= now && now - ts < 1000) return ts;
  return now;
};

export default function TappingTest({ onClose, onDone }) {
  const [phase, setPhase] = useState("intro");   // intro | running | done
  const [count, setCount] = useState(0);
  const [remaining, setRemaining] = useState(Math.ceil(DURATION_MS / 1000));
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState(null);

  const taps = useRef([]);
  const startAt = useRef(0);
  const phaseRef = useRef("intro");
  const timer = useRef(null);
  const tick = useRef(null);
  const interrupted = useRef(false);

  const setPhaseBoth = (p) => { phaseRef.current = p; setPhase(p); };

  const stop = useCallback(() => {
    clearTimeout(timer.current);
    clearInterval(tick.current);
  }, []);

  const finish = useCallback(() => {
    stop();
    setPhaseBoth("done");
  }, [stop]);

  const onTap = useCallback((e) => {
    if (phaseRef.current === "done") return;
    const at = eventNow(e);

    // The block starts on the FIRST tap, not on a countdown. That removes reaction time
    // from the measurement entirely and means the full 15 s is spent tapping.
    if (phaseRef.current === "intro") {
      startAt.current = at;
      taps.current = [0];
      setCount(1);
      setPhaseBoth("running");
      timer.current = setTimeout(finish, DURATION_MS);
      tick.current = setInterval(() => {
        const left = Math.max(0, DURATION_MS - (performance.now() - startAt.current));
        setRemaining(Math.ceil(left / 1000));
      }, 100);
      return;
    }

    const offset = at - startAt.current;
    if (offset > DURATION_MS) return;
    taps.current.push(Math.round(offset));
    setCount(taps.current.length);
  }, [finish]);

  // Backgrounding mid-block loses taps and stretches the wall clock. Flag rather than
  // silently score it.
  useEffect(() => {
    const onHide = () => {
      if (document.hidden && phaseRef.current === "running") interrupted.current = true;
    };
    document.addEventListener("visibilitychange", onHide);
    return () => document.removeEventListener("visibilitychange", onHide);
  }, []);

  useEffect(() => {
    if (phase !== "done" || saving || result) return;
    setSaving(true);
    api.submitTapping({
      taps: taps.current,
      duration_ms: DURATION_MS,
      hand: "dominant",
      interrupted: interrupted.current,
      device: navigator.userAgent.includes("iPhone") ? "iPhone"
            : navigator.userAgent.includes("Mac") ? "Mac" : "other",
      started_at: new Date(Date.now() - DURATION_MS).toISOString(),
    })
      .then((r) => { setResult(r); onDone?.(r); })
      .catch(() => setResult({ error: true }))
      .finally(() => setSaving(false));
  }, [phase, saving, result, onDone]);

  useEffect(() => () => stop(), [stop]);

  return (
    <div
      className="fixed inset-0 z-[60] flex select-none flex-col bg-[#0f172a] touch-none"
      onPointerDown={phase === "done" ? undefined : onTap}
      style={{ WebkitUserSelect: "none", WebkitTouchCallout: "none" }}
    >
      {phase === "intro" && (
        <div className="flex flex-1 flex-col justify-center px-8">
          <p className="text-xs font-semibold uppercase tracking-widest text-indigo-400">
            Tapping
          </p>
          <h2 className="mt-2 text-2xl font-bold text-white">15 seconds</h2>
          <p className="mt-4 text-sm leading-relaxed text-slate-300">
            Tap the screen as fast as you can, with the index finger of the same hand every
            time. The clock starts on your first tap.
          </p>
          <p className="mt-3 text-xs leading-relaxed text-slate-500">
            This is the control for the reaction test: if that slows and this doesn't, the
            change is in your attention rather than your hands. Use the same hand and the
            same posture each day or the comparison is noise.
          </p>
          <p className="mt-8 text-center text-base font-semibold text-indigo-400">
            Tap anywhere to begin
          </p>
          <button onClick={onClose} className="mt-8 py-2 text-sm text-slate-500">
            Cancel
          </button>
        </div>
      )}

      {phase === "running" && (
        <div className="relative flex flex-1 flex-col items-center justify-center">
          <span className="absolute top-6 right-6 text-xs tabular-nums text-slate-600">
            {remaining}s
          </span>
          <span className="text-6xl font-bold tabular-nums text-white">{count}</span>
          <span className="mt-2 text-xs uppercase tracking-widest text-slate-500">taps</span>
          <p className="absolute bottom-16 text-[11px] text-slate-600">keep going</p>
        </div>
      )}

      {phase === "done" && (
        <div className="flex flex-1 flex-col justify-center px-8">
          <p className="text-xs font-semibold uppercase tracking-widest text-emerald-400">Done</p>
          {saving && <p className="mt-4 text-sm text-slate-400">Saving…</p>}
          {result && !result.error && (
            <>
              <h2 className="mt-2 text-2xl font-bold text-white">
                {result.tap_rate} <span className="text-base font-normal text-slate-400">taps/s</span>
              </h2>
              {/* Variability first: it's the fatigue-sensitive measure. Raw rate is
                  comparatively robust and degrades late. */}
              <div className="mt-5 space-y-1.5 text-sm text-slate-300">
                <Row label="Variability (CV)" value={result.iti_cv != null ? `${result.iti_cv}%` : "—"} />
                <Row label="Mean interval" value={result.mean_iti != null ? `${result.mean_iti} ms` : "—"} />
                <Row label="Total taps" value={result.n_taps} />
              </div>
              <p className="mt-4 text-[11px] leading-relaxed text-slate-500">
                Variability is the number to watch — tapping speed holds up under fatigue
                long after timing gets ragged. A single session's within-block change is
                noise; read it as a trend.
                {result.artifacts > 0 && ` (${result.artifacts} interval${result.artifacts === 1 ? "" : "s"} discarded as double-registration.)`}
              </p>
              {result.interrupted && (
                <p className="mt-3 rounded-lg bg-amber-500/10 p-2 text-[11px] leading-relaxed text-amber-400">
                  The app was backgrounded mid-block — treat this session as unreliable.
                </p>
              )}
            </>
          )}
          {result?.error && <p className="mt-4 text-sm text-rose-400">Couldn't save that run.</p>}
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
