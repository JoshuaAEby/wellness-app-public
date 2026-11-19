import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ComposedChart, Area, Bar, Line, Scatter, XAxis, YAxis,
  CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine, ReferenceArea,
} from "recharts";
import { EventFilterPills, computeHighlight, HighlightContrast } from "../components/EventHighlight.jsx";
import { api } from "../api.js";
import { windowDates, fmtDate, linearRegression, kgToLbs } from "../utils.js";
import { glowDot, lastValidIndex, trendGlowColor, windowToTrendDays, interventionMarkers, ChartEventKey } from "../components/chartHelpers.jsx";
import TimeWindowSelector from "../components/TimeWindowSelector.jsx";
import AudioExposure from "../components/AudioExposure.jsx";
import FoodComposition from "../components/FoodComposition.jsx";
import FoodCoverage from "../components/FoodCoverage.jsx";
import BloodPanelModal from "../components/BloodPanelModal.jsx";
import MyFoodsModal from "../components/MyFoodsModal.jsx";
import { Card, Eyebrow, Hero, HeroSub, K, Sep, Foot, Legend, Histogram, TrackBar, SegmentedTabs, EmptyState } from "../components/ui.jsx";
import { useFlagRuns, flagBands, FlagBandLegend } from "../components/FlagBands.jsx";

