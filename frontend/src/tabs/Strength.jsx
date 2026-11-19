import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ComposedChart, Bar, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, ReferenceLine, ResponsiveContainer,
} from "recharts";
import { api } from "../api.js";
import { windowDates, fmtDate } from "../utils.js";
import { windowToTrendDays, interventionMarkers, ChartEventKey } from "../components/chartHelpers.jsx";
import TimeWindowSelector from "../components/TimeWindowSelector.jsx";
import RepBests from "../components/RepBests.jsx";
import ViewVerdict from "../components/ViewVerdict.jsx";
import WorkoutHistory from "../components/WorkoutHistory.jsx";
import { Card, Eyebrow, Hero, HeroSub, K, Sep, Foot, SegmentedTabs } from "../components/ui.jsx";
import MetricInfoModal, { METRIC_INFO } from "../components/MetricInfoModal.jsx";

// ── Strength (Hevy) helpers ───────────────────────────────────────────────────

const KG_TO_LB = 2.20462;

const MUSCLE_COLORS = [
  "#818cf8", "#f472b6", "#34d399", "#fbbf24", "#38bdf8",
  "#fb923c", "#a78bfa", "#4ade80", "#f87171", "#22d3ee",
];

function setsBandColor(avg, band) {
  if (!avg) return "#475569";
  if (avg < band.low) return "#f59e0b";
  if (avg > band.high) return "#ef4444";
  return "#22c55e";
}

// What the lifting is meant to change. Scans stay in Body; this is the one line of them that
// belongs beside the loads. Same noise floor as the Body card (SCAN_NOISE.smm_lb).
const MUSCLE_NOISE_LB = 1;

function MuscleMass() {
  const { data: scans = [] } = useQuery({ queryKey: ["scans"], queryFn: api.getScans });
  const rows = scans.filter((s) => s.smm_lb != null);       // newest first
  if (!rows.length) return null;
  const last = rows[0], first = rows[rows.length - 1];
  const change = last.smm_lb - first.smm_lb;
  return (
    <Card>
      <Eyebrow left="MUSCLE MASS" right="INBODY" />
      <HeroSub>
        {rows.length < 2
          ? <>Skeletal muscle <K>{last.smm_lb.toFixed(1)} lb</K> on {fmtDate(last.date)}. The next scan starts the comparison.</>
          : <>Skeletal muscle {first.smm_lb.toFixed(1)} → <K>{last.smm_lb.toFixed(1)} lb</K>, {fmtDate(first.date)} to {fmtDate(last.date)}
              {Math.abs(change) < MUSCLE_NOISE_LB ? ", within scan noise." : "."}</>}
      </HeroSub>
    </Card>
  );
}

// ── Main ──────────────────────────────────────────────────────────────────────

