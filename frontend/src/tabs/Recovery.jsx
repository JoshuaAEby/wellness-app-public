import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ComposedChart, Area, BarChart, Bar, Line, Scatter, XAxis, YAxis,
  CartesianGrid, Tooltip, ReferenceLine, ReferenceArea, ResponsiveContainer, Cell,
} from "recharts";
import { api } from "../api.js";
import { localMinutes } from "../lib/sleep.js";
import { windowDates, fmtDate, fmtMinutes, addDays } from "../utils.js";
import { EventFilterPills, computeHighlight, HighlightContrast } from "../components/EventHighlight.jsx";
import { glowDot, lastValidIndex, addBands, glowColorFor, isAlarming, TrendBadge, windowToTrendDays, interventionMarkers, ChartEventKey } from "../components/chartHelpers.jsx";
import TimeWindowSelector from "../components/TimeWindowSelector.jsx";
import BatteryResults from "../components/BatteryResults.jsx";
import HealthEvents from "../components/HealthEvents.jsx";
import MetricInfoModal, { METRIC_INFO } from "../components/MetricInfoModal.jsx";
import ReadinessDecomposition from "../components/ReadinessDecomposition.jsx";
import { Card, Eyebrow, Hero, HeroSub, K, Foot, Legend, SegmentedTabs } from "../components/ui.jsx";
import { useFlagRuns, flagBands } from "../components/FlagBands.jsx";

// ── Sleep helpers (moved from Body 2026-07-03) ───────────────────────────────
const DEEP_REM_PCT_TARGET = 40;
const SCHEDULE_TICKS = [0, 120, 240, 360, 480, 600, 720, 840];

function timeToScaleMins(isoStr, offsetMin) {
  if (!isoStr) return null;
  // Wall clock where the user was (tz-aware offset from /sleep), else the phone's zone.
  const totalMins = localMinutes(isoStr, offsetMin);
  if (totalMins == null) return null;
  return ((totalMins - 20 * 60 + 24 * 60) % (24 * 60));
}

function scaleMinsToLabel(scaleMins) {
  const totalMins = (scaleMins + 20 * 60) % (24 * 60);
  const h = Math.floor(totalMins / 60);
  const m = totalMins % 60;
  const h12 = h % 12 || 12;
  const ampm = h < 12 ? "AM" : "PM";
  return m === 0 ? `${h12} ${ampm}` : `${h12}:${String(m).padStart(2, "0")} ${ampm}`;
}

function fmtLocalTime(isoStr, offsetMin) {
  const mins = localMinutes(isoStr, offsetMin);
  if (mins == null) return "—";
  const h = Math.floor(mins / 60), m = mins % 60;
  return `${((h + 11) % 12) + 1}:${String(m).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`;
}

function SleepTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null;
  const d = payload[0]?.payload;
  return (
    <div className="rounded-lg border border-rule bg-canvas p-3 text-xs space-y-1">
      <p className="text-ink-2 mb-1">{label}</p>
      <p className="text-ink">Total: {fmtMinutes(d?.total_min)}</p>
      <p style={{ color: "var(--deep)" }}>Deep: {fmtMinutes(d?.deep_min)}</p>
      <p style={{ color: "#818cf8" }}>Core: {fmtMinutes(d?.core_min)}</p>
      <p style={{ color: "var(--rem)" }}>REM: {fmtMinutes(d?.rem_min)}</p>
      <p style={{ color: "var(--awake)" }}>Awake: {fmtMinutes(d?.awake_min)}</p>
    </div>
  );
}

function ScheduleTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null;
  const d = payload[0]?.payload;
  return (
    <div className="rounded-lg border border-rule bg-canvas p-3 text-xs space-y-1">
      <p className="text-ink-2 mb-1">{label}</p>
      <p className="text-indigo-300">Bedtime: {d?.bedtimeStr ?? "—"}</p>
      <p className="text-emerald-300">Wake: {d?.wakeStr ?? "—"}</p>
      <p className="text-ink-2">{fmtMinutes(d?.total_min)}</p>
    </div>
  );
}

