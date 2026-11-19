import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";
import PvtTest from "./PvtTest.jsx";
import KssTest from "./KssTest.jsx";
import TappingTest from "./TappingTest.jsx";
import BatteryDone from "./BatteryDone.jsx";
import { baselineStats, MIN_BASELINE_N, pvtRt, PVT_FLOOR_MS } from "../lib/battery.js";

// Today-tab runner for the whole daily battery.
//
// ORDER IS FIXED — PVT → KSS → tapping — and must NOT be randomised. Randomising is
// correct for between-subjects work and wrong for a longitudinal n-of-1: it injects
// variance. A fixed order turns "fatigue from the earlier tests" into a constant that
// cancels out of every day-to-day comparison. KSS sits between the two objective tests
// because it asks about the preceding ten minutes, and tapping would change the answer.
//
// Replaced the separate PvtCard + BatteryCard pair (2026-08-13). Two cards each holding
// a finished result took the top of Today all day for work already done, and the split
// left two holes: a partially-finished battery vanished at 12:00 with no way to finish
// it, and a battery never started vanished entirely — so the day it was missed looked
// identical to a day it was never offered.
const WINDOW_START_H = 8;
const WINDOW_END_H = 12;

const ORDER = ["pvt", "kss", "tapping"];

export default function BatteryCard({ onNavigate, onOpenChange }) {
  const [step, setStep] = useState(null);   // null | "pvt" | "kss" | "tapping"
  // A running test renders inline and needs the full column width, so the parent
  // grid is told whenever one is open.
  useEffect(() => { onOpenChange?.(step !== null); }, [step, onOpenChange]);
  // Tests finished in THIS sitting. Tracked locally because the "today" queries are
  // refetched asynchronously after each submission: reading completion from the cache
  // alone would let a not-yet-refetched PVT read as undone and send you back into it
  // after finishing KSS, since the order scan always starts at the first test.
  const [justDone, setJustDone] = useState(() => new Set());
  const qc = useQueryClient();

  const { data: pvt, isLoading: lp } = useQuery({
    queryKey: ["pvt-today"], queryFn: api.getPvtToday, staleTime: 1000 * 60 * 5 });
  const { data: kss, isLoading: lk } = useQuery({
    queryKey: ["kss-today"], queryFn: api.getKssToday, staleTime: 1000 * 60 * 5 });
  const { data: tap, isLoading: lt } = useQuery({
    queryKey: ["tapping-today"], queryFn: api.getTappingToday, staleTime: 1000 * 60 * 5 });
  const { data: pvtHist } = useQuery({
    queryKey: ["pvt-history", 30], queryFn: () => api.getPvtHistory(30),
    staleTime: 1000 * 60 * 15 });

  const refresh = () => {
    ["pvt-today", "kss-today", "tapping-today"].forEach((k) =>
      qc.invalidateQueries({ queryKey: [k] }));
    qc.invalidateQueries({ queryKey: ["pvt-history", 30] });
    qc.invalidateQueries({ queryKey: ["tapping-history", 30] });
  };

  const hour = new Date().getHours();
  const inWindow = hour >= WINDOW_START_H && hour < WINDOW_END_H;

  const stored = { pvt, kss, tapping: tap };
  const isDone = (t) => justDone.has(t) || !!stored[t];
  const remaining = ORDER.filter((t) => !isDone(t));
  const complete = remaining.length === 0;

  // Advance through whatever is still outstanding, preserving the fixed order.
  //
  // Finishing the LAST test lands on "done" rather than null. It used to land on null,
  // which unmounted the running test the instant it posted its result — so the tapping
  // test's own done screen was unreachable and the battery just vanished mid-tap. The
  // pause belongs only at the end: dwelling on a per-test result BETWEEN tests invites
  // reacting to one noisy number, and the fixed order exists to keep them comparable.
  const advance = (justFinished) => {
    refresh();
    const next = new Set(justDone).add(justFinished);
    setJustDone(next);
    setStep(ORDER.find((t) => !(next.has(t) || !!stored[t])) || "done");
  };

  // Don't render a state we're about to contradict — until the three "today" queries
  // resolve, a completed battery looks identical to one never started.
  if (lp || lk || lt) return null;

  // Baseline for the PVT delta: same protocol only, excluding runs whose timing was
  // flagged. v1 and v2 differ in trial count and so in precision — pooling them would
  // compare today against a silently heterogeneous mixture.
  const proto = pvt?.protocol ?? pvtHist?.current_protocol;
  const priors = (pvtHist?.sessions || [])
    .filter((s) => s.id !== pvt?.id && pvtRt(s) != null && s.protocol === proto
                   && !s.timing_suspect && !s.interrupted)
    .map(pvtRt);
  const stats = baselineStats(priors, PVT_FLOOR_MS);
  const delta = pvtRt(pvt) != null && stats.center != null
    ? Math.round(pvtRt(pvt) - stats.center) : null;
  const decided = delta != null && stats.band != null
    && stats.n >= MIN_BASELINE_N && Math.abs(delta) > stats.band;

  return (
    <>
      {/* The completion screen stands alone — without this guard the collapsed row
          would render above it, showing the same numbers twice. */}
      {step === "done" ? null : complete ? (
        // Collapsed. The work is done; the numbers live in Recovery.
        //
        // CRASH FIX 2026-08-15: this branch used to read `pvt.median_rt`,
        // `kss.score` and `tap.tap_rate` unguarded. `complete` is driven by the
        // LOCAL `justDone` set, which fills the moment the third test submits —
        // but `advance()` only *invalidates* the three "today" queries at that
        // instant, so they are still refetching and `pvt`/`kss`/`tap` are the
        // pre-session values, i.e. null on a fresh day. Finishing the battery
        // therefore threw a TypeError and took the tab down every time.
        // Everything here is now optional, with a placeholder for the refetch gap.
        <button
          onClick={() => onNavigate?.("recovery")}
          className="flex w-full items-center gap-2 rounded-2xl border border-rule bg-card px-3 py-2.5 text-left"
        >
          <span className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">
            BATTERY
          </span>
          <span style={{ color: "var(--rhr)" }}>✓</span>
          <span className="flex-1 truncate text-[11px] tabular-nums text-ink-2">
            {pvtRt(pvt) != null ? `${Math.round(pvtRt(pvt))} ms` : "saving…"}
            {decided && (
              <span style={{ color: delta < 0 ? "var(--rhr)" : "var(--breakfast)" }}>
                {" "}({delta > 0 ? "+" : ""}{delta})
              </span>
            )}
            {kss?.score != null && <>{" · KSS "}{kss.score}</>}
            {tap?.tap_rate != null && ` · ${tap.tap_rate}/s`}
          </span>
          <span className="text-ink-3">›</span>
        </button>
      ) : (
        // One row, same footprint as the character sheet's collapsed header (
        // 2026-10-21: the start card took prime space for a test run ~once a month).
        // Tap starts / continues the fixed-order battery. Outside the 08–12 window and
        // untouched it reads muted, so a missed day still differs from one never offered.
        <button
          onClick={() => setStep(remaining[0])}
          className={`flex w-full items-center gap-2 rounded-2xl border border-rule px-3 py-2.5 text-left ${
            inWindow || remaining.length < 3 ? "bg-card" : ""}`}
        >
          <span className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">BATTERY</span>
          <span className={`flex-1 truncate text-[11px] ${inWindow || remaining.length < 3 ? "text-ink-2" : "text-ink-3"}`}>
            {remaining.length < 3 ? `${3 - remaining.length}/3` : inWindow ? "start" : "not done"}
          </span>
          <span className="text-[10px] text-ink-3">▸</span>
        </button>
      )}

      {step === "pvt" && (
        <PvtTest onClose={() => setStep(null)} onDone={() => advance("pvt")} />
      )}
      {step === "kss" && (
        <KssTest onClose={() => setStep(null)} onDone={() => advance("kss")} />
      )}
      {step === "tapping" && (
        <TappingTest onClose={() => setStep(null)} onDone={() => advance("tapping")} />
      )}
      {step === "done" && <BatteryDone onClose={() => setStep(null)} />}
    </>
  );
}