export default function Strength({ window = "3M", onWindowChange, windowOffset = 0, onWindowOffsetChange }) {
  const [tonnageMode, setTonnageMode] = useState("session");
  const [infoMetric, setInfoMetric] = useState(null);
  const { start, end } = windowDates(window, windowOffset);

  // ── Strength (Hevy) queries — section hides itself when no data is synced ──
  const [selectedLift, setSelectedLift] = useState(null);
  const [mgView, setMgView] = useState("regions");

  const { data: hevyDaily = [] } = useQuery({
    queryKey: ["hevy-daily", start, end],
    queryFn: () => api.hevyStrengthDaily(start, end),
  });

  const { data: e1rmData = [] } = useQuery({
    queryKey: ["hevy-e1rm", windowToTrendDays(window)],
    queryFn: () => api.hevyE1rm(windowToTrendDays(window), 5),
    staleTime: 1000 * 60 * 30,
  });

  const { data: muscleGroups } = useQuery({
    queryKey: ["hevy-muscle-groups", "building"],
    queryFn: () => api.hevyMuscleGroups(8, true),
    staleTime: 1000 * 60 * 30,
  });
  const { data: muscleGroupsAll } = useQuery({
    queryKey: ["hevy-muscle-groups", "all"],
    queryFn: () => api.hevyMuscleGroups(8, false),
    staleTime: 1000 * 60 * 30,
  });

  const { data: interventions = [] } = useQuery({ queryKey: ["interventions"], queryFn: api.listInterventions });

  // ── Strength derived data ──────────────────────────────────────────────────
  const tonnageChart = hevyDaily
    .filter(d => d.tonnage_kg != null)
    .map(d => ({
      date: fmtDate(d.date), iso: d.date,
      tonnage: Math.round(d.tonnage_kg * KG_TO_LB),
      sets: d.working_sets,
    }));

  const activeLift = e1rmData.find(e => e.exercise_template_id === selectedLift) ?? e1rmData[0];
  const e1rmChart = (activeLift?.sessions ?? []).map(s => ({
    date: fmtDate(s.date), iso: s.date,
    e1rm: Math.round(s.e1rm * KG_TO_LB),
    detail: `${Math.round(s.weight_kg * KG_TO_LB)} lb × ${s.reps}`,
  }));

  // Two questions per muscle: what you trained (every set, the pale bar) and what is likely
  // to grow it (building sets, the solid bar). Only the solid bar is judged against the
  // guideline, so conditioning can't make a muscle look dosed.
  const mgAll = muscleGroupsAll?.averages ?? {};
  const mgAverages = Object.keys({ ...mgAll, ...muscleGroups?.averages })
    .filter((g) => g !== "other")
    .map((g) => [g, muscleGroups?.averages?.[g] ?? 0])
    .sort((a, b) => b[1] - a[1] || (mgAll[b[0]] ?? 0) - (mgAll[a[0]] ?? 0));
  const mgBand = muscleGroups?.band ?? { low: 10, high: 20 };
  const mgScale = Math.max(mgBand.high, ...mgAverages.map(([g, v]) => Math.max(v, mgAll[g] ?? 0))) * 1.15;
  const hasStrength = hevyDaily.length > 0 || e1rmData.length > 0;

  // Region rollup of the per-muscle averages. Each region bar is STACKED from its
  // constituent muscles, and each segment keeps its own 10–20-band color — so an
  // undertrained region shows as a short bar of red segments at a glance. The
  // band itself is per-MUSCLE (the guideline doesn't sum across a region).
  const MUSCLE_REGIONS = {
    legs:      ["quadriceps", "hamstrings", "glutes", "calves", "abductors", "adductors"],
    back:      ["lats", "upper_back", "lower_back", "traps"],
    chest:     ["chest"],
    shoulders: ["shoulders"],
    arms:      ["biceps", "triceps", "forearms"],
    core:      ["abdominals"],
  };
  const avgMap = Object.fromEntries(mgAverages);
  const regionRows = Object.entries(MUSCLE_REGIONS)
    .map(([region, muscles]) => ({
      region,
      segments: muscles
        .filter(m => (avgMap[m] ?? 0) > 0)
        .map(m => ({ muscle: m, sets: avgMap[m] })),
      total: muscles.reduce((s, m) => s + (avgMap[m] ?? 0), 0),
      allTotal: muscles.reduce((s, m) => s + (mgAll[m] ?? 0), 0),
    }))
    .filter(r => r.total > 0 || r.allTotal > 0)
    .sort((a, b) => b.total - a.total);
  const regionScale = Math.max(1, ...regionRows.map(r => Math.max(r.total, r.allTotal))) * 1.1;

  // Weekly tonnage with ZERO WEEKS INCLUDED — the session-day chart compresses
  // rest gaps out of the x-axis (fine for per-session progression, misleading
  // for volume-over-time); this view is the honest time axis.
  const weeklyTonnage = (() => {
    if (!hevyDaily.length) return [];
    const day = new Date(`${start}T00:00:00`);
    day.setDate(day.getDate() - ((day.getDay() + 6) % 7));   // back to Monday
    const endD = new Date(`${end}T00:00:00`);
    const buckets = [];
    const idx = new Map();
    for (let d = new Date(day); d <= endD; d.setDate(d.getDate() + 7)) {
      const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
      idx.set(iso, buckets.length);
      buckets.push({ week: fmtDate(iso), iso, tonnage: 0, sessions: 0 });
    }
    for (const d of hevyDaily) {
      const dd = new Date(`${d.date}T00:00:00`);
      dd.setDate(dd.getDate() - ((dd.getDay() + 6) % 7));
      const iso = `${dd.getFullYear()}-${String(dd.getMonth() + 1).padStart(2, "0")}-${String(dd.getDate()).padStart(2, "0")}`;
      const i = idx.get(iso);
      if (i != null) {
        buckets[i].tonnage += Math.round((d.tonnage_kg ?? 0) * KG_TO_LB);
        buckets[i].sessions += d.sessions ?? 0;
      }
    }
    return buckets;
  })();

  return (
    <div className="space-y-4 p-4">
      <ViewVerdict view="strength" right="SINCE TRAINING BEGAN" />

      <TimeWindowSelector value={window} onChange={onWindowChange} offset={windowOffset} onOffsetChange={onWindowOffsetChange} />

      {!hasStrength && (
        <Card><Eyebrow left="STRENGTH" /><HeroSub>No Hevy sessions in this period.</HeroSub></Card>
      )}

      {/* ── Strength (Hevy set-level data) ─────────────────────────────────── */}
      {hasStrength && (
        <>
          {/* Tonnage — one chart, two time bases.
              These were two nearly identical bar charts. They are NOT redundant
              though, and the toggle has to preserve why: the per-session view
              compresses rest days out (so it answers "how hard was that session"),
              while the weekly view keeps zero weeks (so it answers "am I actually
              training", and a layoff shows as a gap rather than vanishing). */}
          {tonnageChart.length > 0 && (() => {
            const rows = tonnageMode === "session" ? tonnageChart : weeklyTonnage;
            const xKey = tonnageMode === "session" ? "date" : "week";
            const vals = rows.map(r => r.tonnage).filter(v => v > 0);
            const mean = vals.length >= 3
              ? Math.round(vals.reduce((a, b) => a + b, 0) / vals.length) : null;
            const latest = rows[rows.length - 1];
            return (
              <Card>
                <div className="flex items-baseline justify-between">
                  <p className="flex items-center gap-1 text-[9.5px] font-semibold tracking-eyebrow text-ink-3">
                    TONNAGE · LB
                    <button onClick={(e) => { e.stopPropagation(); setInfoMetric(METRIC_INFO.tonnage); }}
                      className="text-[10px] text-ink-3 hover:text-ink-2">ⓘ</button>
                  </p>
                  <SegmentedTabs
                    className="!border-0 gap-4"
                    value={tonnageMode} onChange={setTonnageMode}
                    options={[{ value: "session", label: "SESSION" }, { value: "week", label: "WEEK" }]}
                  />
                </div>
                <Hero
                  value={latest ? Math.round(latest.tonnage).toLocaleString() : "—"}
                  unit={tonnageMode === "session"
                    ? ["LAST SESSION", `${latest?.sets ?? 0} WORKING SETS`]
                    : ["THIS WEEK", `${latest?.sessions ?? 0} SESSIONS`]}
                />
                <HeroSub>
                  {mean != null
                    ? <>Typical {tonnageMode === "session" ? "session" : "week"}{" "}
                        <K>{mean.toLocaleString()} lb</K> over {vals.length}{" "}
                        {tonnageMode === "session" ? "sessions" : "active weeks"}</>
                    : <>Not enough history yet for a typical value.</>}
                </HeroSub>
                <div className="mt-3">
                  <ResponsiveContainer width="100%" height={150}>
                    <ComposedChart data={rows} margin={{ top: 4, right: 8, bottom: 0, left: -8 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="var(--rule)" vertical={false} />
                      <XAxis dataKey={xKey} tick={{ fill: "var(--ink-3)", fontSize: 10 }} interval="preserveStartEnd" />
                      <YAxis tick={{ fill: "var(--ink-3)", fontSize: 10 }}
                        tickFormatter={v => v >= 1000 ? `${(v / 1000).toFixed(0)}k` : v} />
                      <Tooltip
                        contentStyle={{ background: "var(--canvas)", border: "1px solid var(--rule)", borderRadius: 8, fontSize: 12 }}
                        labelStyle={{ color: "var(--ink-3)" }}
                        formatter={(v, n, { payload }) => [
                          tonnageMode === "session"
                            ? `${v.toLocaleString()} lb · ${payload.sets} sets`
                            : `${v.toLocaleString()} lb · ${payload.sessions} session${payload.sessions === 1 ? "" : "s"}`,
                          tonnageMode === "session" ? "Session" : "Week"]}
                      />
                      {/* Dashed = what you actually do, matching the nutrition bars. */}
                      {mean != null && (
                        <ReferenceLine y={mean} stroke="var(--ink)" strokeDasharray="4 3" strokeOpacity={0.7} />
                      )}
                      {tonnageMode === "session" && interventionMarkers(interventions, tonnageChart, "iso", "date")}
                      <Bar dataKey="tonnage" name="Tonnage" fill="var(--rem)" radius={[2, 2, 0, 0]} isAnimationActive={false} />
                    </ComposedChart>
                  </ResponsiveContainer>
                  {tonnageMode === "session" && (
                    <ChartEventKey interventions={interventions} rows={tonnageChart} isoKey="iso" />
                  )}
                </div>
                <Foot>
                  Weight x reps, warmups excluded — external load, alongside TRIMP's
                  internal one. <K>Session</K> drops rest days, so it reads as "how hard
                  was that session". <K>Week</K> keeps empty weeks, so a layoff shows as
                  a gap instead of disappearing.
                </Foot>
              </Card>
            );
          })()}

          {/* Rep-range bests (PR tracker, 2026-10-29) — above e1RM: class sets are rarely
              near failure, so the best-for-N-reps moves only when he lifts more. */}
          <RepBests days={windowToTrendDays(window)} />

          {/* Estimated 1RM. A bare line answers "what is it" but not "is it moving",
              which is the only reason to look — so the window delta is the headline
              and the best set in the window is marked on the series. */}
          {e1rmData.length > 0 && (() => {
            const vals = e1rmChart.filter(r => r.e1rm != null);
            const first = vals[0]?.e1rm, last = vals[vals.length - 1]?.e1rm;
            const delta = (first != null && last != null) ? last - first : null;
            const best = vals.length ? Math.max(...vals.map(r => r.e1rm)) : null;
            const bestRow = best != null ? vals.find(r => r.e1rm === best) : null;
            const atBest = best != null && last === best;
            return (
              <Card>
                      <div className="flex items-baseline justify-between">
                  <p className="flex items-center gap-1 text-[9.5px] font-semibold tracking-eyebrow text-ink-3">
                    ESTIMATED 1RM
                    <button onClick={() => setInfoMetric(METRIC_INFO.e1rm)}
                      className="text-[10px] text-ink-3 hover:text-ink-2">ⓘ</button>
                  </p>
                  <span className="text-[9.5px] tracking-eyebrow text-ink-3">EPLEY · BEST SET ≤15 REPS</span>
                </div>
                <div className="mt-2.5 flex flex-wrap gap-1.5">
                  {e1rmData.map(e => {
                    const active = e.exercise_template_id === (activeLift?.exercise_template_id);
                    return (
                      <button key={e.exercise_template_id}
                        onClick={() => setSelectedLift(e.exercise_template_id)}
                        className={`rounded-full px-2.5 py-1 text-[10px] font-medium transition-colors ${
                          active ? "bg-surface text-ink ring-1 ring-inset ring-[var(--rem)]"
                                 : "bg-surface text-ink-3 hover:text-ink-2"}`}>
                        {e.title}
                      </button>
                    );
                  })}
                </div>

                {e1rmChart.length < 2 ? (
                  <p className="mt-3 text-[11px] text-ink-3">
                    Not enough sessions of {activeLift?.title} in this window yet.
                  </p>
                ) : (
                  <>
                    <Hero
                      value={last != null ? Math.round(last) : "—"}
                      unit={["LB e1RM", activeLift?.title?.toUpperCase() ?? ""]}
                    />
                    <HeroSub>
                      {delta != null && (
                        <>
                          <K>{delta >= 0 ? "+" : "−"}{Math.abs(Math.round(delta))} lb</K>
                          {" over this window"}
                        </>
                      )}
                      {best != null && (
                        <> <Sep />{atBest ? <K>at your best</K> : <>best <K>{Math.round(best)}</K></>}</>
                      )}
                    </HeroSub>
                    <div className="mt-3">
                      <ResponsiveContainer width="100%" height={160}>
                        <ComposedChart data={e1rmChart} margin={{ top: 10, right: 8, bottom: 0, left: -8 }}>
                          <CartesianGrid strokeDasharray="3 3" stroke="var(--rule)" />
                          <XAxis dataKey="date" tick={{ fill: "var(--ink-3)", fontSize: 10 }} interval="preserveStartEnd" />
                          <YAxis tick={{ fill: "var(--ink-3)", fontSize: 10 }} domain={["auto", "auto"]} />
                          <Tooltip
                            contentStyle={{ background: "var(--canvas)", border: "1px solid var(--rule)", borderRadius: 8, fontSize: 12 }}
                            labelStyle={{ color: "var(--ink-3)" }}
                            formatter={(v, name, { payload }) => [`${v} lb (${payload.detail})`, "e1RM"]}
                          />
                          {/* The best set in the window, named. A max is only useful
                              if you can see WHEN it happened relative to now. */}
                          {bestRow && (
                            <ReferenceLine y={best} stroke="var(--rem)" strokeDasharray="4 3" strokeOpacity={0.55}
                              label={{ value: `best ${Math.round(best)}`, position: "insideTopRight",
                                       fill: "var(--rem)", fontSize: 9 }} />
                          )}
                          <Line dataKey="e1rm" name="e1RM" stroke="var(--rem)" strokeWidth={2}
                            dot={{ r: 2.5, fill: "var(--rem)" }} activeDot={{ r: 4 }} isAnimationActive={false} />
                        </ComposedChart>
                      </ResponsiveContainer>
                    </div>
                  </>
                )}
                <Foot>
                  Your most recent lifts, estimated from each session's best set of 15
                  reps or fewer — rougher at 15 reps than at 5. Session-to-session wobble
                  is normal; the window delta is the signal.
                </Foot>
              </Card>
            );
          })()}

          {/* Weekly hard sets — Regions (default) / Muscles toggle */}
          {mgAverages.length > 0 && (
            <Card>
              <div className="flex items-baseline justify-between">
                <p className="flex items-center gap-1 text-[9.5px] font-semibold tracking-eyebrow text-ink-3">
                  WEEKLY VOLUME · SETS
                  <button onClick={() => setInfoMetric(METRIC_INFO.hard_sets)}
                    className="text-[10px] text-ink-3 hover:text-ink-2">ⓘ</button>
                </p>
                <SegmentedTabs
                  className="!border-0 gap-4"
                  value={mgView} onChange={setMgView}
                  options={[{ value: "regions", label: "REGIONS" }, { value: "muscles", label: "MUSCLES" }]}
                />
              </div>
              <p className="mt-1 text-[9.5px] text-ink-3">
                mean over {muscleGroups.active_weeks} active weeks · guideline {mgBand.low}–{mgBand.high} sets/muscle/wk
              </p>
              <p className="mt-1 mb-2.5 flex items-center gap-3 text-[9.5px] text-ink-3">
                <span className="flex items-center gap-1.5">
                  <span className="h-2 w-3 rounded-sm" style={{ backgroundColor: "var(--ink-3)", opacity: 0.45 }} />all sets
                </span>
                <span className="flex items-center gap-1.5">
                  <span className="h-2 w-3 rounded-sm" style={{ backgroundColor: "#22c55e" }} />building sets
                </span>
                <span className="ml-auto">building / all</span>
              </p>

              {mgView === "regions" ? (
                <>

                  <div className="space-y-2.5">
                    {regionRows.map(({ region, segments, total, allTotal }) => (
                      <div key={region} className="flex items-center gap-3">
                        <span className="w-24 text-xs capitalize text-ink-2">{region}</span>
                        <div className="relative flex-1 h-3 rounded-full bg-surface flex overflow-hidden">
                          <div className="absolute inset-y-0 left-0 rounded-full"
                            style={{ width: `${(allTotal / regionScale) * 100}%`, backgroundColor: "var(--ink-3)", opacity: 0.45 }} />
                          {segments.map((s, i) => (
                            <div key={s.muscle}
                              title={`${s.muscle.replace(/_/g, " ")}: ${s.sets.toFixed(1)}/wk`}
                              className={i > 0 ? "relative h-full border-l border-canvas" : "relative h-full"}
                              style={{
                                width: `${(s.sets / regionScale) * 100}%`,
                                backgroundColor: setsBandColor(s.sets, mgBand),
                              }} />
                          ))}
                        </div>
                        <span className="w-[82px] flex-none whitespace-nowrap text-right text-xs font-semibold text-ink-2">
                          {total.toFixed(1)}<span className="font-normal text-ink-3"> / {allTotal.toFixed(1)}</span>
                        </span>
                      </div>
                    ))}
                  </div>
                  <Foot>
                    The pale bar is everything you did. Each solid segment is one muscle&apos;s
                    building sets, coloured against its own {mgBand.low}–{mgBand.high}/wk guideline.
                    A long pale bar with a short solid one means plenty of work but little loading.
                    Region totals are deliberately not band-scored — the guideline is per muscle.
                  </Foot>
                </>
              ) : (
                <>

                  <div className="space-y-2">
                    {mgAverages.map(([group, avg], i) => (
                      <div key={group} className="flex items-center gap-3">
                        <span className="w-24 text-xs capitalize" style={{ color: MUSCLE_COLORS[i % MUSCLE_COLORS.length] }}>
                          {group.replace(/_/g, " ")}
                        </span>
                        <div className="relative flex-1 h-2 rounded-full bg-surface">
                          {/* guideline band */}
                          <div className="absolute h-full rounded-full bg-ink/15"
                            style={{ left: `${mgBand.low / mgScale * 100}%`, width: `${(mgBand.high - mgBand.low) / mgScale * 100}%` }} />
                          <div className="absolute h-full rounded-full"
                            style={{ width: `${Math.min(100, (mgAll[group] ?? 0) / mgScale * 100)}%`, backgroundColor: "var(--ink-3)", opacity: 0.45 }} />
                          <div className="absolute h-full rounded-full"
                            style={{ width: `${Math.min(100, avg / mgScale * 100)}%`, backgroundColor: setsBandColor(avg, mgBand) }} />
                          {[mgBand.low, mgBand.high].map((t) => (
                            <div key={t} className="absolute -inset-y-0.5 w-px" style={{ left: `${t / mgScale * 100}%`, backgroundColor: "var(--ink-2)" }} />
                          ))}
                        </div>
                        <span className="w-[82px] flex-none whitespace-nowrap text-right text-xs font-semibold" style={{ color: setsBandColor(avg, mgBand) }}>
                          {avg.toFixed(1)}<span className="font-normal text-ink-3"> / {(mgAll[group] ?? 0).toFixed(1)}</span>
                        </span>
                      </div>
                    ))}
                  </div>
                  <Foot>
                    Sets per muscle per week; a secondary muscle counts half. Pale is everything
                    you did, solid is sets likely to build muscle, and the colour judges solid
                    against the {mgBand.low}–{mgBand.high} guideline, ticked on each track.
                  </Foot>
                </>
              )}
            </Card>
          )}
        </>
      )}

      <MuscleMass />

      <WorkoutHistory start={start} defaultFilter="strength" />

      {infoMetric && (
        <MetricInfoModal metric={METRIC_INFO[infoMetric] ?? infoMetric} onClose={() => setInfoMetric(null)} />
      )}
    </div>
  );
}