// Predicted vs actual weight under the calibrated energy model.
//
// This is the model's own error bar, made visible. A single k value can't tell you
// it has gone stale; only the two curves separating can. Divergence means either the
// calibration has drifted (metabolic adaptation during a cut) or logging behaviour
// changed — and you want to see that rather than keep trusting the number.
function EnergyCalibrationCard({ window = "3M" }) {
  const [open, setOpen] = useState(false);
  const days = { "1W": 7, "1M": 30, "3M": 90, "6M": 180, "1Y": 365 }[window] ?? 90;
  const calQ = useQuery({
    queryKey: ["energyCalibration", days],
    queryFn: () => api.getEnergyCalibration(days),
  });
  const { data } = calQ;
  // Keep the whole window so the actual-weight line spans what you selected; the
  // predicted line simply starts where logged intake starts (nulls leave a gap, which
  // is the honest rendering of "the model had no input here").
  const series = data?.available ? (data.series ?? []) : [];
  // Illness and travel move body mass through fluid and appetite, not energy balance —
  // so a divergence that sits inside a flagged run is not evidence the model drifted.
  //
  // ABOVE the early returns deliberately: this card bails out three different ways
  // (no data, <7 predicted days), and a hook below any of them would change hook count
  // between renders. `useFlagRuns` no-ops on an empty `rows`.
  const flagRuns = useFlagRuns({ rows: series, isoKey: "date" });
  const title = (
    <p className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">
      Energy model — predicted vs actual
    </p>
  );
  // These three exits used to be `return null`, so a failed fit, a missing profile
  // and a short window all looked like the card was never built (note 53).
  const nPredicted = series.filter((d) => d.predicted_lbs != null).length;
  if (!data?.available || nPredicted < 7) {
    return (
      <div className="rounded-2xl border border-rule bg-card p-4">
        {title}
        <EmptyState query={calQ} message={
          data && !data.available
            ? `Not available for the last ${days} days — ${data.reason ?? "no reason given"}.`
            : data
              ? `Only ${nPredicted} day${nPredicted === 1 ? "" : "s"} can be predicted in the last ${days} days — the chart needs 7. Each prediction needs 28 days of logged intake and scale readings behind it.`
              : null} />
      </div>
    );
  }
  // Each predicted point is a fixed look-back (notes 47/48): the smoothed weight
  // `horizon` days earlier plus the balance logged since. So a point's gap is one
  // horizon's worth of error, not a window's — it no longer grows with the window.
  const horizon = data.horizon_days ?? 28;
  const gap = data.final_gap_lbs;
  // Positive gap = actual above predicted = the model over-estimated the deficit.
  const gapKcalPerDay = gap != null ? Math.round((gap * 3500) / horizon) : null;
  // Water noise sets the scale a gap has to beat; the window's RMS gap measures it.
  const noise = Math.max(data.rms_gap_lbs ?? 0, 1.5);
  const hist = data.history ?? [];
  const latestFit = hist[0];

  return (
    <div className="rounded-2xl border border-rule bg-card p-4">
      <div className="flex items-baseline justify-between">
        {title}
        <button onClick={() => setOpen((v) => !v)} className="text-xs text-ink-2">
          {open ? "hide" : "detail"}
        </button>
      </div>

      <div className="mt-3 h-44">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={series} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
            <CartesianGrid stroke="var(--rule)" strokeDasharray="3 3" vertical={false} />
            {flagBands(flagRuns, series, { xKey: "date", isoKey: "date" })}
            <XAxis dataKey="date" tick={{ fill: "var(--ink-3)", fontSize: 10 }}
              tickFormatter={fmtDate} minTickGap={40} />
            <YAxis tick={{ fill: "var(--ink-3)", fontSize: 10 }} domain={["auto", "auto"]}
              tickFormatter={(v) => v.toFixed(0)} width={34} />
            <Tooltip
              contentStyle={{ background: "var(--canvas)", border: "1px solid var(--rule)", borderRadius: 8 }}
              labelStyle={{ color: "var(--ink-3)", fontSize: 11 }}
              formatter={(v, n) => [v != null ? `${v.toFixed(1)} lb` : "—",
                n === "predicted_lbs" ? "Predicted" : "Actual"]}
              labelFormatter={fmtDate}
            />
            <Line type="monotone" dataKey="predicted_lbs" stroke="var(--dinner)" strokeWidth={2}
              dot={false} strokeDasharray="4 3" isAnimationActive={false} />
            {/* A dot per weigh-in (2026-10-21): connectNulls bridges the days
                without one, so without dots a 13-day gap looked like 13 readings.
                actual_lbs is null on days with no reading, so dots mark exactly those. */}
            <Line type="monotone" dataKey="actual_lbs" stroke="var(--rhr)" strokeWidth={1.5}
              dot={{ r: 2.5, fill: "var(--rhr)", stroke: "var(--card)", strokeWidth: 1 }} activeDot={{ r: 4 }}
              connectNulls isAnimationActive={false} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>

      <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-2">
        <span><span className="text-ink-2">- -</span> predicted</span>
        <span><span className="text-[color:var(--rhr)]">—</span> actual</span>
        <FlagBandLegend runs={flagRuns} />
        {gap != null && (
          <span className={Math.abs(gap) <= noise ? "text-ink-2" : "text-[color:var(--breakfast)]"}>
            last {horizon} d: gap {gap > 0 ? "+" : ""}{gap} lb
            {gapKcalPerDay != null && ` (~${Math.abs(gapKcalPerDay)} kcal/day)`}
          </span>
        )}
        {data.mean_gap_lbs != null && data.n_compared >= 7 && (
          <span className="text-ink-3">
            window mean {data.mean_gap_lbs > 0 ? "+" : ""}{data.mean_gap_lbs} lb · noise ±{data.rms_gap_lbs}
          </span>
        )}
        {data.k === 1 && (
          <span className="text-[color:var(--breakfast)]">uncalibrated (k = 1) — awaiting first fit</span>
        )}
      </div>

      {open && (
        <div className="mt-3 space-y-2 border-t border-rule pt-3 text-xs text-ink-2">
          <p>
            <span className="text-ink">
              k = {data.k}
              {latestFit?.k_ci_low != null &&
                ` (95% CI ${latestFit.k_ci_low}–${latestFit.k_ci_high})`}
            </span>{" "}
            — personal scaling on Apple&apos;s active calories, fit against your weight
            trajectory. RMR {Math.round(data.rmr)}{" "}
            ({data.rmr_basis === "katch_mcardle" ? "Katch-McArdle" : "Mifflin-St Jeor"}).
          </p>
          <p>
            Each dashed point is your smoothed weight <K>{horizon} days earlier</K> plus the
            energy balance logged since. It asks one question — did the last {horizon} days
            of logging explain the last {horizon} days of weight change? — so a given day
            gets the same prediction whatever window you view, and a steady bias in the
            inputs can build up for {horizon} days at most instead of for the whole window.
          </p>
          <p>
            The gap&apos;s day-to-day scatter (±{data.rms_gap_lbs ?? "—"} lb here) is mostly
            water. Read the <em>mean</em> gap over a window as bias; a single day&apos;s gap
            only means something once it clears that scatter.
          </p>
          {data.n_partial_days > 0 && (
            <p>
              {data.n_partial_days} partly-logged day{data.n_partial_days === 1 ? "" : "s"} in
              this window {data.n_partial_days === 1 ? "is" : "are"} treated as unknown and
              filled with the average of the complete days around {data.n_partial_days === 1 ? "it" : "them"} —
              counted as logged, a missing dinner reads as a ~700 kcal deficit. Sick and
              travel days are kept: their logs are complete.
            </p>
          )}
          <p>
            k is fit on a rolling 90 days regardless of the view.
          </p>
          <p>
            k is not a measurement of Apple&apos;s error. Because it is fit against
            <em> logged</em> intake, any systematic under-logging is folded into the same
            number — the two are not separable from this data. Fine for prediction, not a
            physiological quantity.
          </p>
          {hist.length > 1 && (
            <div>
              <p className="mb-1 text-ink-2">Fit history</p>
              {hist.slice(0, 6).map((h) => (
                <div key={h.fitted_at} className="flex justify-between tabular-nums">
                  <span>{fmtDate(h.fitted_at.slice(0, 10))}</span>
                  <span>k {h.k}{h.k_raw != null && h.k_raw !== h.k && ` (raw ${h.k_raw})`}</span>
                  <span>TDEE {h.adaptive_tdee != null ? Math.round(h.adaptive_tdee) : "—"}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// Your target for one marker, editable, shown alongside (never merged into) the
// lab's reference range. Seeded defaults come from preventive-cardiology and
// metabolic-health literature — opinions, not clinical thresholds — so the source
// is always labelled and always overridable.
function TargetEditor({ marker, latest, fmtTarget }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [lo, setLo] = useState(latest.target_low ?? "");
  const [hi, setHi] = useState(latest.target_high ?? "");

  const done = () => {
    qc.invalidateQueries({ queryKey: ["blood-markers"] });
    setEditing(false);
  };
  const save = useMutation({
    mutationFn: () => api.setBloodTarget(marker,
      lo === "" ? null : parseFloat(lo), hi === "" ? null : parseFloat(hi)),
    onSuccess: done,
  });
  const reset = useMutation({
    mutationFn: () => api.resetBloodTarget(marker),
    onSuccess: done,
  });

  const shown = fmtTarget(latest.target_low, latest.target_high);
  const off = latest.vs_target === "above" || latest.vs_target === "below";

  if (editing) {
    return (
      <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[11px]">
        <span className="text-ink-3">target</span>
        <input value={lo} onChange={e => setLo(e.target.value)} placeholder="min" inputMode="decimal"
          className="w-14 rounded bg-canvas border border-rule px-1.5 py-0.5 text-ink outline-none" />
        <span className="text-ink-3">–</span>
        <input value={hi} onChange={e => setHi(e.target.value)} placeholder="max" inputMode="decimal"
          className="w-14 rounded bg-canvas border border-rule px-1.5 py-0.5 text-ink outline-none" />
        <button onClick={() => save.mutate()} disabled={save.isPending}
          className="rounded bg-surface px-2 py-0.5 font-medium text-ink ring-1 ring-inset ring-rule disabled:opacity-40">save</button>
        <button onClick={() => reset.mutate()} disabled={reset.isPending}
          className="text-ink-3 hover:text-ink-2">use default</button>
        <button onClick={() => setEditing(false)} className="text-ink-3 hover:text-ink-2">cancel</button>
      </div>
    );
  }

  return (
    <p className="mt-1.5 text-[11px] text-ink-3">
      {shown ? (
        <>
          <span className={off ? "text-[color:var(--hrv)]" : "text-ink-2"}>target {shown}</span>
          <span className="text-ink-3">
            {" "}({latest.target_source === "custom" ? "yours" : "suggested"})
          </span>
          {latest.target_rationale && (
            <span className="block text-ink-3">{latest.target_rationale}</span>
          )}
        </>
      ) : (
        <span className="text-ink-3">no target set</span>
      )}
      <button onClick={() => setEditing(true)}
        className="ml-1.5 text-ink-2 hover:text-ink-2">edit</button>
    </p>
  );
}

// Blood markers: slow ground-truth outcomes, 1–2 panels/year. Rows show the
// latest value, ref-range status, and delta vs the previous draw; tapping a row
// expands its full draw history.
function BloodMarkersCard() {
  const [showAdd, setShowAdd] = useState(false);
  const [expanded, setExpanded] = useState(null);
  const [year, setYear] = useState("all");
  const [showStable, setShowStable] = useState(false);

  // Defined before use: `entries` below calls this, and a `const` arrow is in the
  // temporal dead zone until its declaration is evaluated.
  const changeInfo = (latest, prev, delta) => {
    if (!prev || delta == null || latest.rcv_pct == null || !prev.value) {
      return { significant: null, trend: null,
               title: prev ? `change since ${prev.date}` : undefined };
    }
    const pct = ((latest.value - prev.value) / Math.abs(prev.value)) * 100;
    const significant = Math.abs(pct) > latest.rcv_pct;
    let trend = null;
    if (significant && latest.direction === "higher_better") trend = pct > 0 ? "better" : "worse";
    if (significant && latest.direction === "lower_better") trend = pct > 0 ? "worse" : "better";
    return {
      significant, trend,
      title: `${pct >= 0 ? "+" : ""}${pct.toFixed(1)}% since ${prev.date} · `
           + `needs >${latest.rcv_pct}% to exceed normal biological + assay variation`
           + (significant ? " — real change" : " — within normal variation"),
    };
  };
  const { data: markers = [] } = useQuery({
    queryKey: ["blood-markers"],
    queryFn: api.getBloodMarkers,
    staleTime: 1000 * 60 * 60,
  });

  // Year filter: restrict which draws are considered, so "latest vs previous"
  // compares within the selected year rather than silently across it.
  const years = [...new Set(markers.map(m => m.date.slice(0, 4)))].sort().reverse();
  const visible = year === "all" ? markers : markers.filter(m => m.date.startsWith(year));

  // Group flat rows by marker, newest first (API returns date-desc)
  const byMarker = new Map();
  for (const m of visible) {
    if (!byMarker.has(m.marker)) byMarker.set(m.marker, []);
    byMarker.get(m.marker).push(m);
  }

  const GROUP_ORDER = ["Lipids", "Metabolic", "Liver", "Kidney & Electrolytes",
    "Blood count", "Thyroid & Hormones", "Inflammation & Nutrients", "Other"];
  const CORE_ORDER = ["apob", "ldl_c", "hdl_c", "triglycerides", "total_cholesterol",
    "hba1c", "fasting_glucose", "fasting_insulin", "hs_crp", "ferritin", "vitamin_d",
    "testosterone_total", "tsh"];

  // Triage. `concern` and `near_bound` come from the server (services/blood_markers)
  // so the card and the weekly/monthly model context can't disagree about what counts
  // as out of range; only the tier *combination* is a UI concern.
  //   attention — out of range the way that matters (the lab's interval, direction-aware)
  //   watch     — a change that clears RCV, in range but hugging the bound that
  //               matters, or off your target. A real change while IN range used to
  //               be "attention" (2026-10-28: two of one panel's five flags were in-range
  //               moves, one of them back at an earlier value) — worth
  //               a look, not the same claim as out of range.
  //   stable    — everything else, collapsed by default
  const entries = [...byMarker.entries()].map(([key, hist]) => {
    const latest = hist[0];
    const prev = hist.find(h => h.date !== latest.date);
    const chg = changeInfo(latest, prev, prev ? latest.value - prev.value : null);
    // Off-target is a WATCH signal, never an attention one: a target is an editable
    // opinion, and conflating "outside your goal" with "outside the lab's interval"
    // would launder the former into the authority of the latter.
    const offTarget = latest.vs_target === "above" || latest.vs_target === "below";
    const tier = latest.concern ? "attention"
               : (chg.significant || latest.near_bound || offTarget) ? "watch" : "stable";
    return { key, hist, latest, prev, chg, tier, offTarget };
  });

  const rank = (e) => (e.tier === "attention" ? 0 : e.tier === "watch" ? 1 : 2);
  const sortRows = (a, b) => {
    if (rank(a) !== rank(b)) return rank(a) - rank(b);
    const ai = CORE_ORDER.indexOf(a.key), bi = CORE_ORDER.indexOf(b.key);
    if (ai !== -1 || bi !== -1) return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
    return a.key.localeCompare(b.key);
  };

  const flagged = entries.filter(e => e.tier !== "stable").sort(sortRows);
  const stable = entries.filter(e => e.tier === "stable").sort(sortRows);
  const nAttention = entries.filter(e => e.tier === "attention").length;

  // Grouped view of whatever is being shown, in panel order.
  const grouped = (list) => GROUP_ORDER
    .map(g => [g, list.filter(e => (e.latest.group || "Other") === g)])
    .filter(([, xs]) => xs.length > 0);

  const refStatus = (m) => {
    // Outside the range in the direction that matters → amber/red + a label.
    // Colour and wording scale with HOW FAR outside, not merely whether. A 12%
    // overage previously rendered exactly like a value 3x the limit.
    if (m.concern) {
      const word = m.position === "below" ? "low" : "high";
      const shade = { mild: "var(--breakfast)", moderate: "var(--breakfast)", marked: "var(--over)" };
      const qualifier = m.severity === "mild" ? "slightly " : m.severity === "marked" ? "well " : "";
      return {
        color: shade[m.severity] ?? "var(--over)",
        symbol: m.severity === "mild" ? "▲" : "▲",
        label: `${qualifier}${word}`,
        detail: m.excess_pct != null
          ? `${m.excess_pct}% ${m.position === "below" ? "below" : "above"} the lab's limit`
          : null,
      };
    }
    // Outside the range the GOOD way (HDL above 59, eGFR above 90). Say so rather
    // than rendering it identically to a mid-range value — otherwise it looks like
    // the app didn't notice.
    // A % differential whose absolute count is in the same draw: the composition
    // shifts with every other cell type, so the absolute row carries the judgement
    // (services/blood_markers "White-cell differential"). Say where it sits, quietly.
    if (m.read_as) {
      const abs = m.position === "in" ? null : m.position === "above" ? "↑" : "↓";
      return { color: "var(--ink-3)", symbol: "●",
               label: abs ? `${abs} % only` : null,
               detail: abs ? "A share of the white count, not a count — judge it by the matching (abs) row." : null };
    }
    if (m.favorable) {
      return { color: "var(--rhr)", symbol: "●",
               label: m.position === "above" ? "above range · good" : "below range · good" };
    }
    if (m.ref_low == null && m.ref_high == null) return { color: "var(--ink-3)", label: null, symbol: "●" };
    return { color: "var(--rhr)", label: null, symbol: "●" };
  };

  const fmtTarget = (lo, hi) =>
    lo != null && hi != null ? `${lo}–${hi}` : hi != null ? `< ${hi}` : lo != null ? `> ${lo}` : null;

  // One marker row. Shared by the flagged and stable sections so they can't drift.
  const renderRow = ({ key, hist, latest, prev, chg, offTarget }) => {
    const rs = refStatus(latest);
    const delta = prev ? latest.value - prev.value : null;
    const open = expanded === key;
    return (
      <div key={key}>
        <button onClick={() => setExpanded(open ? null : key)}
          className="w-full flex items-center justify-between rounded-lg bg-canvas/50 px-3 py-2 hover:bg-canvas/80">
          <span className="text-xs text-ink-2 flex items-center gap-2 min-w-0">
            <span style={{ color: rs.color }}>{rs.symbol ?? "●"}</span>
            <span className="truncate">{latest.label || key}</span>
            {/* Not on the lab report — WBC × % from the same draw, with the app's
                lenient adult interval (services/blood_markers ABS_DIFFERENTIAL). */}
            {latest.derived && <span className="shrink-0 text-[10px] text-ink-3">calc.</span>}
            {rs.label && <span className="shrink-0 text-[10px]" style={{ color: rs.color }}>{rs.label}</span>}
            {/* Off your target but inside the lab's range — deliberately quieter
                than the range label, and a different colour, so the two signals
                stay visually distinct. */}
            {offTarget && !rs.label && (
              <span className="shrink-0 text-[10px] text-[color:var(--hrv)]">
                off target
              </span>
            )}
          </span>
          <span className="shrink-0 text-xs tabular-nums">
            <span className="text-ink font-semibold">{latest.value}</span>
            <span className="text-ink-3"> {latest.unit || ""}</span>
            {delta != null && Math.abs(delta) > 1e-9 && (
              <span className={`ml-2 ${chg.significant ? "font-semibold" : "text-ink-3"}`}
                style={chg.significant ? { color: chg.trend === "better" ? "var(--rhr)"
                                                : chg.trend === "worse" ? "var(--breakfast)"
                                                : "var(--ink-2)" } : undefined}
                title={chg.title}>
                {delta > 0 ? "+" : ""}{+delta.toFixed(2)}
                {chg.significant === false && <span className="ml-0.5 text-ink-3">~</span>}
              </span>
            )}
          </span>
        </button>
        {open && (
          <div className="ml-6 mt-1 mb-2 space-y-0.5">
            {latest.derived && (
              <p className="text-[11px] text-ink-3">
                Calculated as WBC × {key.replace("_abs", "")} % from the same draw. The
                lab printed no range for it; the range shown is a lenient adult interval
                the app supplies.
              </p>
            )}
            {hist.map(h => (
              <p key={h.id ?? `${h.date}-${h.marker}`} className="text-[11px] text-ink-3 tabular-nums">
                {h.date} · <span className="text-ink-2">{h.value} {h.unit || ""}</span>
                {h.ref_low != null || h.ref_high != null
                  ? <span className="text-ink-3"> (ref {h.ref_low ?? "–"}–{h.ref_high ?? "–"})</span>
                  : null}
              </p>
            ))}
            {rs.detail && (
              <p className="mt-1 text-[11px]" style={{ color: rs.color }}>
                {rs.detail}
                {latest.severity === "mild" && (
                  <span className="text-ink-3">
                    {" "}— on a panel this size, roughly one marker falls outside its
                    range by chance alone
                  </span>
                )}
              </p>
            )}
            <TargetEditor marker={key} latest={latest} fmtTarget={fmtTarget} />
          </div>
        )}
      </div>
    );
  };

  // ── Normalised panel (from the markers-reference-panel mockup) ──────────
  // Sixteen-plus analytes in incomparable units become comparable by normalising
  // each to ITS OWN reference interval: 0 = lower limit, 1 = upper. "Where in range"
  // is then a shared coordinate, so one out-of-range dot in a field of grey answers
  // "is anything wrong" instantly — which a flag-when-abnormal list cannot, because
  // it can't show an in-range value drifting toward an edge.
  //
  // Colour is driven by the backend's `position` + `direction`, NOT by a symmetric
  // value>ref_high test: high HDL and high eGFR are GOOD outcomes, and a naive test
  // paints them as alarms (services/blood_markers opens by naming this exact trap).
  const PanelRow = ({ m, prev }) => {
    if (m.ref_low == null || m.ref_high == null || m.ref_high <= m.ref_low) return null;
    const norm = (v) => (v - m.ref_low) / (m.ref_high - m.ref_low);
    const t = norm(m.value);
    // Prior draw on the SAME normalised axis, so movement is a distance you can see
    // rather than a percentage you have to hold in your head. Only drawn when the
    // prior used the same reference interval — a lab changing its range would make
    // the two marks incomparable and the movement a fiction.
    const sameRef = prev && prev.ref_low === m.ref_low && prev.ref_high === m.ref_high;
    const tPrev = sameRef && prev.value != null ? norm(prev.value) : null;
    const AX0 = -0.38, AX1 = 1.38;
    const pct = (v) => ((Math.max(AX0, Math.min(AX1, v)) - AX0) / (AX1 - AX0)) * 100;
    // `position` is "in" | "above" | "below" (services/blood_markers.assess).
    const out = m.position && m.position !== "in";
    // `favorable` means out-of-range in the GOOD direction — high HDL, high eGFR.
    // Painting those red is the exact trap the service warns about, so they take
    // the neutral-good token rather than the alarm one.
    const color = m.read_as ? "var(--ink-3)"
                : !out ? (m.near_bound ? "var(--breakfast)" : "var(--ink-2)")
                : m.favorable ? "var(--rhr)"
                : m.position === "above" ? "var(--over)" : "var(--under)";
    return (
      <div className="flex items-center gap-2">
        <span className="w-[76px] flex-none truncate text-right text-[9.5px]"
          style={{ color: out && !m.read_as ? "var(--ink)" : "var(--ink-3)" }} title={m.label}>
          {m.label}
        </span>
        <div className="relative h-[13px] flex-1">
          {/* the in-range span: uncoloured, like every other "fine" field here */}
          <div className="absolute inset-y-[2px] rounded-[3px] bg-surface"
            style={{ left: `${pct(0)}%`, width: `${pct(1) - pct(0)}%` }} />
          <div className="absolute inset-y-0 w-px bg-rule" style={{ left: `${pct(0.5)}%` }} />

          {/* Movement: a faint connector from the prior mark to the current one.
              Suppressed under ~2% of the axis — below that the "arrow" is shorter
              than the dots and reads as a smudge rather than a direction. */}
          {tPrev != null && Math.abs(pct(t) - pct(tPrev)) > 2 && (
            <div className="absolute top-1/2 h-px -translate-y-1/2"
              style={{
                left: `${Math.min(pct(t), pct(tPrev))}%`,
                width: `${Math.abs(pct(t) - pct(tPrev))}%`,
                background: `linear-gradient(to ${pct(t) > pct(tPrev) ? "right" : "left"}, transparent, ${color})`,
                opacity: 0.7,
              }} />
          )}
          {tPrev != null && (
            <div className="absolute top-1/2 h-[5px] w-[5px] -translate-y-1/2 rounded-full ring-1"
              style={{ left: `calc(${pct(tPrev)}% - 2.5px)`,
                       background: "transparent", borderColor: "transparent",
                       boxShadow: `inset 0 0 0 1px var(--ink-3)` }}
              title={`${prev.date}: ${prev.value} ${prev.unit ?? ""}`} />
          )}

          <div className="absolute top-1/2 h-[7px] w-[7px] -translate-y-1/2 rounded-full"
            style={{ left: `calc(${pct(t)}% - 3.5px)`, background: color }}
            title={`${m.label}: ${m.value} ${m.unit ?? ""} (range ${m.ref_low}–${m.ref_high})`
                   + (prev ? ` · was ${prev.value} on ${prev.date}` : "")} />
        </div>
        <span className="w-[46px] flex-none text-[9.5px] tabular-nums"
          style={{ color: out ? color : "var(--ink-3)" }}>
          {m.value}
        </span>
      </div>
    );
  };

  return (
    <div className="rounded-2xl border border-rule bg-card p-4">
      <div className="mb-1 flex items-center justify-between">
        <p className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">BLOOD MARKERS</p>
        <button onClick={() => setShowAdd(true)}
          className="rounded-md bg-surface px-2.5 py-1 text-[10px] font-semibold tracking-micro text-ink ring-1 ring-inset ring-rule hover:ring-ink-3">
          + Add panel
        </button>
      </div>
      <p className="mb-3 text-xs text-ink-3">
        lab panels — upload a PDF, paste a report, or enter manually · ● fine · ▲ needs attention
        · deltas marked <span className="text-ink-3">~</span> are within normal biological variation
      </p>

      {entries.length === 0 && (
        <p className="text-xs text-ink-3">
          {markers.length === 0
            ? "No panels yet — upload your latest lab PDF to backfill history."
            : "No markers in that year."}
        </p>
      )}

      {years.length > 1 && (
        <div className="mb-3 flex gap-1">
          {["all", ...years].map(y => (
            <button key={y} onClick={() => { setYear(y); setExpanded(null); }}
              className={`rounded px-2 py-0.5 text-[11px] font-medium ${
                year === y ? "bg-surface text-ink ring-1 ring-inset ring-ink-3" : "bg-surface text-ink-2 hover:bg-surface"}`}>
              {y === "all" ? "All" : y}
            </button>
          ))}
        </div>
      )}

      {entries.length > 0 && (
        <p className="mb-2 text-[11px] text-ink-2">
          {nAttention > 0
            ? <><span className="font-semibold text-[color:var(--over)]">{nAttention} need{nAttention === 1 ? "s" : ""} attention</span>
                {flagged.length > nAttention && ` · ${flagged.length - nAttention} worth a look`}</>
            : flagged.length > 0
              ? <>{flagged.length} worth a look</>
              : <span className="text-[color:var(--rhr)]/90">Everything in range and stable</span>}
          {stable.length > 0 && <span className="text-ink-3"> · {stable.length} stable</span>}
        </p>
      )}

      {/* Overview first: every marker on one normalised axis. The detailed rows
          below stay as they were — this answers "is anything off", they answer
          "by how much, and is it a real change". */}
      {entries.length > 2 && (
        <div className="mb-4 space-y-[3px] border-b border-rule pb-4">
          <div className="mb-1.5 flex items-center justify-between text-[8px] tracking-micro text-ink-3">
            <span>LOW</span><span>REFERENCE RANGE</span><span>HIGH</span>
          </div>
          {entries.map((e) => <PanelRow key={e.latest.marker} m={e.latest} prev={e.prev} />)}
          <p className="prose-ui pt-2 text-[9.5px] leading-snug text-ink-3">
            Each row is normalised to that marker's own reference interval, so position
            is comparable across rows even though the units are not. The hollow mark is
            your previous draw and the trail shows which way it moved. Amber = in range
            but near the edge that matters.
          </p>
        </div>
      )}

      {grouped(flagged).map(([g, list]) => (
        <div key={g} className="mb-2">
          <p className="mb-1 text-[10px] font-medium uppercase tracking-widest text-ink-3">{g}</p>
          <div className="space-y-1">{list.map(renderRow)}</div>
        </div>
      ))}

      {stable.length > 0 && (
        <button onClick={() => setShowStable(v => !v)}
          className="mt-1 text-[11px] font-medium text-ink-2 hover:text-ink-2">
          {showStable ? "Hide" : "Show"} {stable.length} stable marker{stable.length === 1 ? "" : "s"} {showStable ? "▾" : "▸"}
        </button>
      )}
      {showStable && grouped(stable).map(([g, list]) => (
        <div key={g} className="mt-2">
          <p className="mb-1 text-[10px] font-medium uppercase tracking-widest text-ink-3">{g}</p>
          <div className="space-y-1">{list.map(renderRow)}</div>
        </div>
      ))}

      {showAdd && <BloodPanelModal onClose={() => setShowAdd(false)} />}
    </div>
  );
}

const PROTEIN_MULT = { building: 0.75, maintaining: 0.65, cutting: 0.85, peaking: 0.80 };

// The meal palette is defined once as tokens in index.css — Today's nutrition card
// reads the same five, so a breakfast bar is the same colour on both tabs. Keeping a
// private copy here is exactly how the two drifted apart before.
const MEAL_LABELS = {
  breakfast: "Breakfast", lunch: "Lunch", dinner: "Dinner", dessert: "Dessert",
  drinks: "Drinks", other: "Snack",   // `other` is the snack tag, not untagged data
};

const MEAL_COLORS = {
  breakfast: "var(--breakfast)",
  lunch:     "var(--lunch)",
  dinner:    "var(--dinner)",
  dessert:   "var(--dessert)",
  drinks:    "var(--drinks)",
  other:     "var(--other)",
};

// ── Body-composition helpers ─────────────────────────────────────────────────

// Trailing rolling mean over `win` readings — the emphasised "trend" line.
function rollingMean(series, win) {
  return series.map((_, i, arr) => {
    const slice = arr.slice(Math.max(0, i - win + 1), i + 1).filter((p) => p.value != null);
    return slice.length ? slice.reduce((s, p) => s + p.value, 0) / slice.length : null;
  });
}

// Plain-language verdict on the WEIGHT trend only (rewritten 2026-09-12).
//
// It used to split the change into fat vs lean and issue verdicts like "Clean cut —
// lean mass preserved" or "~30% of the loss is lean mass. More protein or a smaller
// deficit." This scale's body-fat % is 99% predictable from weight alone, so its "lean"
// moves ~0.3 lb per lb of weight by construction — every weight loss would have read
// as "losing ~30% lean". Those verdicts were the scale's formula, not a measurement.
// fatRate/leanRate stay in the signature so the caller is unchanged.
function recompVerdict(weightRate, fatRate, leanRate) {
  if (weightRate == null) return null;
  const FLAT = 0.1; // lb/wk dead-band
  const tail = " The scale can't tell fat from lean — the TAPE card below can.";
  if (Math.abs(weightRate) < FLAT) return { text: "Weight stable." + tail };
  return { text: (weightRate < 0 ? "Weight trending down." : "Weight trending up.") + tail };
}

function fmtRate(r, unit) {
  return r == null ? "—" : `${r > 0 ? "+" : ""}${r.toFixed(2)} ${unit}/wk`;
}

// ── Tape measurements (added 2026-09-12) ─────────────────────────────────────
// The scale's body-fat % is 99% explained by weight alone (R^2 0.990 over 26 weeks),
// so it cannot see recomposition — more muscle and less fat at the same weight. A
// tape can: waist down at flat weight = fat loss; arm or chest up with waist flat =
// muscle. Entered in inches, stored in cm. Self-measurement error is ~0.5–1 cm, so
// the change row compares 4-week AVERAGES rather than single readings.
const CM_PER_IN = 2.54;
const TAPE_SITES = [
  { key: "waist_cm", label: "WAIST" },
  { key: "arm_cm", label: "ARM" },
  { key: "chest_cm", label: "CHEST" },
];

function TapeCard() {
  const qc = useQueryClient();
  const { data: rows = [] } = useQuery({
    queryKey: ["measurements"],
    queryFn: () => api.getMeasurements(365),
  });
  const empty = { waist_cm: "", arm_cm: "", chest_cm: "" };
  const [vals, setVals] = useState(empty);
  const save = useMutation({
    mutationFn: (body) => api.logMeasurement(body),
    onSuccess: () => { setVals(empty); qc.invalidateQueries({ queryKey: ["measurements"] }); },
  });
  const del = useMutation({
    mutationFn: (date) => api.deleteMeasurement(date),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["measurements"] }),
  });

  // Local calendar date (en-CA formats as YYYY-MM-DD) — never toISOString(), which
  // rolls over to tomorrow in the evening (never toISOString().slice(0, 10) for a local date).
  const today = new Date().toLocaleDateString("en-CA");
  const parsed = Object.fromEntries(TAPE_SITES.map(s => {
    const v = parseFloat(vals[s.key]);
    return [s.key, Number.isFinite(v) && v > 0 ? +(v * CM_PER_IN).toFixed(1) : null];
  }));
  const canSave = Object.values(parsed).some(v => v != null);
  const latest = rows[0];
  const inches = (cm) => (cm == null ? "—" : (cm / CM_PER_IN).toFixed(1));

  // 4-week average vs the 4 weeks before it, per site.
  const dayMs = 86400000, now = Date.now();
  const avgIn = (key, from, to) => {
    const v = rows.filter(r => {
      const age = (now - new Date(r.date + "T12:00:00").getTime()) / dayMs;
      return age >= from && age < to && r[key] != null;
    }).map(r => r[key]);
    return v.length ? v.reduce((a, b) => a + b, 0) / v.length / CM_PER_IN : null;
  };

  return (
    <Card>
      <Eyebrow left="TAPE" right="INCHES" />
      <div className="mt-2 grid grid-cols-3 gap-2">
        {TAPE_SITES.map(s => (
          <label key={s.key} className="block">
            <span className="text-[8.5px] tracking-micro text-ink-3">{s.label}</span>
            <input type="number" inputMode="decimal" step="0.1" min="0"
              value={vals[s.key]} placeholder={latest ? inches(latest[s.key]) : "—"}
              onChange={e => setVals(v => ({ ...v, [s.key]: e.target.value }))}
              className="mt-0.5 w-full rounded-md border border-rule bg-transparent px-2 py-1 text-[13px] text-ink-2" />
          </label>
        ))}
      </div>
      <button disabled={!canSave || save.isPending}
        onClick={() => save.mutate({ date: today, ...parsed })}
        className="mt-2 w-full rounded-md border border-rule py-1.5 text-[11px] font-semibold tracking-micro text-ink-2 disabled:opacity-40">
        {save.isPending ? "SAVING…" : "LOG TODAY"}
      </button>
      {save.isError && (
        <p className="mt-1 text-[11px]" style={{ color: "var(--over)" }}>
          {String(save.error?.message ?? "Save failed")}
        </p>
      )}

      {rows.length > 0 && (
        <div className="mt-3 flex gap-2 border-t border-rule pt-3">
          {TAPE_SITES.map(s => {
            const cur = avgIn(s.key, 0, 28), prev = avgIn(s.key, 28, 56);
            const d = cur != null && prev != null ? cur - prev : null;
            return (
              <div key={s.key} className="flex-1">
                <p className="text-[8.5px] tracking-micro text-ink-3">{s.label}</p>
                <p className="text-[13px] font-semibold text-ink-2">{cur == null ? "—" : cur.toFixed(1)}</p>
                <p className="text-[8.5px] leading-none text-ink-3">
                  {d == null ? "4-wk avg" : `${d > 0 ? "+" : ""}${d.toFixed(1)} vs prior 4 wk`}
                </p>
              </div>
            );
          })}
        </div>
      )}

      {rows.length > 0 && (
        <div className="mt-2 space-y-0.5">
          {rows.slice(0, 5).map(r => (
            <div key={r.date} className="flex items-center gap-2 text-[11px] text-ink-2">
              <span className="w-20 flex-none text-ink-3">{fmtDate(r.date)}</span>
              {TAPE_SITES.map(s => <span key={s.key} className="w-12 flex-none">{inches(r[s.key])}</span>)}
              <button onClick={() => del.mutate(r.date)} className="ml-auto text-ink-3" title="Delete">×</button>
            </div>
          ))}
        </div>
      )}

      <Foot>
        Weekly, same morning, before eating. Waist at the navel, relaxed, end of a normal
        breath out; arm flexed at its widest; chest at nipple height. Take each twice and
        average. Changes under ~0.4 in are within tape error.
      </Foot>
    </Card>
  );
}

// ── Body scans (InBody, 2026-10-29) ──────────────────────────────────────────
// A segmental (hand + foot) scan every few months. Its job is to CHECK the home scale,
// whose lean mass ran a steady ~14 lb above the scan at two scans, and to give a
// fat/lean split that is not just weight re-drawn. Changes between scans smaller than
// the device's repeatability are labelled as such — rough test-retest figures for
// 8-electrode BIA, not a calibration of this machine.
const SCAN_NOISE = { weight_lb: 0.5, body_fat_pct: 1.0, fat_mass_lb: 2.0, lean_mass_lb: 2.0, smm_lb: 1.0 };
const SCAN_FIELDS = [
  { key: "weight_lb",    label: "WEIGHT",   unit: "lb", req: true },
  { key: "body_fat_pct", label: "BODY FAT", unit: "%",  req: true },
  { key: "smm_lb",       label: "SMM",      unit: "lb" },
  { key: "fat_mass_lb",  label: "FAT MASS", unit: "lb" },
  { key: "lean_mass_lb", label: "LEAN",     unit: "lb" },
  { key: "tbw_lb",       label: "WATER",    unit: "lb" },
];
const SCAN_SEGMENTS = [
  { key: "left_arm", label: "L ARM" }, { key: "right_arm", label: "R ARM" }, { key: "trunk", label: "TRUNK" },
  { key: "left_leg", label: "L LEG" }, { key: "right_leg", label: "R LEG" },
];

function ScanForm({ onDone }) {
  const qc = useQueryClient();
  const [date, setDate] = useState(new Date().toLocaleDateString("en-CA"));
  const [time, setTime] = useState("");
  const [vals, setVals] = useState({});
  const [segs, setSegs] = useState({});
  const num = (v) => { const x = parseFloat(v); return Number.isFinite(x) && x > 0 ? x : null; };
  const save = useMutation({
    mutationFn: (body) => api.logScan(body),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["scans"] }); onDone(); },
  });
  const body = {
    date, time: time || null, device: "InBody",
    ...Object.fromEntries(SCAN_FIELDS.map((f) => [f.key, num(vals[f.key])])),
  };
  const segVals = Object.fromEntries(SCAN_SEGMENTS.map((s) => [s.key, num(segs[s.key])]).filter(([, v]) => v != null));
  if (Object.keys(segVals).length) body.segments = segVals;
  const canSave = body.weight_lb != null && body.body_fat_pct != null && date;
  const input = "mt-0.5 w-full rounded-md border border-rule bg-transparent px-2 py-1 text-[13px] text-ink-2";
  const field = (key, label, value, onChange, type = "number") => (
    <label key={key} className="block">
      <span className="block truncate text-[8.5px] tracking-micro text-ink-3">{label}</span>
      <input type={type} inputMode={type === "number" ? "decimal" : undefined} step="0.1" min="0"
        value={value ?? ""} onChange={(e) => onChange(e.target.value)} className={input} />
    </label>
  );
  return (
    <div className="mt-3 border-t border-rule pt-3">
      <div className="grid grid-cols-2 gap-2">
        {field("date", "DATE", date, setDate, "date")}
        {field("time", "TIME", time, setTime, "time")}
      </div>
      <div className="mt-2 grid grid-cols-3 gap-2">
        {SCAN_FIELDS.map((f) => field(f.key, `${f.label} ${f.unit.toUpperCase()}`, vals[f.key],
          (v) => setVals((s) => ({ ...s, [f.key]: v }))))}
      </div>
      <p className="mt-2 text-[8.5px] tracking-micro text-ink-3">SEGMENTAL LEAN (LB) · OPTIONAL</p>
      <div className="mt-0.5 grid grid-cols-5 gap-1.5">
        {SCAN_SEGMENTS.map((s) => field(s.key, s.label, segs[s.key], (v) => setSegs((x) => ({ ...x, [s.key]: v }))))}
      </div>
      <p className="mt-2 text-[10.5px] leading-snug text-ink-3">
        Weight and body fat are required. Fat mass and lean are worked out from them if
        left blank.
      </p>
      <div className="mt-2 flex gap-2">
        <button onClick={onDone}
          className="flex-1 rounded-md border border-rule py-1.5 text-[11px] font-semibold tracking-micro text-ink-3">
          CANCEL
        </button>
        <button disabled={!canSave || save.isPending} onClick={() => save.mutate(body)}
          className="flex-1 rounded-md border border-rule py-1.5 text-[11px] font-semibold tracking-micro text-ink-2 disabled:opacity-40">
          {save.isPending ? "SAVING…" : "SAVE SCAN"}
        </button>
      </div>
      {save.isError && (
        <p className="mt-1 text-[11px]" style={{ color: "var(--over)" }}>
          {String(save.error?.message ?? "Save failed")}
        </p>
      )}
    </div>
  );
}

function ScanCard() {
  const qc = useQueryClient();
  const [adding, setAdding] = useState(false);
  const { data: scans = [] } = useQuery({ queryKey: ["scans"], queryFn: api.getScans });
  const del = useMutation({
    mutationFn: (date) => api.deleteScan(date),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["scans"] }),
  });
  const [latest, prev] = scans;
  const signed = (x, d = 1) => `${x > 0 ? "+" : ""}${x.toFixed(d)}`;
  const home = latest?.home_scale;
  // Only worth saying when the two scans were taken at clearly different hours.
  const mins = (t) => { const [h, m] = (t || "").split(":").map(Number); return Number.isFinite(h) ? h * 60 + (m || 0) : null; };
  const timeShift = latest && prev && mins(latest.time) != null && mins(prev.time) != null
    && Math.abs(mins(latest.time) - mins(prev.time)) >= 60;

  return (
    <Card>
      <Eyebrow left="BODY SCAN" right={latest ? `${fmtDate(latest.date)} · LB` : "INBODY"} />
      {latest ? (<>
        <Hero value={latest.body_fat_pct.toFixed(1)} unit={["% BODY FAT", `${scans.length} SCAN${scans.length === 1 ? "" : "S"}`]} />
        <HeroSub>
          Fat {latest.fat_mass_lb?.toFixed(1) ?? "—"} · lean {latest.lean_mass_lb?.toFixed(1) ?? "—"}
          {latest.smm_lb != null && <> · muscle {latest.smm_lb.toFixed(1)}</>} lb at {latest.weight_lb.toFixed(1)} lb.
        </HeroSub>

        {prev && (
          <div className="mt-3 flex gap-2 border-t border-rule pt-3">
            {[
              { key: "weight_lb", label: "WEIGHT" }, { key: "fat_mass_lb", label: "FAT" },
              { key: "lean_mass_lb", label: "LEAN" }, { key: "smm_lb", label: "MUSCLE" },
            ].map(({ key, label }) => {
              const d = latest[key] != null && prev[key] != null ? latest[key] - prev[key] : null;
              const noise = d != null && Math.abs(d) < SCAN_NOISE[key];
              return (
                <div key={key} className="flex-1">
                  <p className="text-[8.5px] tracking-micro text-ink-3">{label}</p>
                  <p className={`text-[13px] font-semibold ${noise ? "text-ink-3" : "text-ink-2"}`}>
                    {d == null ? "—" : signed(d)}
                  </p>
                  <p className="text-[8.5px] leading-none text-ink-3">{noise ? "within noise" : "lb"}</p>
                </div>
              );
            })}
          </div>
        )}
        {prev && (
          <p className="prose-ui mt-2 text-[10.5px] leading-snug text-ink-3">
            Change since {fmtDate(prev.date)}
            {timeShift && <> · scanned at {prev.time} then, {latest.time} now — time of day moves body water</>}.
          </p>
        )}

        {home && home.lean_gap_lb != null && (
          <p className="prose-ui mt-3 border-t border-rule pt-3 text-[11.5px] leading-snug text-ink-2">
            <K>Home scale</K> ({fmtDate(home.date)}): lean {home.lean_mass_lb.toFixed(1)} lb, {signed(home.lean_gap_lb)} lb vs
            this scan{home.bf_gap_pct != null && <>; body fat {home.body_fat_pct.toFixed(1)}%, {signed(home.bf_gap_pct)} pts</>}.
          </p>
        )}

        {latest.segments && (
          <div className="mt-3 flex gap-2 border-t border-rule pt-3">
            {SCAN_SEGMENTS.map((s) => (
              <div key={s.key} className="flex-1">
                <p className="text-[8.5px] tracking-micro text-ink-3">{s.label}</p>
                <p className="text-[12px] font-semibold text-ink-2">{latest.segments[s.key]?.toFixed(1) ?? "—"}</p>
              </div>
            ))}
          </div>
        )}

        <div className="mt-3 space-y-0.5 border-t border-rule pt-2">
          {scans.map((r) => (
            <div key={r.date} className="flex items-center gap-2 text-[11px] text-ink-2">
              <span className="w-20 flex-none text-ink-3">{fmtDate(r.date)}</span>
              <span className="w-14 flex-none">{r.weight_lb.toFixed(1)}</span>
              <span className="w-12 flex-none">{r.body_fat_pct.toFixed(1)}%</span>
              <span className="w-14 flex-none">{r.smm_lb != null ? r.smm_lb.toFixed(1) : "—"}</span>
              <button onClick={() => { if (confirm(`Delete the ${fmtDate(r.date)} scan?`)) del.mutate(r.date); }}
                className="ml-auto text-ink-3" title="Delete">×</button>
            </div>
          ))}
        </div>
      </>) : (
        !adding && <HeroSub>No scans yet. Enter the printout from an InBody (or similar hand-and-foot) scan.</HeroSub>
      )}

      {adding
        ? <ScanForm onDone={() => setAdding(false)} />
        : <button onClick={() => setAdding(true)}
            className="mt-3 w-full rounded-md border border-rule py-1.5 text-[11px] font-semibold tracking-micro text-ink-2">
            + ADD SCAN
          </button>}

      <Foot>
        A hand-and-foot scan measures the arms and trunk, which the home scale only estimates
        from weight, so its fat/lean split is the better one. It is still bioimpedance: a
        repeat scan moves ~1 lb lean and ~1 pt body fat on its own, more after a meal, a
        workout or a different time of day. Scan at the same hour, fasted. DEXA is the
        instrument that can see a 2 lb muscle change.
      </Foot>
    </Card>
  );
}

// ── My Foods entry point (2026-09-13) ─────────────────────────────────────────
// The library of foods worth precise facts (docs/MY_FOODS.md). Lives under Nutrition;
// the review and the library open full-screen.
function MyFoodsCard() {
  const [open, setOpen] = useState(false);
  const qc = useQueryClient();
  const { data: foods = [] } = useQuery({ queryKey: ["my-foods"], queryFn: () => api.listMyFoods() });
  const { data: cands } = useQuery({ queryKey: ["my-food-candidates", 15], queryFn: () => api.myFoodCandidates(15) });
  const toReview = (cands?.groups ?? []).reduce((a, g) => a + g.total, 0);
  return (
    <Card>
      <Eyebrow left="MY FOODS" right={`${foods.length} IN LIBRARY`} />
      <HeroSub>
        {toReview > 0
          ? <>{toReview} foods you log repeatedly are waiting for review — confirm their numbers
              once and every future entry uses them.</>
          : <>Your library of foods with checked numbers.</>}
      </HeroSub>
      <button onClick={() => setOpen(true)}
        className="mt-3 w-full rounded-md border border-rule py-1.5 text-[11px] font-semibold tracking-micro text-ink-2">
        {toReview > 0 ? "REVIEW & BUILD LIBRARY →" : "OPEN LIBRARY →"}
      </button>
      {open && <MyFoodsModal onClose={() => {
        setOpen(false);
        // Created / skipped foods leave the review, so the count here must refresh.
        qc.invalidateQueries({ queryKey: ["my-food-candidates"] });
      }} />}
    </Card>
  );
}

export default function Body({ window = "3M", onWindowChange, windowOffset = 0, onWindowOffsetChange }) {
  const [nutrientMode, setNutrientMode] = useState("calories");
  const [hlType, setHlType] = useState(null);     // behaviour highlight on the composition chart
  const { start, end } = windowDates(window, windowOffset);

  // ── Data queries ────────────────────────────────────────────────────────────

  const { data: events = [] } = useQuery({
    queryKey: ["events", start, end],
    queryFn: () => api.events(start, end),
  });
  const { data: ahWeight = [] } = useQuery({
    queryKey: ["weight-trend", start, end],
    queryFn: () => api.rawTrend("weight", start, end),
  });
  const { data: manualWeight = [] } = useQuery({
    queryKey: ["weight", window],
    queryFn: () => api.getWeight(365),
  });
  const { data: bodyFat = [] } = useQuery({
    queryKey: ["bf-trend", start, end],
    queryFn: () => api.rawTrend("body_fat_percentage", start, end),
  });
  const { data: leanMass = [] } = useQuery({
    queryKey: ["lean-trend", start, end],
    queryFn: () => api.rawTrend("lean_body_mass", start, end),
  });
  const { data: scans = [] } = useQuery({ queryKey: ["scans"], queryFn: api.getScans });
  const { data: settings } = useQuery({
    queryKey: ["settings"],
    queryFn: api.getSettings,
  });
  const { data: nutritionDaily = [] } = useQuery({
    queryKey: ["nutrition-daily", start, end],
    queryFn: () => api.getNutritionDaily(start, end),
  });
  const { data: nutritionBreakdown = [] } = useQuery({
    queryKey: ["nutrition-breakdown", start, end],
    queryFn: () => api.getNutritionBreakdown(start, end),
  });
  const { data: energy } = useQuery({
    queryKey: ["energy", start, end],
    queryFn: () => api.getEnergy(start, end),
  });
  // Trends — for directional glow + changepoint shading (no badges: the composition
  // tiles already show per-week rates). Same lookback as the visible window.
  const { data: trendsData } = useQuery({
    queryKey: ["trends", windowToTrendDays(window)],
    queryFn: () => api.getTrends(windowToTrendDays(window)),
    staleTime: 1000 * 60 * 30,
  });
  const trend = Object.fromEntries((trendsData?.results ?? []).map(r => [r.key, r]));

  const consistencyQ = useQuery({
    queryKey: ["nutrition-consistency"],
    queryFn: () => api.getNutritionConsistency(12),
    staleTime: 1000 * 60 * 30,
  });
  const nutConsistency = consistencyQ.data;
  const { data: interventions = [] } = useQuery({ queryKey: ["interventions"], queryFn: api.listInterventions });

  // Changepoint ReferenceArea for a chart keyed on `label`, matched via the raw `date`.
  const cpArea = (cp, rows) => {
    if (!cp) return null;
    const i = rows.findIndex(d => d.date && d.date >= cp.date);
    if (i < 0) return null;
    return <ReferenceArea x1={rows[i].label} x2={rows[rows.length - 1].label} fill="var(--breakfast)" fillOpacity={0.08} ifOverflow="hidden"
      label={{ value: `step ${cp.shift}`, position: "insideTopLeft", fill: "var(--breakfast)", fontSize: 9 }} />;
  };
  // ── Derived data ────────────────────────────────────────────────────────────

  // Weight chart
  const manualByDate = {};
  manualWeight.forEach((w) => {
    const d = w.timestamp?.slice(0, 10);
    if (d) manualByDate[d] = kgToLbs(w.weight_kg);
  });
  const weightData = ahWeight
    .filter((r) => r.date >= start)
    .map((r) => ({ date: r.date, label: fmtDate(r.date), value: manualByDate[r.date] ?? r.value }));
  Object.entries(manualByDate).forEach(([d, v]) => {
    if (!ahWeight.find((r) => r.date === d) && d >= start && d <= end)
      weightData.push({ date: d, label: fmtDate(d), value: v });
  });
  weightData.sort((a, b) => a.date.localeCompare(b.date));

  const vals = weightData.map((r) => r.value);
  const reg = linearRegression(vals);
  const weightChartData = weightData.map((r, i) => ({
    ...r,
    trend: reg ? parseFloat(reg.predict(i).toFixed(1)) : null,
  }));

  const latestWeight  = weightData[weightData.length - 1]?.value;

  // ── Body composition (weight + body fat % + lean; fat mass derived) ──────────
  const bfData   = bodyFat.filter((r) => r.date >= start).map((r) => ({ date: r.date, label: fmtDate(r.date), value: r.value }));
  const leanData = leanMass.filter((r) => r.date >= start).map((r) => ({ date: r.date, label: fmtDate(r.date), value: r.value }));
  const latestBf   = bfData[bfData.length - 1]?.value;
  const latestLean = leanData[leanData.length - 1]?.value;

  // Per-week rates come from the trends endpoint (robust Theil–Sen), so the tiles match
  // the "What's Moving" card exactly — one source of truth. Fat-mass rate is derived
  // (d(weight) = d(fat) + d(lean), so fatRate = weightRate − leanRate, exactly).
  const weightRate = trend.weight?.per_week ?? null;
  const bfRate     = trend.body_fat?.per_week ?? null;
  const leanRate   = trend.lean?.per_week ?? null;
  const fatRate    = (weightRate != null && leanRate != null) ? weightRate - leanRate : null;
  const recomp     = recompVerdict(weightRate, fatRate, leanRate);

  // Body-fat chart: faint per-reading line + emphasised ~2-week trailing trend.
  const bfTrend = rollingMean(bfData, 7);
  // ── Stacked composition: lean measured, fat derived as weight − lean ────────
  // The three separate charts this replaces (weight line, body-fat % line, prose
  // verdict) each answered part of one question. Stacked, the segments sum to the
  // measured weight, so "did that gain go on as fat or lean" is read directly off
  // the two bands instead of inferred from three places.
  const compSeries = (() => {
    const byDate = new Map();
    for (const r of weightData) if (r.value != null) byDate.set(r.date, { date: r.date, label: r.label, weight: r.value });
    for (const r of leanData) {
      const e = byDate.get(r.date);
      if (e && r.value != null) { e.lean = r.value; e.fat = parseFloat((e.weight - r.value).toFixed(1)); }
    }
    const rows = [...byDate.values()].filter((e) => e.lean != null)
      .sort((a, b) => a.date.localeCompare(b.date));
    // Plot CHANGE from the window's first reading, not absolute mass.
    //
    // Absolute stacking failed at the actual job: lean and fat in the tens to
    // hundreds of lb put the axis near 0–200, where the 1–2 lb moves that matter are a pixel of
    // thickness and both bands look perfectly flat. Deltas re-centre the axis on
    // zero, so a couple of pounds fills the plot — and because Δlean + Δfat is
    // exactly Δweight, the decomposition still sums correctly.
    const base = rows[0];
    if (!base) return [];
    return rows.map((e) => ({
      ...e,
      dLean: parseFloat((e.lean - base.lean).toFixed(1)),
      dFat: parseFloat((e.fat - base.fat).toFixed(1)),
      dWeight: parseFloat((e.weight - base.weight).toFixed(1)),
    }));
  })();

  // Behaviour highlight (same day, lagDays=0 — weight responds to water/glycogen the
  // same day). Points mark Δweight on those days. The contrast is on the RESIDUAL from
  // the window's linear trend, not on Δweight itself: Δweight-from-start is dominated by
  // the trend, so a cut would make whichever days fell late in the window look "lighter".
  const compReg = linearRegression(compSeries.map((r) => r.dWeight));
  const compResid = compSeries.map((r, i) => ({ ...r, resid: compReg ? r.dWeight - compReg.predict(i) : null }));
  // Scan days as vertical markers, snapped to the first plotted reading on/after the scan.
  // Markers only: the scan's lean sits ~14 lb under the home scale's, so plotting its
  // values on this Δ-from-start axis would compare two instruments.
  const scanMarks = scans.map((sc) => compSeries.find((r) => r.date >= sc.date))
    .filter((r, i, a) => r && a.indexOf(r) === i);
  const compHl = computeHighlight({ events, activeType: hlType, data: compResid, isoKey: "date", valueKey: "dWeight" });
  const compHlStats = computeHighlight({ events, activeType: hlType, data: compResid, isoKey: "date", valueKey: "resid" });

  const bfChartData = bfData.map((r, i) => ({
    ...r,
    value: parseFloat(r.value.toFixed(1)),
    trend: bfTrend[i] != null ? parseFloat(bfTrend[i].toFixed(1)) : null,
  }));

  // Nutrition goals
  const proteinGoal = settings?.protein_goal_g != null
    ? settings.protein_goal_g
    : latestWeight != null && settings
      ? Math.round(latestWeight * (PROTEIN_MULT[settings.training_goal] ?? 0.8))
      : null;
  const calorieGoal = settings?.calorie_goal_effective ?? settings?.calorie_goal ?? null;
  const calorieDynamic = settings?.calorie_goal_dynamic ?? false;

  // The app has ONE belief about maintenance: the weight-calibrated adaptive TDEE
  // from services/energy, which the Energy Balance card below reports with a CI.
  // A settings number labelled "maintenance" in another card contradicts it — same
  // word, different value, on the same screen. Prefer the calibrated figure and say
  // which one is being used; fall back to the configured goal only when the fit
  // isn't available, and call that a goal rather than maintenance.
  const adaptiveTdee =
    energy?.available && energy.adaptive?.confidence && energy.adaptive.confidence !== "none"
      ? energy.adaptive.tdee : null;

  const breakdownChartData = nutritionBreakdown.map((r) => ({
    ...r,
    label: fmtDate(r.date),
    total_cal: (r.breakfast_cal || 0) + (r.lunch_cal || 0) + (r.dinner_cal || 0) + (r.drinks_cal || 0) + (r.other_cal || 0),
    total_pro: (r.breakfast_pro || 0) + (r.lunch_pro || 0) + (r.dinner_pro || 0) + (r.drinks_pro || 0) + (r.other_pro || 0),
  }));
  const hasNutrition = nutritionBreakdown.length > 0;

  // 7-day rolling average, drawn on the per-day chart (note 45). Rebuilt 2026-09-23:
  // the old version was computed but never drawn after the calorie/protein charts were
  // merged, averaged in today's partial day and incomplete_data days (each reads as a
  // low day), and summed a meal list that predated Dessert. Now: trailing 7 CALENDAR
  // days, COMPLETE days only (server-side `complete`), at least 4 of them, else a gap.
  const ROLL = 7;
  const ROLL_MIN = 4;
  const ALL_MEALS = ["breakfast", "lunch", "dinner", "dessert", "drinks", "other"];
  const dayTotal = (r, suf) => ALL_MEALS.reduce((a, m) => a + (r[`${m}_${suf}`] || 0), 0);
  const breakdownWithTrend = breakdownChartData.map((row) => {
    const t = new Date(`${row.date}T12:00:00`).getTime();
    const win = breakdownChartData.filter((d) => {
      const dt = (t - new Date(`${d.date}T12:00:00`).getTime()) / 864e5;
      return d.complete && dt >= 0 && dt < ROLL;
    });
    const avg = (suf) => win.length >= ROLL_MIN
      ? Math.round((win.reduce((a, d) => a + dayTotal(d, suf), 0) / win.length) * (suf === "cal" ? 1 : 10)) / (suf === "cal" ? 1 : 10)
      : null;
    return { ...row, cal_avg: avg("cal"), pro_avg: avg("pro"), fib_avg: avg("fib") };
  });

  // Current 7-day average over COMPLETE days (exclude today's partial) — printed readout
  const _now = new Date();
  const _todayISO = `${_now.getFullYear()}-${String(_now.getMonth() + 1).padStart(2, "0")}-${String(_now.getDate()).padStart(2, "0")}`;
  const last7Complete = breakdownChartData.filter((d) => d.date < _todayISO).slice(-ROLL);
  const cal7Avg = last7Complete.length ? Math.round(last7Complete.reduce((s, d) => s + d.total_cal, 0) / last7Complete.length) : null;
  const pro7Avg = last7Complete.length ? Math.round(last7Complete.reduce((s, d) => s + d.total_pro, 0) / last7Complete.length) : null;

  // Meal type averages over logged days (days where that meal had > 0 kcal)
  const MEALS = ["breakfast", "lunch", "dinner", "dessert", "drinks", "other"];
  const mealAverages = MEALS.map((m) => {
    const days = nutritionBreakdown.filter((r) => (r[`${m}_cal`] || 0) > 0);
    const avgCal = days.length ? Math.round(days.reduce((s, r) => s + (r[`${m}_cal`] || 0), 0) / days.length) : 0;
    const avgPro = days.length ? Math.round(days.reduce((s, r) => s + (r[`${m}_pro`] || 0), 0) / days.length) : 0;
    return { meal: m, avgCal, avgPro, days: days.length, color: MEAL_COLORS[m] };
  }).filter((m) => m.days > 0);

  const [view, setView] = useState("measures");

  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <div className="space-y-4 p-4">
      <TimeWindowSelector value={window} onChange={onWindowChange} offset={windowOffset} onOffsetChange={onWindowOffsetChange} />

      {/* Two questions that happen to share a tab: what my body is doing, and
          what I am eating. Split in place rather than into two files — Body's
          chart components are defined locally and mechanical extraction has
          silently dropped identifiers here before. */}
      <SegmentedTabs
        value={view}
        onChange={setView}
        options={[
          { value: "measures",  label: "MEASURES" },
          { value: "nutrition", label: "NUTRITION" },
        ]}
      />

      {view === "measures" && (<>
      <EnergyCalibrationCard window={window} />

      {/* ── Composition ────────────────────────────────────────────────────
          One card replacing four: three rate tiles, a prose recomposition verdict,
          a weight line and a body-fat line. They were all answering the same
          question from different angles — is my mass changing, and is the change
          fat or lean? Stacked lean+fat sums to measured weight, so the answer is
          the shape of the two bands. */}
      <Card>
        <Eyebrow left="COMPOSITION" right={window} />
        <Hero
          value={latestWeight != null ? latestWeight.toFixed(1) : "—"}
          unit={["LB", fmtRate(weightRate, "lb").toUpperCase()]}
        />
        {recomp && <HeroSub>{recomp.text}</HeroSub>}

        {compSeries.length > 1 ? (
          <div className="mt-3">
            <EventFilterPills events={events} active={hlType} onChange={setHlType} lagNote="same day · vs the window's trend" />
            <HighlightContrast hl={compHlStats} fmt={(x) => `${x > 0 ? "+" : ""}${x.toFixed(1)} lb`} />
            <ResponsiveContainer width="100%" height={170}>
              {/* stackOffset="sign" so gains stack upward and losses downward from
                  zero — the two can point in opposite directions, which is exactly
                  what recomposition looks like and what a plain stack would hide. */}
              <ComposedChart data={compHl.data} stackOffset="sign" margin={{ top: 4, right: 8, bottom: 0, left: -8 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--rule)" vertical={false} />
                <XAxis dataKey="label" tick={{ fill: "var(--ink-3)", fontSize: 10 }} interval="preserveStartEnd" />
                <YAxis tick={{ fill: "var(--ink-3)", fontSize: 10 }}
                  tickFormatter={(v) => `${v > 0 ? "+" : ""}${v}`} />
                <Tooltip
                  contentStyle={{ background: "var(--canvas)", border: "1px solid var(--rule)", borderRadius: 8, fontSize: 12 }}
                  labelStyle={{ color: "var(--ink-3)" }}
                  formatter={(v, n) => [`${v > 0 ? "+" : ""}${v} lb`, n === "dLean" ? "Lean" : n === "dFat" ? "Fat" : `Weight (${n})`]} />
                <ReferenceLine y={0} stroke="var(--ink-2)" strokeWidth={1} />
                {interventionMarkers(interventions, compSeries, "date", "label")}
                {scanMarks.map((r) => (
                  <ReferenceLine key={`scan-${r.date}`} x={r.label} stroke="var(--ink-2)" strokeOpacity={0.6}
                    label={{ value: "scan", position: "insideBottomRight", fill: "var(--ink-2)", fontSize: 9 }} />
                ))}
                <Area dataKey="dLean" name="dLean" stackId="c" stroke="var(--rhr)" fill="var(--rhr)"
                  fillOpacity={compHl.active ? 0.12 : 0.4} strokeOpacity={compHl.active ? 0.3 : 1}
                  strokeWidth={1.5} isAnimationActive={false} />
                <Area dataKey="dFat" name="dFat" stackId="c" stroke="var(--breakfast)" fill="var(--breakfast)"
                  fillOpacity={compHl.active ? 0.12 : 0.4} strokeOpacity={compHl.active ? 0.3 : 1}
                  strokeWidth={1.5} isAnimationActive={false} />
                {compHl.active && (
                  <Scatter dataKey="_hl" name={compHl.label} fill={compHl.color} isAnimationActive={false} />
                )}
              </ComposedChart>
            </ResponsiveContainer>
            <ChartEventKey interventions={interventions} rows={compSeries} isoKey="date" />
            <Legend items={[
              { label: "Lean", color: "var(--rhr)",
                note: (() => { const d = compSeries[compSeries.length - 1]?.dLean;
                  return d == null ? "" : `${d > 0 ? "+" : ""}${d} lb`; })() },
              { label: "Fat", color: "var(--breakfast)",
                note: (() => { const d = compSeries[compSeries.length - 1]?.dFat;
                  return d == null ? "" : `${d > 0 ? "+" : ""}${d} lb`; })() },
            ]} />
          </div>
        ) : (
          <p className="mt-3 text-[11px] text-ink-3">
            Not enough paired weight + lean-mass readings in this window to split the change.
          </p>
        )}

        {/* Rates, as a row rather than three tiles. Neutral inside the dead-band —
            below it the per-week rate is noise, and colouring noise trains you to
            read it as signal. */}
        <div className="mt-3 flex gap-2 border-t border-rule pt-3">
          {[
            { label: "WEIGHT", rate: weightRate, unit: "lb", goodDown: true,  flat: 0.1 },
            { label: "FAT",    rate: fatRate,    unit: "lb", goodDown: true,  flat: 0.1 },
            { label: "LEAN",   rate: leanRate,   unit: "lb", goodDown: false, flat: 0.1 },
          ].map((r) => {
            let color = "var(--ink-3)";
            if (r.rate != null && Math.abs(r.rate) >= r.flat) {
              color = (r.goodDown ? r.rate < 0 : r.rate > 0) ? "var(--rhr)" : "var(--over)";
            }
            return (
              <div key={r.label} className="flex-1">
                <p className="text-[8.5px] tracking-micro text-ink-3">{r.label}</p>
                <p className="text-[13px] font-semibold" style={{ color }}>
                  {r.rate == null ? "—" : `${r.rate > 0 ? "+" : ""}${r.rate.toFixed(2)}`}
                </p>
                <p className="text-[8.5px] leading-none text-ink-3">{r.unit}/wk</p>
              </div>
            );
          })}
        </div>

        <Foot>
          Change since the start of this window, not absolute mass. Read the split with
          care: this scale does not really measure composition. Over 26 weeks its
          body-fat % was 99% predictable from weight alone, and lean is simply weight ×
          (1 − body-fat %) — so the lean/fat bands mostly re-draw the weight change
          (about 0.3 lb of "lean" per lb of weight) and cannot show recomposition. The
          TAPE card below can. Rates are Theil–Sen over {window} — the same robust fit
          the Trends tab uses, so they agree.
          {scans[0]?.home_scale?.lean_gap_lb != null && <> Against the {fmtDate(scans[0].date)} body
            scan this scale reads lean {scans[0].home_scale.lean_gap_lb > 0 ? "+" : ""}
            {scans[0].home_scale.lean_gap_lb.toFixed(1)} lb{scanMarks.length > 0 && ' (the solid "scan" lines)'}.</>}
        </Foot>
      </Card>

      <TapeCard />

      <ScanCard />

      {/* ── Energy Balance / TDEE ─────────────────────────────────────────── */}
      {energy && energy.available === false && (
        <Card>
          <Eyebrow left="ENERGY BALANCE" />
          <p className="prose-ui mt-2 text-[11.5px] leading-snug text-ink-2">{energy.reason}</p>
        </Card>
      )}
      {energy && energy.available && (() => {
        const a = energy.adaptive || {};
        const L = energy.latest || {};
        const hasAdaptive = a.confidence && a.confidence !== "none";
        const headlineTdee = hasAdaptive ? a.tdee : L.tdee_prior;
        const headlineKind = hasAdaptive ? "Adaptive (weight-calibrated)" : "Estimate (RMR + active)";
        const hasCi = hasAdaptive && a.tdee_ci_low != null && a.tdee_ci_high != null;
        const wkChange = a.weekly_weight_change_kg;
        const confColor = { high: "var(--rhr)", medium: "var(--breakfast)", low: "var(--over)", none: "var(--ink-3)" }[a.confidence || "none"];
        const chartData = (energy.series || [])
          .filter((d) => d.intake != null || d.tdee_prior != null)
          .map((d) => ({ ...d, label: fmtDate(d.date) }));
        return (
          <Card>
            <Eyebrow left="ENERGY BALANCE" right={hasAdaptive ? "WEIGHT-CALIBRATED" : "ESTIMATE"} />
            {/* Maintenance is the number this card exists to deliver — the other two
                figures are how it was arrived at, so they sit underneath as working
                rather than beside it as equals. The CI rides in the hero-sub because
                a maintenance estimate without its uncertainty invites false precision. */}
            <Hero value={headlineTdee ?? "—"} unit={["KCAL", "MAINTENANCE"]} />
            <HeroSub>
              {hasAdaptive
                ? <>From <K>{a.n_intake_days}</K> logged days against your weight trend
                    {hasCi && <> · 95% CI <K>{a.tdee_ci_low}–{a.tdee_ci_high}</K></>}
                    {" "}· confidence <span style={{ color: confColor }}>{a.confidence}</span>
                    {a.confidence === "low" && " — needs ~2 weeks of logging plus weigh-ins to firm up"}</>
                : <>{headlineKind} — not yet calibrated against your weight trend.</>}
            </HeroSub>

            <div className="mt-3 flex gap-2 border-t border-rule pt-3">
              {[
                { label: "RMR + ACTIVE", value: L.tdee_prior ?? "—", unit: "kcal",
                  sub: `${L.bmr ?? "—"} + ${L.active_calories ?? "—"}` },
                { label: "WEIGHT TREND",
                  value: wkChange != null ? `${wkChange > 0 ? "+" : ""}${(wkChange * 2.20462).toFixed(2)}` : "—",
                  unit: "lb/wk", sub: L.weight_lbs ? `${L.weight_lbs} lb now` : "—" },
                { label: "MEAN INTAKE", value: a.mean_intake ?? "—", unit: "kcal",
                  sub: hasAdaptive ? `${a.n_intake_days} days` : "—" },
              ].map((t) => (
                <div key={t.label} className="flex-1">
                  <p className="text-[8.5px] tracking-micro text-ink-3">{t.label}</p>
                  <p className="text-[15px] font-semibold leading-tight text-ink">
                    {t.value}<span className="text-[9px] font-normal text-ink-3"> {t.unit}</span>
                  </p>
                  <p className="text-[8.5px] leading-none text-ink-3">{t.sub}</p>
                </div>
              ))}
            </div>
            {chartData.length > 1 && (
              <ResponsiveContainer width="100%" height={180}>
                <ComposedChart data={chartData} margin={{ top: 4, right: 8, bottom: 0, left: -20 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--rule)" />
                  <XAxis dataKey="label" tick={{ fill: "var(--ink-3)", fontSize: 10 }} />
                  <YAxis tick={{ fill: "var(--ink-3)", fontSize: 10 }} />
                  <Tooltip contentStyle={{ background: "var(--canvas)", border: "1px solid var(--rule)", borderRadius: 8, fontSize: 12 }} />
                  <Bar dataKey="intake" name="Intake" fill="var(--dinner)" radius={[2, 2, 0, 0]} isAnimationActive={false} />
                  <Line dataKey="tdee_prior" name="Out (RMR + active)" stroke="var(--breakfast)" strokeWidth={2} dot={false} connectNulls isAnimationActive={false} />
                  {/* Dashed = calibrated maintenance, the same convention the nutrition
                      bars use for "what you actually do" versus a set target. */}
                  {headlineTdee && <ReferenceLine y={headlineTdee} stroke="var(--ink)" strokeDasharray="4 3" strokeOpacity={0.8} />}
                </ComposedChart>
              </ResponsiveContainer>
            )}
            <Legend items={[
              { label: "Intake", color: "var(--dinner)" },
              { label: "Out (RMR + active)", color: "var(--breakfast)" },
              { label: "Calibrated maintenance", color: "var(--ink)" },
            ]} />
            <Foot>
              Bars are what you ate; the line is RMR plus measured active calories.
              Maintenance is fitted against your actual weight trend, so it absorbs
              whatever the prior misses. Any CI shown is statistical only — glycogen
              and water shifts are not in the model, so a tight interval is not the
              same as an accurate number.
            </Foot>
          </Card>
        );
      })()}

      {/* ── Blood markers (lab panels) ─────────────────────────────────────── */}
      <BloodMarkersCard />

      {/* Noise dose — a long-horizon baseline, so it sits with blood markers rather
          than anywhere near a daily view. */}
      <AudioExposure />
      </>)}

      {view === "nutrition" && (<>
      <MyFoodsCard />
      {/* ── Nutrition charts ──────────────────────────────────────────────── */}
      {hasNutrition && (
        <>
          {/* Calories and protein were two near-identical charts: same days, same
              meal stacking, same 7-day line, differing only in which nutrient the
              segments measured. One chart with a nutrient toggle says the same
              thing in half the height, and puts the two on equal footing so you
              can flip between them on the same dates rather than scrolling. */}
          {(() => {
            const isCal = nutrientMode === "calories";
            const suf = { calories: "cal", protein: "pro", fiber: "fib" }[nutrientMode] ?? "cal";
            const keys = MEALS.map((m) => `${m}_${suf}`);
            const totalOf = (r) => keys.reduce((a, k) => a + (r[k] ?? 0), 0);
            const totals = breakdownWithTrend.map(totalOf).filter((v) => v > 0);
            const mean = totals.length >= 3
              ? Math.round(totals.reduce((a, b) => a + b, 0) / totals.length) : null;
            // Calories are judged against calibrated maintenance when we have it,
            // otherwise the configured goal. Protein always has a goal, never a
            // "maintenance" — the concept doesn't apply.
            const goal = isCal ? (adaptiveTdee ?? calorieGoal)
              : suf === "fib" ? (nutConsistency?.fiber_goal ?? null) : proteinGoal;
            const goalLabel = isCal
              ? (adaptiveTdee != null ? "maintenance" : (calorieDynamic ? "maintenance" : "goal"))
              : "goal";
            const goalSource = isCal && adaptiveTdee != null
              ? "weight-calibrated" : isCal ? "configured" : null;
            return (
              <Card>
                <div className="flex items-baseline justify-between">
                  <p className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">
                    PER DAY · BY MEAL
                  </p>
                  <SegmentedTabs
                    className="!border-0 gap-4"
                    value={nutrientMode} onChange={setNutrientMode}
                    options={[{ value: "calories", label: "KCAL" }, { value: "protein", label: "PROTEIN" },
                              { value: "fiber", label: "FIBER" }]}
                  />
                </div>
                <Hero
                  value={mean != null ? mean.toLocaleString() : "—"}
                  unit={["MEAN / DAY", isCal ? "KCAL" : "GRAMS"]}
                />
                <HeroSub>
                  Over <K>{totals.length}</K> logged days
                  {goal && (
                    <> · {goalLabel} <K>{goal.toLocaleString()}</K>
                      {goalSource && <span className="text-ink-3"> ({goalSource})</span>}</>
                  )}
                  {mean != null && goal && (
                    <> · running <K>{mean > goal ? "+" : ""}{(mean - goal).toLocaleString()}</K> against it</>
                  )}
                </HeroSub>

                <div className="mt-3">
                  <ResponsiveContainer width="100%" height={160}>
                    <ComposedChart data={breakdownWithTrend} margin={{ top: 4, right: 8, bottom: 0, left: -8 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="var(--rule)" vertical={false} />
                      <XAxis dataKey="label" tick={{ fill: "var(--ink-3)", fontSize: 10 }} interval="preserveStartEnd" />
                      <YAxis tick={{ fill: "var(--ink-3)", fontSize: 10 }} />
                      <Tooltip
                        contentStyle={{ background: "var(--canvas)", border: "1px solid var(--rule)", borderRadius: 8, fontSize: 12 }}
                        labelStyle={{ color: "var(--ink-3)" }}
                        formatter={(v, n) => [v == null ? "—" : `${v}${isCal ? " kcal" : " g"}`, n]}
                      />
                      {/* Solid = the goal you set. Dashed = what you actually do.
                          Same convention as the Today nutrition bars. */}
                      {goal && <ReferenceLine y={goal} stroke="var(--ink-2)" strokeWidth={1.2} />}
                      {mean != null && (
                        <ReferenceLine y={mean} stroke="var(--ink)" strokeDasharray="4 3" strokeOpacity={0.75} />
                      )}
                      {MEALS.map((m, i) => (
                        <Bar key={m} dataKey={`${m}_${suf}`} name={MEAL_LABELS[m] ?? m}
                          stackId="a" fill={MEAL_COLORS[m]} isAnimationActive={false}
                          radius={i === MEALS.length - 1 ? [2, 2, 0, 0] : undefined} />
                      ))}
                      {/* 7-day rolling average over complete days — a gap where fewer
                          than 4 of the 7 were complete, rather than an invented value. */}
                      <Line dataKey={`${suf}_avg`} name="7-day average" stroke="var(--ink)"
                        strokeWidth={2} dot={false} isAnimationActive={false} />
                    </ComposedChart>
                  </ResponsiveContainer>
                </div>

                {/* The legend doubles as per-meal averages — it was a separate card. */}
                <Legend items={[...mealAverages.filter((m) => m.days > 0).map((m) => ({
                  label: MEAL_LABELS[m.meal] ?? m.meal,
                  color: m.color,
                  note: suf === "fib" ? null : isCal ? `~${m.avgCal}` : `~${m.avgPro}g`,
                })), { label: "7-day average", color: "var(--ink)" }]} />

                <Foot>
                  Stacked by meal, so a change in the total is attributable to a meal
                  rather than just visible. The tilde figures are that meal's average on
                  days it was logged. Solid line is the {goalLabel}
                  {goalSource === "weight-calibrated" &&
                    " — the same weight-calibrated figure the Energy Balance card reports, not a fixed number"}
                  ; dashed is your mean. The heavy line is a 7-day rolling average over
                  complete days only — today and partly-logged days are left out, since a
                  missing meal would read as a low day.
                </Foot>
              </Card>
            );
          })()}
        </>
      )}

      {/* ── Nutrition consistency ──────────────────────────────────────────
          Two goals, opposite directions: protein is a floor you want to clear,
          calories a ceiling you want to stay under. The old card drew both as
          plain lines in green and pink, which read as two equivalent series and
          left you working out which way was "good" for each. Latest values now
          lead, and the 80% target is the shared reference both are judged against. */}
      {!(nutConsistency?.data?.length > 0) && (
        // Used to vanish outright when there were no weeks (note 53).
        <Card>
          <Eyebrow left="CONSISTENCY · WEEKLY" right="% OF LOGGED DAYS" />
          <EmptyState query={consistencyQ} what="No complete logged days" window="the last 12 weeks" />
        </Card>
      )}
      {nutConsistency?.data?.length > 0 && (() => {
        const rows = nutConsistency.data;
        // Lead with the last COMPLETE week (note 54): early in the week the current
        // one has a day or two and swung the headline to 0% / 100%. It stays on the
        // chart, drawn hollow, and is named below the tiles.
        const complete = rows.filter((r) => !r.in_progress);
        const current = rows.find((r) => r.in_progress) ?? null;
        const last = complete[complete.length - 1] ?? {};
        const mean = (k) => {
          const v = complete.map((r) => r[k]).filter((x) => x != null);
          return v.length ? Math.round(v.reduce((a, b) => a + b, 0) / v.length) : null;
        };
        const pMean = mean("protein_pct"), cMean = mean("calorie_pct");
        const fMean = mean("fiber_pct");
        return (
          <Card>
            <Eyebrow left="CONSISTENCY · WEEKLY"
                     right={last.label ? `WEEK OF ${last.label.toUpperCase()}` : "% OF LOGGED DAYS"} />
            <div className="mt-2.5 flex items-stretch gap-3">
              {[
                { label: "PROTEIN", sub: `≥ ${nutConsistency.protein_goal ?? "—"}g`,
                  now: last.protein_pct, avg: pMean, color: "var(--rhr)" },
                { label: "CALORIES", sub: `≤ ${nutConsistency.calorie_goal?.toLocaleString() ?? "—"}`,
                  now: last.calorie_pct, avg: cMean, color: "var(--breakfast)" },
                // Fiber was computed by the endpoint all along but never plotted; it
                // lived as a tile in Behaviors instead. It belongs with the other two
                // goals, and it is the one furthest from target.
                { label: "FIBER", sub: `≥ ${nutConsistency.fiber_goal ?? "—"}g`,
                  now: last.fiber_pct, avg: fMean, color: "var(--food-plantprotein)" },
              ].map((m) => (
                <div key={m.label} className="flex-1">
                  <p className="text-[8.5px] tracking-micro text-ink-3">{m.label} {m.sub}</p>
                  <p className="text-[22px] font-semibold leading-none" style={{ color: m.color }}>
                    {m.now != null ? `${m.now}%` : "—"}
                  </p>
                  <p className="mt-0.5 text-[9.5px] leading-none text-ink-3">
                    {m.avg != null ? `${m.avg}% mean` : "—"}
                  </p>
                </div>
              ))}
            </div>

            <div className="mt-3">
              <ResponsiveContainer width="100%" height={140}>
                <ComposedChart data={rows} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--rule)" vertical={false} />
                  <XAxis dataKey="label" tick={{ fill: "var(--ink-3)", fontSize: 10 }} interval="preserveStartEnd" />
                  <YAxis tick={{ fill: "var(--ink-3)", fontSize: 10 }} domain={[0, 100]}
                    ticks={[0, 50, 80, 100]} tickFormatter={(v) => `${v}%`} width={36} interval={0} />
                  <Tooltip
                    contentStyle={{ background: "var(--canvas)", border: "1px solid var(--rule)", borderRadius: 8, fontSize: 12 }}
                    labelStyle={{ color: "var(--ink-3)" }}
                    formatter={(v, n) => [v != null ? `${v}%` : "—", n]}
                  />
                  <ReferenceLine y={80} stroke="var(--ink-2)" strokeDasharray="4 4" strokeOpacity={0.7} />
                  {[["protein_pct", "Protein", "var(--rhr)"], ["calorie_pct", "Calories", "var(--breakfast)"],
                    ["fiber_pct", "Fiber", "var(--food-plantprotein)"]].map(([k, n, col]) => (
                    <Line key={k} dataKey={k} name={n} stroke={col} strokeWidth={2} connectNulls isAnimationActive={false}
                      // The in-progress week is provisional — hollow, so it doesn't read as a result.
                      dot={({ cx, cy, payload, index }) => cx == null || cy == null ? null : payload?.in_progress
                        ? <circle key={index} cx={cx} cy={cy} r={3} fill="var(--card)" stroke={col} strokeWidth={1.5} />
                        : <circle key={index} cx={cx} cy={cy} r={2} fill={col} />} />
                  ))}
                </ComposedChart>
              </ResponsiveContainer>
            </div>

            {current && (
              <p className="mt-1 text-[9.5px] text-ink-3">
                This week (from {current.label}) is in progress — {current.logged} complete
                day{current.logged === 1 ? "" : "s"} so far, drawn hollow; today is not counted until it ends.
              </p>
            )}
            <Legend items={[
              { label: "Protein — a floor to clear", color: "var(--rhr)" },
              { label: "Calories — a ceiling to stay under", color: "var(--breakfast)" },
              { label: "Fiber — a floor to clear", color: "var(--food-plantprotein)" },
            ]} />
            <Foot>
              Percentage of days in each week that met the goal, counting only days with
              a complete log — a partly-logged day would otherwise read as a miss. The
              dashed line is 80%, i.e. hitting it four days in five.
            </Foot>
          </Card>
        );
      })()}

      {/* Diet composition, then the coverage tripwire. Composition answers "what is
          this made of" by calorie share; coverage answers "has a food group gone
          missing" by drift against your own baseline. Related but different
          questions, so they stay adjacent rather than merged. */}
      <FoodComposition days={120} weeks={16} />
      <FoodCoverage />
      </>)}
    </div>
  );
}
