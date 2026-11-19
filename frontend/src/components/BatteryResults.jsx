import { useQuery } from "@tanstack/react-query";
import { api } from "../api.js";
import { baselineStats, MIN_BASELINE_N, pvtRt, PVT_FLOOR_MS } from "../lib/battery.js";

// Recovery-tab detail view for the daily battery.
//
// Lives here rather than in Body or Habits because all three tests are readiness
// readouts and belong beside the measures they'll be correlated against: PVT is the
// sleep-research standard instrument, KSS is a sleepiness scale, tapping is the fatigue
// control. Longitudinal views (sorted-RT curve, trial raster, circadian scatter) belong
// in Trends when they're built — this card is "today, in context".
//
// Every metric here is shown with the precision it actually has. Bootstrapped
// within-session SE on real sessions: median RT 3.4 ms (CV 1.1%), fastest10 3.6 ms
// (1.3%), mean 1/RT 1.9% — but lapses 2.2 on a value of 6, i.e. CV 37%. So lapses,
// performance score and the time-on-task slope are shown as context, never as a verdict.
export default function BatteryResults() {
  const { data: pvt } = useQuery({ queryKey: ["pvt-today"], queryFn: api.getPvtToday });
  const { data: kss } = useQuery({ queryKey: ["kss-today"], queryFn: api.getKssToday });
  const { data: tap } = useQuery({ queryKey: ["tapping-today"], queryFn: api.getTappingToday });
  const { data: pvtHist } = useQuery({
    queryKey: ["pvt-history", 30], queryFn: () => api.getPvtHistory(30) });
  const { data: tapHist } = useQuery({
    queryKey: ["tapping-history", 30], queryFn: () => api.getTappingHistory(30) });

  if (!pvt && !kss && !tap) return null;

  const proto = pvt?.protocol ?? pvtHist?.current_protocol;
  const pvtPriors = (pvtHist?.sessions || [])
    .filter((s) => s.id !== pvt?.id && pvtRt(s) != null && s.protocol === proto
                   && !s.timing_suspect && !s.interrupted)
    .map(pvtRt);
  const pvtStats = baselineStats(pvtPriors, PVT_FLOOR_MS);

  const tapPriors = (tapHist?.sessions || [])
    .filter((s) => s.id !== tap?.id && s.iti_cv != null && !s.interrupted)
    .map((s) => s.iti_cv);
  // Floor of 0.5 percentage points: iti_cv is itself a variability measure, so its own
  // day-to-day spread is what sets the scale — there's no separate measurement floor to
  // borrow the way there is for PVT median RT.
  const tapStats = baselineStats(tapPriors, 0.5);

  const flagged = pvt?.timing_suspect || pvt?.interrupted || tap?.interrupted;

  return (
    <div className="rounded-xl bg-slate-800 p-4">
      <div className="flex items-baseline justify-between">
        <h3 className="text-sm font-semibold text-white">Vigilance battery</h3>
        <span className="text-[10px] text-slate-600">
          {pvt?.local_time || kss?.local_time || tap?.local_time}
        </span>
      </div>

      {pvt && (
        <Metric
          label="Reaction time (from mean speed)"
          value={`${Math.round(pvtRt(pvt))} ms`}
          today={pvtRt(pvt)}
          stats={pvtStats}
          lowerIsBetter
          sub={`${pvt.n_trials} trials · median ${pvt.median_rt} ms · fastest 10% ${pvt.fastest10_mean} ms · speed ${pvt.mean_reciprocal_rt}/s`}
        />
      )}

      {kss && (
        <div className="mt-3 border-t border-slate-800 pt-3">
          <div className="flex items-baseline justify-between">
            <span className="text-xs text-slate-400">Sleepiness (KSS)</span>
            <span className="text-sm font-semibold tabular-nums text-white">{kss.score}/9</span>
          </div>
          <p className="mt-0.5 text-[11px] text-slate-500">{kss.label}</p>
          {/* The dissociation is the point. Feeling fine while measurably slow is a
              documented sleep-loss state and the dangerous one, so it's called out
              explicitly rather than left for the reader to spot across two rows. */}
          {pvt && pvtStats.center != null && kss.score <= 4
            && pvtRt(pvt) > pvtStats.center + (pvtStats.band ?? 0) && (
            <p className="mt-1.5 rounded-lg bg-amber-500/10 p-2 text-[11px] leading-relaxed text-amber-400">
              You rated yourself alert but reacted slower than usual. Feeling fine while
              measurably impaired is the state worth knowing about.
            </p>
          )}
        </div>
      )}

      {tap && (
        <div className="mt-3 border-t border-slate-800 pt-3">
          <Metric
            label="Tapping variability"
            value={tap.iti_cv != null ? `${tap.iti_cv}%` : "—"}
            today={tap.iti_cv}
            stats={tapStats}
            lowerIsBetter
            sub={`${tap.tap_rate} taps/s · ${tap.n_taps} taps · interval ${tap.mean_iti} ms`}
            bare
          />
          <p className="mt-1.5 text-[11px] leading-relaxed text-slate-500">
            The control: if reaction time slows and this doesn't, the change is
            attentional rather than motor.
          </p>
        </div>
      )}

      {pvt && (
        <p className="mt-3 border-t border-slate-800 pt-3 text-[11px] leading-relaxed text-slate-500">
          Context, not verdicts — each is too noisy to read from one session:
          {" "}{pvt.lapses_355} lapses{pvt.timeouts ? ` · ${pvt.timeouts} no-response` : ""}
          {pvt.perf_score != null && ` · score ${pvt.perf_score}%`}
          {pvt.slowest10_mean != null && ` · slowest 10% ${pvt.slowest10_mean} ms`}
        </p>
      )}

      {flagged && (
        <p className="mt-2 rounded-lg bg-amber-500/10 p-2 text-[11px] leading-relaxed text-amber-400">
          Timing was unreliable on part of this session — it's excluded from your baseline.
        </p>
      )}
    </div>
  );
}

function Metric({ label, value, today, stats, sub, lowerIsBetter, bare }) {
  const delta = today != null && stats.center != null ? today - stats.center : null;
  const decided = delta != null && stats.band != null
    && stats.n >= MIN_BASELINE_N && Math.abs(delta) > stats.band;
  const good = lowerIsBetter ? delta < 0 : delta > 0;

  return (
    <div className={bare ? "" : "mt-3"}>
      <div className="flex items-baseline justify-between">
        <span className="text-xs text-slate-400">{label}</span>
        <span className="text-sm font-semibold tabular-nums text-white">{value}</span>
      </div>
      {delta != null && (
        <p className="mt-0.5 text-[11px] tabular-nums">
          {decided ? (
            <span className={good ? "text-emerald-400" : "text-amber-400"}>
              {delta > 0 ? "+" : ""}{round(delta)} vs your usual
            </span>
          ) : (
            <span className="text-slate-500">
              {stats.n < MIN_BASELINE_N
                ? `baseline still forming — ${MIN_BASELINE_N - stats.n} more run${MIN_BASELINE_N - stats.n === 1 ? "" : "s"}`
                : `within your usual range (±${round(stats.band)})`}
            </span>
          )}
        </p>
      )}
      {sub && <p className="mt-0.5 text-[11px] text-slate-500">{sub}</p>}
    </div>
  );
}

const round = (v) => (Math.abs(v) >= 10 ? Math.round(v) : Math.round(v * 10) / 10);
