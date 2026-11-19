import { useQuery } from "@tanstack/react-query";
import { api } from "../api.js";
import { baselineStats, MIN_BASELINE_N, pvtRt, PVT_FLOOR_MS } from "../lib/battery.js";

/**
 * Battery completion screen — "Done!" plus all three tests against your own average.
 *
 * WHY THIS EXISTS (2026-08-31). Finishing the last test used to drop you straight back
 * to the collapsed battery row with no confirmation and no numbers. TappingTest does
 * have its own done screen, but it was never reachable: it calls `onDone` the moment
 * the result posts, BatteryCard's `advance()` then set `step` to null, and that
 * unmounted the component mid-render. You tapped the last tap and the card simply
 * vanished.
 *
 * Advancing BETWEEN tests should stay instant — dwelling on a per-test result mid-
 * battery invites reacting to one noisy number, and the fixed PVT → KSS → tapping order
 * exists precisely so the tests stay comparable. So the pause belongs at the END only,
 * and it shows all three together rather than whichever happened to be last.
 *
 * Values are read from the "today" queries, which `advance()` has just invalidated, so
 * the final test is briefly in a refetch gap — every field is optional with a "saving…"
 * placeholder. This is the same gap that once crashed the collapsed row (see BatteryCard).
 */
export default function BatteryDone({ onClose }) {
  const { data: pvt } = useQuery({ queryKey: ["pvt-today"], queryFn: api.getPvtToday });
  const { data: kss } = useQuery({ queryKey: ["kss-today"], queryFn: api.getKssToday });
  const { data: tap } = useQuery({ queryKey: ["tapping-today"], queryFn: api.getTappingToday });
  const { data: pvtHist } = useQuery({
    queryKey: ["pvt-history", 30], queryFn: () => api.getPvtHistory(30) });
  const { data: tapHist } = useQuery({
    queryKey: ["tapping-history", 30], queryFn: () => api.getTappingHistory(30) });
  const { data: kssHist } = useQuery({
    queryKey: ["kss-history", 30], queryFn: () => api.getKssHistory(30) });

  // Same protocol only, and never a flagged run — pooling protocols would compare
  // today against a silently heterogeneous mixture. Mirrors BatteryCard/BatteryResults.
  const proto = pvt?.protocol ?? pvtHist?.current_protocol;
  const pvtStats = baselineStats(
    (pvtHist?.sessions || [])
      .filter((s) => s.id !== pvt?.id && pvtRt(s) != null && s.protocol === proto
                     && !s.timing_suspect && !s.interrupted)
      .map(pvtRt), PVT_FLOOR_MS);

  // Floor 0.5pp: iti_cv is itself a variability measure, so its own day-to-day spread
  // sets the scale — there is no separate measurement floor to borrow.
  const tapStats = baselineStats(
    (tapHist?.sessions || [])
      .filter((s) => s.id !== tap?.id && s.iti_cv != null && !s.interrupted)
      .map((s) => s.iti_cv), 0.5);

  // Floor 0.5 of a KSS point: the scale is integer 1-9, so anything finer would call
  // rounding a change.
  const kssStats = baselineStats(
    (kssHist?.sessions || [])
      .filter((s) => s.id !== kss?.id && s.score != null)
      .map((s) => s.score), 0.5);

  return (
    <div className="rounded-2xl border border-rule bg-card p-5">
      <p className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">BATTERY</p>
      <h2 className="mt-1 text-2xl font-bold text-ink">Done!</h2>
      <p className="mt-1 text-[11px] leading-relaxed text-ink-3">
        All three logged. Compared against your own recent runs, not a population norm.
      </p>

      <div className="mt-4 space-y-3">
        <Row
          label="Reaction time"
          value={pvtRt(pvt) != null ? `${Math.round(pvtRt(pvt))} ms` : null}
          today={pvtRt(pvt)}
          stats={pvtStats}
          unit="ms"
        />
        <Row
          label="Sleepiness (KSS)"
          value={kss?.score != null ? `${kss.score}/9` : null}
          today={kss?.score}
          stats={kssStats}
          unit=""
        />
        <Row
          label="Tapping variability"
          value={tap?.iti_cv != null ? `${tap.iti_cv}%` : null}
          today={tap?.iti_cv}
          stats={tapStats}
          unit="pp"
        />
      </div>

      {(pvt?.timing_suspect || pvt?.interrupted || tap?.interrupted) && (
        <p className="mt-3 rounded-lg p-2 text-[11px] leading-relaxed"
           style={{ background: "color-mix(in srgb, var(--over) 12%, transparent)",
                    color: "var(--over)" }}>
          Timing was unreliable on part of this session — it's excluded from your baseline.
        </p>
      )}

      <button
        onClick={onClose}
        className="mt-5 w-full rounded-lg bg-surface py-2.5 text-[13px] font-semibold text-ink ring-1 ring-inset ring-ink-3 active:bg-card"
      >
        Close
      </button>
    </div>
  );
}

/**
 * One test, with its distance from your usual.
 *
 * Lower is better for all three — reaction time, sleepiness and tapping variability
 * alike — so there is no direction flag to get wrong here.
 *
 * A delta is only given a VERDICT (coloured, "vs usual") once the baseline has enough
 * runs to estimate its own spread; below that it says so plainly instead of dressing a
 * guess as a finding. Same rule as the Recovery card, via the shared baselineStats.
 */
function Row({ label, value, today, stats, unit }) {
  const delta = today != null && stats.center != null ? today - stats.center : null;
  const decided = delta != null && stats.band != null
    && stats.n >= MIN_BASELINE_N && Math.abs(delta) > stats.band;
  const need = MIN_BASELINE_N - stats.n;

  return (
    <div className="border-t border-rule pt-2 first:border-t-0 first:pt-0">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-[11px] text-ink-2">{label}</span>
        <span className="text-[13px] font-semibold tabular-nums text-ink">
          {value ?? <span className="font-normal text-ink-3">saving…</span>}
        </span>
      </div>
      {delta != null && (
        <p className="mt-0.5 text-right text-[10px] tabular-nums">
          {decided ? (
            <span style={{ color: delta < 0 ? "var(--rhr)" : "var(--over)" }}>
              {delta > 0 ? "+" : ""}{round(delta)}{unit} vs usual
            </span>
          ) : (
            <span className="text-ink-3">
              {stats.n < MIN_BASELINE_N
                ? `baseline forming — ${need} more run${need === 1 ? "" : "s"}`
                : `within your usual range (±${round(stats.band)}${unit})`}
            </span>
          )}
        </p>
      )}
    </div>
  );
}

const round = (v) => (Math.abs(v) >= 10 ? Math.round(v) : Math.round(v * 10) / 10);