function GoalBar({ current, target, label, unit = "", color = "#6366f1" }) {
  if (current == null || target == null) return null;
  const pct = Math.min(100, Math.round((current / target) * 100));
  const met = current >= target;
  return (
    <div>
      <div className="flex justify-between text-xs mb-1">
        <span className="text-ink-2">{label}</span>
        <span style={{ color: met ? "#10b981" : "#f59e0b" }}>
          {typeof current === "number" && !Number.isInteger(current) ? current.toFixed(0) : current}{unit}
          {" / "}
          <span className="text-ink-3">{target}{unit} goal</span>
        </span>
      </div>
      <div className="h-1.5 rounded-full bg-surface">
        <div className="h-1.5 rounded-full transition-all" style={{ width: `${pct}%`, backgroundColor: met ? "#10b981" : color }} />
      </div>
    </div>
  );
}

// Vital trend chart: shaded "typical range" band (if data carries `band`), an
// optional dashed baseline, the value line, and a glowing latest point.
function TrendChart({ data, valueKey, baselineKey, color, glowColor, changepoint, interventions, height = 160, unit = "", highlight = null }) {
  // Sick / travel bands sit UNDER the series. Without them a dip during illness looks
  // identical to a dip from training, and the reader has to remember which it was.
  const flagRuns = useFlagRuns({ rows: data, isoKey: "iso" });
  const lastIdx = lastValidIndex(data, valueKey);
  const gc = glowColor ?? color;
  const hasDanger = data.some((d) => d.danger != null);
  // Shade the post-changepoint region (Apple-style "highlighted trend region").
  let cpBand = null;
  if (changepoint) {
    const i = data.findIndex((d) => d.iso && d.iso >= changepoint.date);
    if (i >= 0) cpBand = { x1: data[i].date, x2: data[data.length - 1].date };
  }
  const fmt = (v, n) => Array.isArray(v)
    ? [`${v[0]}–${v[1]}${unit}`, n]
    : [v != null ? `${v}${unit}` : "—", n];
  return (
    <>
    <ResponsiveContainer width="100%" height={height}>
      <ComposedChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: -16 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--rule)" />
        {flagBands(flagRuns, data, { xKey: "date", isoKey: "iso" })}
        <XAxis dataKey="date" tick={{ fill: "var(--ink-3)", fontSize: 10 }} interval="preserveStartEnd" />
        <YAxis tick={{ fill: "var(--ink-3)", fontSize: 10 }} domain={["auto", "auto"]} />
        <Tooltip
          contentStyle={{ background: "var(--canvas)", border: "1px solid var(--rule)", borderRadius: 8, fontSize: 12 }}
          labelStyle={{ color: "var(--ink-3)" }}
          formatter={fmt}
        />
        {cpBand && (
          <ReferenceArea x1={cpBand.x1} x2={cpBand.x2} fill="#f59e0b" fillOpacity={0.08} ifOverflow="hidden"
            label={{ value: `step ${changepoint.shift}`, position: "insideTopLeft", fill: "#f59e0b", fontSize: 9 }} />
        )}
        {interventionMarkers(interventions, data, "iso", "date")}
        <Area dataKey="band" name="Typical" stroke="none" fill={color} fillOpacity={0.12}
          isAnimationActive={false} activeDot={false} connectNulls />
        {hasDanger && (
          <Line dataKey="danger" name="Alarm" stroke="var(--over)" strokeWidth={1.25}
            strokeDasharray="5 3" strokeOpacity={0.55} dot={false} isAnimationActive={false} connectNulls />
        )}
        {baselineKey && (
          <Line dataKey={baselineKey} name="Baseline" stroke={color} strokeWidth={1.5}
            strokeDasharray="4 4" strokeOpacity={0.5} dot={false} isAnimationActive={false} />
        )}
        {/* Behaviour highlight: the series fades to a ghost and the associated days stand
            out, with the two group means as reference lines (EventHighlight.jsx). */}
        <Line dataKey={valueKey} name="Value" stroke={color} strokeWidth={2}
          strokeOpacity={highlight?.active ? 0.22 : 1}
          dot={highlight?.active ? false : glowDot(gc, lastIdx)} activeDot={{ r: 4 }} isAnimationActive={false} />
        {highlight?.active && (
          <Scatter dataKey="_hl" name={highlight.label} fill={highlight.color} isAnimationActive={false} />
        )}
        {highlight?.active && highlight.subsetMean != null && (
          <ReferenceLine y={highlight.subsetMean} stroke={highlight.color} strokeDasharray="4 3" strokeOpacity={0.8} />
        )}
        {highlight?.active && highlight.otherMean != null && (
          <ReferenceLine y={highlight.otherMean} stroke="var(--ink-3)" strokeDasharray="4 3" strokeOpacity={0.6} />
        )}
      </ComposedChart>
    </ResponsiveContainer>
    <ChartEventKey interventions={interventions} runs={flagRuns} rows={data} isoKey="iso" />
    </>
  );
}

