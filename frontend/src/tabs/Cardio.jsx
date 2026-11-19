import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ComposedChart, Bar, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, ReferenceLine, ReferenceArea, ResponsiveContainer,
} from "recharts";
import { api } from "../api.js";
import { windowDates, fmtDate } from "../utils.js";
import { glowDot, lastValidIndex, trendGlowColor, TrendBadge, windowToTrendDays, interventionMarkers, ChartEventKey } from "../components/chartHelpers.jsx";
import TimeWindowSelector from "../components/TimeWindowSelector.jsx";
import TrainingLoadChart from "../components/TrainingLoadChart.jsx";
import HRRecoveryCard from "../components/HRRecoveryCard.jsx";
import ViewVerdict from "../components/ViewVerdict.jsx";
import WorkoutHistory from "../components/WorkoutHistory.jsx";
import { ACWR_HIGH, ACWR_LABEL, ACWR_LOW, acwrBand } from "../lib/acwr.js";
import { Card, Hero, HeroSub, K, Foot, Legend, SectionHeader, SegmentedTabs, EmptyState } from "../components/ui.jsx";
import MetricInfoModal, { METRIC_INFO } from "../components/MetricInfoModal.jsx";

// ── Helpers ───────────────────────────────────────────────────────────────────

// Normal is deliberately UNCOLOURED ink: "fine" is not a state that should compete for
// attention. Only high load takes a hue; a light week is fine too (lib/acwr.js, 2026-11-04).
function acwrColor(acwr) {
  const b = acwrBand(acwr);
  if (b == null) return "var(--ink-3)";
  return b === "high" || b === "very_high" ? "var(--over)" : "var(--ink)";
}

function acwrStatus(acwr) {
  const b = acwrBand(acwr);
  return { label: b ? ACWR_LABEL[b] : "no data" };
}
function CustomTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border border-rule bg-canvas p-3 text-xs space-y-1">
      <p className="text-ink-2 mb-1">{label}</p>
      {payload.filter((p) => p.dataKey !== "band").map((p) => (
        <p key={p.dataKey} style={{ color: p.color }}>
          {p.name}: {p.value != null ? Number(p.value).toFixed(1) : "—"}
        </p>
      ))}
    </div>
  );
}

// ── Main ──────────────────────────────────────────────────────────────────────