/**
 * Minimal sparkline for the small-multiples grid: the series shape and where the
 * latest point sits in it, nothing else. No axes, no grid, no band — at ~46 px tall
 * those would be noise, and the full banded chart is one tap away.
 */
function Sparkline({ data, color, bad, height = 34 }) {
  const vals = data.map(d => d.value).filter(v => v != null);
  if (vals.length < 2) return <div style={{ height }} />;
  const lo = Math.min(...vals), hi = Math.max(...vals), span = (hi - lo) || 1;
  const W = 100;
  const pts = [];
  data.forEach((d, i) => {
    if (d.value == null) return;
    pts.push([(i / (data.length - 1)) * W, height - ((d.value - lo) / span) * (height - 6) - 3]);
  });
  const path = "M " + pts.map(p => `${p[0].toFixed(2)} ${p[1].toFixed(2)}`).join(" L ");
  const last = pts[pts.length - 1];
  return (
    <svg viewBox={`0 0 ${W} ${height}`} preserveAspectRatio="none"
      className="block w-full" style={{ height }} aria-hidden="true">
      <path d={`${path} L ${W} ${height} L 0 ${height} Z`} fill={color} opacity="0.10" />
      <path d={path} fill="none" stroke={color} strokeWidth="1.4"
        vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
      <circle cx={last[0]} cy={last[1]} r="2" fill={bad ? "var(--over)" : color}
        vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function readinessColor(score) {
  if (score == null) return "#64748b";
  if (score >= 75) return "#10b981";
  if (score >= 50) return "#eab308";
  if (score >= 25) return "#f97316";
  return "#ef4444";
}

export default function Recovery({ window = "3M", onWindowChange, windowOffset = 0, onWindowOffsetChange }) {
  const [infoMetric, setInfoMetric] = useState(null);
  // Which small-multiple is expanded to its full banded chart (null = all collapsed).
  const [expandedVital, setExpandedVital] = useState(null);
  // Behaviour highlight on HRV + RHR (one choice drives both charts).
  const [hlType, setHlType] = useState(null);
  const { start, end } = windowDates(window, windowOffset);

  // From the day BEFORE the window: with the next-morning lag, an event on that day
  // lands on the window's first day.
  const { data: events = [] } = useQuery({
    queryKey: ["events", addDays(start, -1), end],
    queryFn: () => api.events(addDays(start, -1), end),
  });

  const { data: daily = [], isLoading: loadingDaily } = useQuery({
    queryKey: ["daily", start, end],
    queryFn: () => api.daily(start, end),
  });


  const { data: respRate = [] } = useQuery({
    queryKey: ["resp-rate", start, end],
    queryFn: () => api.rawTrend("respiratory_rate", start, end),
  });

  // Walking HR moved to Cardio 2026-11-04: it measures capacity, not daily state.

  const { data: sleepHrElev = [] } = useQuery({
    // Fixed-window early-night HR since 2026-10-28 (the thirds-based sleep_hr_elevation
    // scaled with night length; see sleep_processing.compute_sleep_hr_early_fixed).
    queryKey: ["raw-trend", "sleep_hr_early_fixed", start, end],
    queryFn: () => api.rawTrend("sleep_hr_early_fixed", start, end),
  });

  const { data: bloodOxygen = [] } = useQuery({
    queryKey: ["blood-oxygen", start, end],
    queryFn: () => api.rawTrend("blood_oxygen", start, end),
  });

  const { data: breathing = [] } = useQuery({
    queryKey: ["breathing-disturbances", start, end],
    queryFn: () => api.rawTrend("breathing_disturbances", start, end),
  });

  const { data: daylight = [] } = useQuery({
    queryKey: ["daylight", start, end],
    queryFn: () => api.rawTrend("time_in_daylight", start, end, "sum"),
  });

  const { data: settings } = useQuery({ queryKey: ["settings"], queryFn: api.getSettings });
  const { data: sleepSessions = [], isLoading: sleepLoading } = useQuery({
    queryKey: ["sleep", start, end],
    queryFn: () => api.sleep(start, end),
  });

  // Trend badges — same lookback as the visible window
  const { data: trendsData } = useQuery({
    queryKey: ["trends", windowToTrendDays(window)],
    queryFn: () => api.getTrends(windowToTrendDays(window)),
    staleTime: 1000 * 60 * 30,
  });
  const trend = Object.fromEntries((trendsData?.results ?? []).map(r => [r.key, r]));
  const { data: ivRows = [] } = useQuery({ queryKey: ["interventions"], queryFn: api.listInterventions });
  const { data: healthEvents = [] } = useQuery({ queryKey: ["healthEvents"], queryFn: api.getHealthEvents });
  // Health events draw as markers here only: these are the charts they might show up on.
  const interventions = [...ivRows, ...healthEvents.map((e) => (
    { id: `health-${e.id}`, date: e.date, label: e.name, notes: e.notes, category: "health" }))];

  // ── Sleep derived data (moved from Body 2026-07-03) ─────────────────────────
  const TARGET_MIN       = Math.round((settings?.target_sleep_hours ?? 7.5) * 60);
  const targetHoursLabel = (settings?.target_sleep_hours ?? 7.5) + "h";

  const sleepChartData = sleepSessions.map(s => ({
    date: fmtDate(s.date),
    total_min: s.total_min, deep_min: s.deep_min, core_min: s.core_min,
    rem_min: s.rem_min, awake_min: s.awake_min,
  }));

  const scheduleData = sleepSessions
    .filter(s => s.bedtime && s.wake_time)
    .map(s => {
      const bedMins = timeToScaleMins(s.bedtime, s.bed_offset_min);
      const wakeMins = timeToScaleMins(s.wake_time, s.wake_offset_min);
      if (bedMins == null || wakeMins == null) return null;
      const duration = wakeMins > bedMins ? wakeMins - bedMins : wakeMins + 24 * 60 - bedMins;
      return {
        date: fmtDate(s.date), total_min: s.total_min,
        bedtimeStr: fmtLocalTime(s.bedtime, s.bed_offset_min), wakeStr: fmtLocalTime(s.wake_time, s.wake_offset_min),
        offset: bedMins, sleep: duration,
      };
    })
    .filter(Boolean);

  const sleepN         = sleepSessions.length;
  const sleepAvg       = sleepN ? Math.round(sleepSessions.reduce((s, r) => s + (r.total_min ?? 0), 0) / sleepN) : null;
  const avgDeepRem     = sleepN ? Math.round(sleepSessions.reduce((s, r) => s + ((r.deep_min ?? 0) + (r.rem_min ?? 0)), 0) / sleepN) : null;
  const avgDeepRemPct    = sleepAvg ? Math.round((avgDeepRem / sleepAvg) * 100) : null;
  const deepRemTargetMin = sleepAvg ? Math.round(sleepAvg * (DEEP_REM_PCT_TARGET / 100)) : null;

  const BED_MAX_SCALE = 600, WAKE_MIN_SCALE = 480, WAKE_MAX_SCALE = 840;
  const consistencySessions = sleepSessions.filter(s => {
    if (!s.bedtime || !s.wake_time || (s.total_min ?? 0) < 210) return false;
    const bed = timeToScaleMins(s.bedtime, s.bed_offset_min), wake = timeToScaleMins(s.wake_time, s.wake_offset_min);
    return bed != null && bed <= BED_MAX_SCALE && wake != null && wake >= WAKE_MIN_SCALE && wake <= WAKE_MAX_SCALE;
  });
  const consistencyStats = (() => {
    const m = consistencySessions.length;
    if (m < 3) return null;
    const bedMins  = consistencySessions.map(s => timeToScaleMins(s.bedtime, s.bed_offset_min)).filter(v => v != null);
    const wakeMins = consistencySessions.map(s => timeToScaleMins(s.wake_time, s.wake_offset_min)).filter(v => v != null);
    if (bedMins.length < 3 || wakeMins.length < 3) return null;
    const mean = arr => arr.reduce((a, b) => a + b, 0) / arr.length;
    const std  = arr => { const mu = mean(arr); return Math.sqrt(arr.reduce((s, v) => s + (v - mu) ** 2, 0) / arr.length); };
    return { bedMean: mean(bedMins), bedStd: std(bedMins), wakeMean: mean(wakeMins), wakeStd: std(wakeMins), n: m };
  })();

  // Alarm-tied bands: HRV/RHR centered on the backend EWMA baseline so the danger line
  // sits at exactly z=∓1 (the recovery_alarm boundary). badDir = which side trips it.
  const hrvChart = addBands(daily.map(d => ({
    date: fmtDate(d.date), iso: d.date,
    value: d.hrv_value != null ? Math.round(d.hrv_value) : null,
    baseline: d.hrv_baseline != null ? Math.round(d.hrv_baseline) : null,
  })), "value", { badDir: "low", centerKey: "baseline" });
  const rhrChart = addBands(daily.map(d => ({
    date: fmtDate(d.date), iso: d.date,
    value: d.rhr_value != null ? Math.round(d.rhr_value) : null,
    baseline: d.rhr_baseline != null ? Math.round(d.rhr_baseline) : null,
  })), "value", { badDir: "high", centerKey: "baseline" });
  const hrvGlow = glowColorFor(hrvChart, "value", "low", "#10b981");
  const rhrGlow = glowColorFor(rhrChart, "value", "high", "#f43f5e");
  // Overnight-recovery metrics respond the MORNING AFTER a behaviour → lagDays=1.
  const hrvHl = computeHighlight({ events, activeType: hlType, lagDays: 1, data: hrvChart, valueKey: "value" });
  const rhrHl = computeHighlight({ events, activeType: hlType, lagDays: 1, data: rhrChart, valueKey: "value" });

  const readinessChart = daily.map(d => ({
    date: fmtDate(d.date), iso: d.date,
    score: d.readiness_score != null ? parseFloat(d.readiness_score.toFixed(1)) : null,
    label: d.readiness_label,
  }));

  // HRV COV — 7-day rolling coefficient of variation (std/mean × 100). Lower = more stable.
  const hrvCovData = daily.map((d, i) => {
    const slice = daily.slice(Math.max(0, i - 6), i + 1).map(x => x.hrv_value).filter(v => v != null);
    if (slice.length < 3) return { date: fmtDate(d.date), cov: null };
    const mean = slice.reduce((a, b) => a + b, 0) / slice.length;
    const variance = slice.reduce((a, b) => a + (b - mean) ** 2, 0) / slice.length;
    const cov = mean > 0 ? parseFloat((Math.sqrt(variance) / mean * 100).toFixed(1)) : null;
    return { date: fmtDate(d.date), iso: d.date, cov };
  });
  const covTrend = (() => {
    const vals = hrvCovData.map(d => d.cov).filter(v => v != null);
    if (vals.length < 8) return null;
    const recent = vals.slice(-7).reduce((a, b) => a + b, 0) / 7;
    const prior  = vals.slice(-14, -7).reduce((a, b) => a + b, 0) / 7;
    const delta  = recent - prior;
    if (Math.abs(delta) < 0.3) return { label: "Stable →", color: "#94a3b8" };
    return delta < 0 ? { label: "More Stable ↓", color: "#10b981" } : { label: "Less Stable ↑", color: "#f87171" };
  })();
  const covBanded = addBands(hrvCovData, "cov", { badDir: "high" });  // lower COV = more stable
  const covGlow = glowColorFor(covBanded, "cov", "high", "var(--rem)");


  // Resp rate: absolute alarm at +1.5 bpm above baseline (matches recovery_alarm).
  const respChart = addBands(respRate.map(r => ({
    date: fmtDate(r.date), iso: r.date,
    value: r.value != null ? parseFloat(r.value.toFixed(1)) : null,
  })), "value", { badDir: "high", absOffset: 1.5 });
  const respGlow = glowColorFor(respChart, "value", "high", "#38bdf8");

  // Early-night HR: mean HR over the first 2 h after sleep onset − the night's 10th-percentile HR.
  // The late-digestion / alcohol / arousal signature — higher = worse.
  const hrElevChart = addBands(sleepHrElev.map(r => ({
    date: fmtDate(r.date), iso: r.date,
    value: r.value != null ? parseFloat(r.value.toFixed(1)) : null,
  })), "value", { badDir: "high" });
  const hrElevGlow = glowColorFor(hrElevChart, "value", "high", "#f472b6");

  // SpO2 / breathing aren't in the calibrated alarm — use a ±1σ typical band, directional.
  const spo2Chart = addBands(bloodOxygen.map(r => ({
    date: fmtDate(r.date), iso: r.date,
    value: r.value != null ? parseFloat(r.value.toFixed(1)) : null,
  })), "value", { badDir: "low" });
  const spo2Glow = glowColorFor(spo2Chart, "value", "low", "#22d3ee");

  const breathChart = addBands(breathing.map(r => ({
    date: fmtDate(r.date), iso: r.date,
    value: r.value != null ? parseFloat(r.value.toFixed(1)) : null,
  })), "value", { badDir: "high" });
  const breathGlow = glowColorFor(breathChart, "value", "high", "var(--rem)");

  const daylightChart = daylight.map(r => ({ date: fmtDate(r.date), iso: r.date, value: r.value != null ? Math.round(r.value) : 0 }));
  const daylightAvg = daylightChart.length ? Math.round(daylightChart.reduce((s, r) => s + r.value, 0) / daylightChart.length) : 0;

  // Latest values for tiles
  const latest = daily[daily.length - 1];

  return (
    <div className="space-y-4 p-4">
      <TimeWindowSelector value={window} onChange={onWindowChange} offset={windowOffset} onOffsetChange={onWindowOffsetChange} />
      {/* Recovery score as the weighted sum it is. Replaces the two latest-value
          tiles plus the 0-100 bar chart: the tiles restated numbers the inputs
          below already carry, and a bar of the composite cannot say WHICH input
          moved it. Segment widths are effective weights, so a day with a missing
          component renders its real 45/70 split rather than a fictional 45/100. */}
      <Card>
        <div className="flex items-baseline justify-between">
          <p className="flex items-center gap-1 text-[9.5px] font-semibold tracking-eyebrow text-ink-3">
            RECOVERY · COMPOSITE
            <button onClick={() => setInfoMetric("readiness")}
              className="text-[10px] text-ink-3 hover:text-ink-2">ⓘ</button>
          </p>
          <TrendBadge trend={trend.readiness} />
        </div>
        <Hero
          value={latest?.readiness_score != null ? Math.round(latest.readiness_score) : "—"}
          unit={["OF 100", (latest?.readiness_label ?? "—").toUpperCase()]}
          color={readinessColor(latest?.readiness_score)}
        />
        {loadingDaily ? <p className="mt-3 text-[11px] text-ink-3">Loading…</p> : (
          <div className="mt-3">
            <ReadinessDecomposition
              onInfo={setInfoMetric}
              row={latest}
              history={readinessChart}
              readings={{
                hrv: latest?.hrv_value != null
                  ? `${Math.round(latest.hrv_value)} ms · base ${latest.hrv_baseline != null ? Math.round(latest.hrv_baseline) : "—"}`
                  : "",
                rhr: latest?.rhr_value != null
                  ? `${Math.round(latest.rhr_value)} bpm · base ${latest.rhr_baseline != null ? Math.round(latest.rhr_baseline) : "—"}`
                  : "",
                sleep: latest?.sleep_debt_hours != null
                  ? `${latest.sleep_debt_hours.toFixed(1)} h debt` : "",
              }}
            />
          </div>
        )}
        <Foot>
          Segment width is the input's weight in the score; the filled part is what it
          returned today. A full bar is 100. Training load is deliberately absent — it
          was removed from this score in July 2026 for measuring load state rather than
          recovery, and for pointing the wrong way in your own validation.
        </Foot>
      </Card>

      {/* HRV trend */}
      <div className="rounded-2xl border border-rule bg-card p-4">
        <div className="mb-1 flex items-center justify-between">
          <p className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">HRV</p>
          <TrendBadge trend={trend.hrv} />
        </div>
        <p className="mb-3 text-xs text-ink-3">Solid = daily · Shaded = typical range · <span className="text-red-400">Red = alarm threshold (z∓1)</span></p>
        {loadingDaily ? <p className="text-sm text-ink-2">Loading…</p> : (
          <>
            <EventFilterPills events={events} active={hlType} onChange={setHlType} lagNote="shown the next morning, on HRV and RHR" />
            <HighlightContrast hl={hrvHl} lagDays={1} fmt={(x) => `${Math.round(x)} ms`} goodHigh />
            <TrendChart data={hrvHl.data} valueKey="value" color="var(--rhr)" glowColor={hrvGlow} changepoint={trend.hrv?.changepoint} interventions={interventions} unit=" ms" highlight={hrvHl} />
          </>
        )}
      </div>

      {/* RHR trend */}
      <div className="rounded-2xl border border-rule bg-card p-4">
        <div className="mb-1 flex items-center justify-between">
          {/* Overnight construct since note 90 (2026-10-27): 10th-percentile HR in the sleep
              window, not Apple's daytime-estimated RHR — ~8 bpm lower by construction. */}
          <p className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">Overnight Resting HR</p>
          <TrendBadge trend={trend.rhr} />
        </div>
        <p className="mb-3 text-xs text-ink-3">Solid = daily · Shaded = typical range · <span className="text-red-400">Red = alarm threshold (z∓1)</span></p>
        {loadingDaily ? <p className="text-sm text-ink-2">Loading…</p> : (
          <>
            <HighlightContrast hl={rhrHl} lagDays={1} fmt={(x) => `${x.toFixed(1)} bpm`} goodHigh={false} />
            <TrendChart data={rhrHl.data} valueKey="value" color="var(--over)" glowColor={rhrGlow} changepoint={trend.rhr?.changepoint} interventions={interventions} unit=" bpm" highlight={rhrHl} />
          </>
        )}
      </div>

      {/* HRV Stability (COV) */}
      <div className="rounded-2xl border border-rule bg-card p-4">
        <div className="flex items-center justify-between mb-1">
          <p className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">HRV Stability (COV)</p>
          {covTrend && (
            <span className="text-xs font-semibold" style={{ color: covTrend.color }}>{covTrend.label}</span>
          )}
        </div>
        <p className="mb-3 text-xs text-ink-3">7-day rolling coefficient of variation · lower = more stable</p>
        {loadingDaily ? <p className="text-sm text-ink-2">Loading…</p> : (<>
          <ResponsiveContainer width="100%" height={150}>
            <ComposedChart data={covBanded} margin={{ top: 4, right: 8, bottom: 0, left: -16 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--rule)" />
              <XAxis dataKey="date" tick={{ fill: "var(--ink-3)", fontSize: 10 }} interval="preserveStartEnd" />
              <YAxis tick={{ fill: "var(--ink-3)", fontSize: 10 }} domain={["auto", "auto"]} />
              <Tooltip
                contentStyle={{ background: "var(--canvas)", border: "1px solid var(--rule)", borderRadius: 8, fontSize: 12 }}
                labelStyle={{ color: "var(--ink-3)" }}
                formatter={(v, n) => Array.isArray(v) ? [`${v[0]}–${v[1]}%`, n] : [v != null ? `${v}%` : "—", n]}
              />
              <Area dataKey="band" name="Typical" stroke="none" fill="var(--rem)" fillOpacity={0.12}
                isAnimationActive={false} activeDot={false} connectNulls />
              <Line dataKey="danger" name="High" stroke="var(--over)" strokeWidth={1.25}
                strokeDasharray="5 3" strokeOpacity={0.55} dot={false} isAnimationActive={false} connectNulls />
              {interventionMarkers(interventions, covBanded, "iso", "date")}
              <Line dataKey="cov" name="HRV COV" stroke="var(--rem)" strokeWidth={2}
                dot={glowDot(covGlow, lastValidIndex(covBanded, "cov"))} activeDot={{ r: 4 }} isAnimationActive={false} />
            </ComposedChart>
          </ResponsiveContainer>
          <ChartEventKey interventions={interventions} rows={covBanded} isoKey="iso" />
        </>)}
      </div>

      {/* Diagnostic vitals as SMALL MULTIPLES.
          These were four full-width cards — ~800 px of scroll for four series you
          check for exceptions, not for their day-to-day shape. Side by side they
          are comparable at a glance and an out-of-band day is obvious; the full
          banded chart is one tap away for whichever one looks off. */}
      {(respChart.length > 0 || hrElevChart.length > 0 || spo2Chart.length > 0 || breathChart.length > 0) && (
        <Card>
          <div className="flex items-baseline justify-between">
            <p className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">OVERNIGHT VITALS</p>
            <span className="text-[9px] tracking-micro text-ink-3">TAP TO EXPAND</span>
          </div>
          <div className="mt-2.5 grid grid-cols-2 gap-2">
            {[
              { key: "resp",   label: "RESP RATE",  data: respChart,   color: "var(--hrv)",  glow: respGlow,   unit: " brpm", badDir: "high", trend: trend.resp_rate,       cp: trend.resp_rate?.changepoint },
              { key: "hrelev", label: "EARLY-NIGHT HR", data: hrElevChart, color: "var(--rem)", glow: hrElevGlow, unit: " bpm", badDir: "high", trend: trend.sleep_hr_elev, cp: trend.sleep_hr_elev?.changepoint },
              { key: "spo2",   label: "BLOOD O₂",   data: spo2Chart,   color: "var(--drinks)", glow: spo2Glow, unit: "%",     badDir: "low",  trend: trend.blood_oxygen,    cp: trend.blood_oxygen?.changepoint },
              { key: "breath", label: "BREATHING",  data: breathChart, color: "var(--slp)",  glow: breathGlow, unit: "",      badDir: "high", trend: trend.breathing,       cp: trend.breathing?.changepoint },
            ].filter(v => v.data.length > 0).map((v) => {
              const i = lastValidIndex(v.data, "value");
              const val = i >= 0 ? v.data[i].value : null;
              const danger = i >= 0 ? v.data[i].danger : null;
              // Red only when the latest reading is on the alarm side — the same
              // rule the composite recovery alarm uses, so the two never disagree.
              const bad = isAlarming(v.data, "value", v.badDir);
              const open = expandedVital === v.key;
              return (
                <button key={v.key} onClick={() => setExpandedVital(open ? null : v.key)}
                  className={`rounded-lg bg-surface p-2 text-left transition-colors ${open ? "col-span-2 ring-1 ring-inset ring-ink-3" : ""}`}>
                  <div className="flex items-baseline justify-between gap-1">
                    <span className="truncate text-[8.5px] tracking-micro text-ink-3">{v.label}</span>
                    <TrendBadge trend={v.trend} />
                  </div>
                  <div className="mt-0.5 flex items-baseline gap-1">
                    <span className="text-[15px] font-semibold leading-none"
                      style={{ color: bad ? "var(--over)" : "var(--ink)" }}>
                      {val != null ? val : "—"}
                    </span>
                    <span className="text-[9px] text-ink-3">{v.unit.trim()}</span>
                  </div>
                  <div className="mt-1">
                    {open
                      ? <TrendChart data={v.data} valueKey="value" color={v.color} glowColor={v.glow}
                          changepoint={v.cp} interventions={interventions} unit={v.unit} height={150} />
                      : <Sparkline data={v.data} color={v.color} bad={bad} />}
                  </div>
                  {open && danger != null && (
                    <p className="mt-1 text-[9px] text-ink-3">
                      Alarm threshold {danger}{v.unit} · shaded band is the 30-day typical range
                    </p>
                  )}
                </button>
              );
            })}
          </div>
        </Card>
      )}

      {/* Daylight — circadian lever (feeds sleep/mood/HRV correlations) */}
      {daylightChart.length > 0 && (
        <div className="rounded-2xl border border-rule bg-card p-4">
          <div className="mb-1 flex items-center justify-between">
            <p className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">Daylight</p>
            <div className="flex items-center gap-2">
              <span className="text-xs text-ink-2">avg {daylightAvg} min/day</span>
              <TrendBadge trend={trend.time_in_daylight} />
            </div>
          </div>
          <p className="mb-3 text-xs text-ink-3">Total time in daylight · more light → better circadian rhythm, sleep &amp; mood</p>
          <ResponsiveContainer width="100%" height={130}>
            <ComposedChart data={daylightChart} margin={{ top: 4, right: 8, bottom: 0, left: -16 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--rule)" vertical={false} />
              <XAxis dataKey="date" tick={{ fill: "var(--ink-3)", fontSize: 10 }} interval="preserveStartEnd" />
              <YAxis tick={{ fill: "var(--ink-3)", fontSize: 10 }} tickFormatter={v => `${Math.round(v / 60)}h`} />
              <Tooltip
                contentStyle={{ background: "var(--canvas)", border: "1px solid var(--rule)", borderRadius: 8, fontSize: 12 }}
                labelStyle={{ color: "var(--ink-3)" }}
                formatter={(v) => [`${v} min`, "Daylight"]}
              />
              {interventionMarkers(interventions, daylightChart, "iso", "date")}
              <Bar dataKey="value" name="Daylight" fill="var(--breakfast)" radius={[2, 2, 0, 0]} isAnimationActive={false} />
            </ComposedChart>
          </ResponsiveContainer>
          <ChartEventKey interventions={interventions} rows={daylightChart} isoKey="iso" />
        </div>
      )}

      {/* Today's cognitive battery. Belongs on this tab — all three tests are
          readiness readouts and sit beside the measures they'll be correlated
          against — but NOT at the top: it is a once-a-day result, whereas the
          recovery score is the headline this tab exists to deliver. Moved down
          2026-08-15. Self-hiding until something has been recorded today. */}
      <BatteryResults />

      <HealthEvents />

      {/* Sleep moved to its own view (tabs/Sleep.jsx) 2026-08-15 — four sleep
          sections on top of ten vitals sections made this tab long enough that the
          sleep half was never read, and schedule/consistency read far better as
          distributions on a shared clock axis than as two more time series. */}

      <MetricInfoModal metric={METRIC_INFO[infoMetric] ?? infoMetric} onClose={() => setInfoMetric(null)} />
    </div>
  );
}