// Load lives here, not with lifting: TRIMP is heart-rate based and barely registers strength work.
export default function Cardio({ window = "3M", onWindowChange, windowOffset = 0, onWindowOffsetChange }) {
  const [infoMetric, setInfoMetric] = useState(null);
  const [zoneDays, setZoneDays] = useState(30);
  const { start, end } = windowDates(window, windowOffset);

  const dailyQ = useQuery({
    queryKey: ["daily", start, end],
    queryFn: () => api.daily(start, end),
  });
  const { data: daily = [], isLoading: loadingDaily } = dailyQ;

  // Walking HR moved here from Recovery (2026-11-04): it is the capacity measure the Cardio
  // headline rests on. Same query key as Recovery used, so the cache carries over.
  const { data: walkingHr = [] } = useQuery({
    queryKey: ["walking-hr", start, end],
    queryFn: () => api.rawTrend("walking_heart_rate_avg", start, end),
  });

  const { data: vo2Max = [] } = useQuery({
    queryKey: ["vo2max", start, end],
    queryFn: () => api.rawTrend("vo2_max", start, end),
  });

  const { data: trendsData } = useQuery({
    queryKey: ["trends", windowToTrendDays(window)],
    queryFn: () => api.getTrends(windowToTrendDays(window)),
    staleTime: 1000 * 60 * 30,
  });

  const { data: flights = [] } = useQuery({
    queryKey: ["flights", start, end],
    queryFn: () => api.rawTrend("flights_climbed", start, end, "sum"),
  });


  const { data: trainingZones } = useQuery({
    queryKey: ["training-zones", zoneDays],
    queryFn: () => api.getTrainingZones(zoneDays),
    staleTime: 1000 * 60 * 15,
  });

  // ── Derived chart data ─────────────────────────────────────────────────────

  // ATL/CTL lines + the ACWR normal range drawn AROUND CTL: [0.8·CTL, 2.0·CTL] (lib/acwr.js).
  // When the ATL line sits inside the band, ACWR is in the normal range;
  // ATL above the band = high load, below = a light week. This makes the ratio
  // legible on the load chart itself (replaces the old separate ACWR chart).
  const loadChartData = daily.map(d => ({
    date: fmtDate(d.date), iso: d.date,
    atl:  d.atl != null ? parseFloat(d.atl.toFixed(1)) : null,
    ctl:  d.ctl != null ? parseFloat(d.ctl.toFixed(1)) : null,
    band: d.ctl != null ? [parseFloat((ACWR_LOW * d.ctl).toFixed(1)), parseFloat((ACWR_HIGH * d.ctl).toFixed(1))] : null,
  }));

  // Ratio-space series for AcwrChart: the ratio itself plus the daily load rail.
  const acwrSeries = daily.map(d => ({
    label: fmtDate(d.date), iso: d.date,
    acwr: d.acwr != null ? d.acwr : null,
    atl: d.atl != null ? d.atl : null,
    ctl: d.ctl != null ? d.ctl : null,
    load: d.daily_load ?? 0,
  }));
  const { data: forecast } = useQuery({
    queryKey: ["acwr-forecast", 7],
    queryFn: () => api.getAcwrForecast(7),
    staleTime: 1000 * 60 * 30,
  });

  const walkChart = walkingHr.map(r => ({ date: fmtDate(r.date), iso: r.date, value: r.value != null ? Math.round(r.value) : null }));
  const vo2Chart = vo2Max.map(r => ({ date: fmtDate(r.date), iso: r.date, value: r.value != null ? parseFloat(r.value.toFixed(1)) : null }));
  const trend = Object.fromEntries((trendsData?.results ?? []).map(r => [r.key, r]));

  const flightsChart = flights.map(r => ({ date: fmtDate(r.date), value: r.value != null ? Math.round(r.value) : 0 }));
  const flightsTotal = flightsChart.reduce((s, r) => s + r.value, 0);
  const { data: interventions = [] } = useQuery({ queryKey: ["interventions"], queryFn: api.listInterventions });

  const latest = daily[daily.length - 1];

  return (
    <div className="space-y-4 p-4">
      <ViewVerdict view="cardio" right="VS LAST YEAR" />

      <TimeWindowSelector value={window} onChange={onWindowChange} offset={windowOffset} onOffsetChange={onWindowOffsetChange} />

      {/* ── Training Load ──────────────────────────────────────────────────── */}
      <SectionHeader label="Training Load" />

      {/* Training load in LOAD UNITS: CTL (fitness), ATL (fatigue), and the ACWR
          target as a band around CTL. Plotting the ratio alone divides CTL out —
          i.e. hides progress — so the ratio lives in the readout, not the y-axis.
          The three summary tiles that used to sit above this were removed as
          redundant; their info modals are the tappable stats below. */}
      <Card>
        <div className="flex items-baseline justify-between">
          <p className="flex items-center gap-1 text-[9.5px] font-semibold tracking-eyebrow text-ink-3">
            TRAINING LOAD · TRIMP
            <button onClick={() => setInfoMetric(METRIC_INFO.trimp)}
              className="text-[10px] text-ink-3 hover:text-ink-2">ⓘ</button>
          </p>
          <span className="text-[9.5px] tracking-eyebrow text-ink-3">
            {forecast?.available ? `${daily.length}D + ${forecast.horizon} AHEAD` : `${daily.length}D`}
          </span>
        </div>

        {/* The three former tiles, as one tappable stat row. Each still opens its
            info modal — that was the part worth keeping. */}
        <div className="mt-2.5 flex items-stretch gap-2">
          {[
            { key: "ctl",  label: "FITNESS",  sub: "CTL · 42d", value: latest?.ctl?.toFixed(1),  color: "var(--ink-2)" },
            { key: "atl",  label: "FATIGUE",  sub: "ATL · 7d",  value: latest?.atl?.toFixed(1),  color: "var(--hrv)" },
            { key: "acwr", label: "RATIO",    sub: acwrStatus(latest?.acwr).label, value: latest?.acwr?.toFixed(2), color: acwrColor(latest?.acwr) },
          ].map((t) => (
            <button key={t.key} onClick={() => setInfoMetric(METRIC_INFO[t.key])}
              className="flex-1 rounded-md bg-surface px-2 py-1.5 text-left">
              <span className="flex items-baseline gap-1">
                <span className="text-[8.5px] tracking-micro text-ink-3">{t.label}</span>
                <span className="text-[8px] text-ink-3">ⓘ</span>
              </span>
              <span className="block text-[17px] font-semibold leading-tight" style={{ color: t.color }}>
                {t.value ?? "—"}
              </span>
              <span className="block text-[8.5px] leading-none text-ink-3">{t.sub}</span>
            </button>
          ))}
        </div>

        <div className="mt-3">
          {/* The chart returns null on empty history, which left a silent hole in a
              card whose tiles still read "—" (note 53). Say which it is instead. */}
          {loadingDaily
            ? <p className="text-[11px] text-ink-3">Loading…</p>
            : acwrSeries.length
              ? <TrainingLoadChart history={acwrSeries} forecast={forecast} />
              : <EmptyState query={dailyQ} what="No daily metrics" window={`${start} → ${end}`} />}
        </div>

        <Legend className="mt-2" items={[
          { label: "CTL fitness", color: "var(--ink-2)" },
          { label: "ATL fatigue", color: "var(--hrv)" },
          { label: `${ACWR_LOW}–${ACWR_HIGH.toFixed(1)}× CTL normal range`, color: "var(--rhr)" },
        ]} />

        {/* Where the next week lands under the two choices that BOUND it, and —
            the question you actually face each morning — where ONE session vs a
            rest day puts tomorrow (day 1 of the same two scenarios; note 49). */}
        {forecast?.available && (
          <div className="mt-3 space-y-1.5 border-t border-rule pt-3">
            {forecast.scenarios.train?.points?.[0] && forecast.scenarios.rest?.points?.[0] && (() => {
              const t1 = forecast.scenarios.train.points[0], r1 = forecast.scenarios.rest.points[0];
              return (
                <div className="mb-2 flex items-baseline gap-2 text-[11px]">
                  <span className="text-[9px] tracking-micro text-ink-3">TOMORROW</span>
                  <span className="ml-auto text-[10px] text-ink-3">
                    one session → ratio <span className="font-semibold" style={{ color: acwrColor(t1.acwr) }}>
                      {t1.acwr?.toFixed(2)}</span>
                    {" · "}rest → <span className="font-semibold" style={{ color: acwrColor(r1.acwr) }}>
                      {r1.acwr?.toFixed(2)}</span>
                  </span>
                </div>
              );
            })()}
            <p className="text-[9px] tracking-micro text-ink-3">
              IN {forecast.horizon} DAYS, IF YOU…
            </p>
            {[
              { k: "train", label: `train every day`, color: "var(--hrv)" },
              { k: "rest",  label: "never train",     color: "var(--under)" },
            ].map(({ k, label, color }) => {
              const sc = forecast.scenarios[k];
              if (!sc) return null;
              return (
                <div key={k} className="flex items-baseline gap-2 text-[11px]">
                  <span className="h-[9px] w-[9px] flex-none translate-y-[1px] rounded-[2px]" style={{ background: color }} />
                  <span className="prose-ui w-[92px] flex-none text-ink-2">{label}</span>
                  <span className="ml-auto text-[10px] text-ink-3">
                    fitness <span className="font-semibold text-ink">{sc.end_ctl?.toFixed(0)}</span>
                    {" · "}fatigue <span className="font-semibold text-ink">{sc.end_atl?.toFixed(0)}</span>
                    {" · "}ratio <span className="font-semibold" style={{ color: acwrColor(sc.end_acwr) }}>
                      {sc.end_acwr?.toFixed(2)}
                    </span>
                  </span>
                </div>
              );
            })}
            <p className="pt-0.5 text-[9.5px] leading-snug text-ink-3">
              A typical training session for you is {Math.round(forecast.typical_workout_load)} TRIMP
              (mean of your last {forecast.typical_n_sessions} sessions, walks excluded). The top
              line is one of those every day — a reference, not a ceiling: harder sessions land
              above it.
            </p>
          </div>
        )}
      </Card>

      {/* HR recovery sits next to load on purpose: load says how much you did, this
          says how well you absorbed it, and it is the fitness signal that survives
          VO2max being unreliable here. */}
      <HRRecoveryCard />
      {/* ── Training Load Focus ────────────────────────────────────────────── */}
      <SectionHeader label="Training Load Focus" />

      <Card>
        <div className="flex items-baseline justify-between">
          <p className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">ZONE DISTRIBUTION</p>
          <SegmentedTabs
            className="!border-0 gap-4"
            value={zoneDays} onChange={setZoneDays}
            options={[{ value: 30, label: "30D" }, { value: 60, label: "60D" }, { value: 90, label: "90D" }]}
          />
        </div>

        {!trainingZones || trainingZones.total_min === 0 ? (
          <p className="text-sm text-ink-3">No HR data in workouts for this period.</p>
        ) : (
          <>
            {/* Stacked bar */}
            <div className="flex h-3 rounded-full overflow-hidden mb-4">
              {[...trainingZones.zones].reverse().map(z => (
                z.pct > 0 && <div key={z.label} style={{ width: `${z.pct}%`, backgroundColor: z.color }} />
              ))}
            </div>
            {/* Zone rows */}
            <div className="space-y-2.5">
              {trainingZones.zones.map(z => (
                <div key={z.label} className="flex items-center gap-3">
                  <span className="text-xs font-medium w-24" style={{ color: z.color }}>{z.label}</span>
                  <div className="flex-1 h-2 rounded-full bg-surface">
                    <div className="h-full rounded-full" style={{ width: `${z.pct}%`, backgroundColor: z.color }} />
                  </div>
                  <span className="text-xs text-ink-2 w-8 text-right">{z.pct}%</span>
                  <span className="text-xs text-ink-3 w-12 text-right">{z.min}m</span>
                </div>
              ))}
            </div>
            {/* Focus label */}
            {trainingZones.focus && (
              <div className="mt-4 rounded-lg bg-surface px-3 py-2.5">
                <p className="text-sm font-semibold text-ink">{trainingZones.focus}</p>
                <p className="text-xs text-ink-2 mt-0.5">{trainingZones.focus_description}</p>
              </div>
            )}
          </>
        )}
      </Card>

      {/* ── Training Adaptation ────────────────────────────────────────────── */}
      <SectionHeader label="Training Adaptation" />

      {/* Walking heart rate — the cardio marker with the most data (one value a day). */}
      {walkChart.length > 0 && (
        <Card>
          <div className="flex items-start justify-between">
            <p className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">WALKING HEART RATE</p>
            <TrendBadge trend={trend.walking_hr} />
          </div>
          <Hero value={walkChart[walkChart.length - 1]?.value ?? "—"} unit={["BPM", "DAILY AVERAGE"]} />
          <HeroSub>
            Your average heart rate while walking. Lower means the same walking costs your heart less,
            which is what fitter looks like. Single days swing with pace, hills and heat, so read the trend.
          </HeroSub>
          <ResponsiveContainer width="100%" height={160}>
            <ComposedChart data={walkChart} margin={{ top: 4, right: 8, bottom: 0, left: -16 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--rule)" />
              <XAxis dataKey="date" tick={{ fill: "var(--ink-3)", fontSize: 10 }} interval="preserveStartEnd" />
              <YAxis tick={{ fill: "var(--ink-3)", fontSize: 10 }} domain={["auto", "auto"]} />
              <Tooltip
                contentStyle={{ background: "var(--canvas)", border: "1px solid var(--rule)", borderRadius: 8, fontSize: 12 }}
                labelStyle={{ color: "var(--ink-3)" }}
                formatter={(v) => [`${v} bpm`, "Walking HR"]}
              />
              {(() => {
                const cp = trend.walking_hr?.changepoint;
                if (!cp) return null;
                const i = walkChart.findIndex(d => d.iso && d.iso >= cp.date);
                if (i < 0) return null;
                return <ReferenceArea x1={walkChart[i].date} x2={walkChart[walkChart.length - 1].date} fill="#f59e0b" fillOpacity={0.08} ifOverflow="hidden"
                  label={{ value: `step ${cp.shift}`, position: "insideTopLeft", fill: "#f59e0b", fontSize: 9 }} />;
              })()}
              {interventionMarkers(interventions, walkChart, "iso", "date")}
              <Line dataKey="value" name="Walking HR" stroke="var(--breakfast)" strokeWidth={2}
                dot={glowDot(trendGlowColor(trend.walking_hr, "var(--breakfast)"), lastValidIndex(walkChart, "value"))} activeDot={{ r: 4 }} isAnimationActive={false} />
            </ComposedChart>
          </ResponsiveContainer>
          <ChartEventKey interventions={interventions} rows={walkChart} isoKey="iso" />
          <Foot>Apple&apos;s daily walking average. Insights → Progress compares it with the same dates last year.</Foot>
        </Card>
      )}

      {/* VO2 Max */}
      {vo2Chart.length > 0 && (
        <Card>
          <div className="flex items-start justify-between">
            <p className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">VO₂ MAX</p>
            <div className="flex items-center gap-2">
              <TrendBadge trend={trend.vo2_max} />
              <button className="text-[10px] text-ink-3 hover:text-ink-2"
                onClick={() => setInfoMetric(METRIC_INFO.vo2max)}>ⓘ</button>
            </div>
          </div>
          <Hero
            value={vo2Chart[vo2Chart.length - 1]?.value ?? "—"}
            unit={["ML/KG/MIN", `${vo2Chart.length} READING${vo2Chart.length === 1 ? "" : "S"}`]}
          />
          {(() => {
            // Staleness is the story here more often than the value. Apple only
            // estimates VO2max from outdoor walk/run/hike with continuous HR, so a
            // gap means "no qualifying session", NOT "fitness unchanged" — and a
            // flat-looking line across a long gap would imply the latter.
            const lastIso = vo2Chart[vo2Chart.length - 1]?.iso;
            const days = lastIso
              ? Math.round((Date.now() - new Date(`${lastIso}T12:00:00`)) / 86400000) : null;
            if (days == null) return null;
            return (
              <HeroSub>
                {days <= 14
                  ? <>Last reading <K>{days} days</K> ago.</>
                  : <>Last reading <K>{days} days</K> ago — Apple only estimates this from an
                     outdoor walk, run or hike with continuous heart rate, so a gap means no
                     qualifying session, not unchanged fitness.</>}
              </HeroSub>
            );
          })()}
          <ResponsiveContainer width="100%" height={160}>
            <ComposedChart data={vo2Chart} margin={{ top: 4, right: 8, bottom: 0, left: -16 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--rule)" />
              <XAxis dataKey="date" tick={{ fill: "var(--ink-3)", fontSize: 10 }} interval="preserveStartEnd" />
              <YAxis tick={{ fill: "var(--ink-3)", fontSize: 10 }} domain={["auto", "auto"]} />
              <Tooltip
                contentStyle={{ background: "var(--canvas)", border: "1px solid var(--rule)", borderRadius: 8, fontSize: 12 }}
                labelStyle={{ color: "var(--ink-3)" }}
                formatter={(v) => [`${v} ml/kg/min`, "VO₂ Max"]}
              />
              {(() => {
                const cp = trend.vo2_max?.changepoint;
                if (!cp) return null;
                const i = vo2Chart.findIndex(d => d.iso && d.iso >= cp.date);
                if (i < 0) return null;
                return <ReferenceArea x1={vo2Chart[i].date} x2={vo2Chart[vo2Chart.length - 1].date} fill="#f59e0b" fillOpacity={0.08} ifOverflow="hidden"
                  label={{ value: `step ${cp.shift}`, position: "insideTopLeft", fill: "#f59e0b", fontSize: 9 }} />;
              })()}
              <ReferenceLine y={55} stroke="#10b981" strokeDasharray="3 3" strokeOpacity={0.5}
                label={{ value: "Excellent (55)", position: "insideTopRight", fill: "#10b981", fontSize: 9 }} />
              <ReferenceLine y={40} stroke="#eab308" strokeDasharray="3 3" strokeOpacity={0.5}
                label={{ value: "Average (40)", position: "insideTopRight", fill: "#eab308", fontSize: 9 }} />
              {interventionMarkers(interventions, vo2Chart, "iso", "date")}
              <Line dataKey="value" name="VO₂ Max" stroke="var(--rem)" strokeWidth={2}
                dot={glowDot(trendGlowColor(trend.vo2_max, "var(--rem)"), lastValidIndex(vo2Chart, "value"))} activeDot={{ r: 4 }} isAnimationActive={false} />
            </ComposedChart>
          </ResponsiveContainer>
          <ChartEventKey interventions={interventions} rows={vo2Chart} isoKey="iso" />
          <Foot>Reference lines are general adult benchmarks; actual ranges vary by age and sex.</Foot>
        </Card>
      )}

      {/* Flights climbed — light activity context */}
      {flightsTotal > 0 && (
        <div className="rounded-2xl border border-rule bg-card p-4">
          <div className="mb-3 flex items-center justify-between">
            <p className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">Flights Climbed</p>
            <span className="text-xs text-ink-2">{flightsTotal.toLocaleString()} total · {Math.round(flightsTotal / Math.max(1, flightsChart.length))}/day</span>
          </div>
          <ResponsiveContainer width="100%" height={110}>
            <ComposedChart data={flightsChart} margin={{ top: 4, right: 8, bottom: 0, left: -16 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--rule)" vertical={false} />
              <XAxis dataKey="date" tick={{ fill: "var(--ink-3)", fontSize: 10 }} interval="preserveStartEnd" />
              <YAxis tick={{ fill: "var(--ink-3)", fontSize: 10 }} allowDecimals={false} />
              <Tooltip
                contentStyle={{ background: "var(--canvas)", border: "1px solid var(--rule)", borderRadius: 8, fontSize: 12 }}
                labelStyle={{ color: "var(--ink-3)" }}
                formatter={(v) => [`${v} flights`, "Climbed"]}
              />
              <Bar dataKey="value" name="Flights" fill="var(--drinks)" radius={[2, 2, 0, 0]} isAnimationActive={false} />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}
      <WorkoutHistory start={start} defaultFilter="cardio" />

      {infoMetric && (
        <MetricInfoModal metric={METRIC_INFO[infoMetric] ?? infoMetric} onClose={() => setInfoMetric(null)} />
      )}
    </div>
  );
}
