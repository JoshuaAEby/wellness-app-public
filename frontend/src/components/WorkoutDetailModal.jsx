import { useQuery } from "@tanstack/react-query";
import { api } from "../api.js";
import { fmtDuration } from "../utils.js";
import {
  ResponsiveContainer, LineChart, Line, XAxis, YAxis,
  CartesianGrid, Tooltip, ReferenceLine,
} from "recharts";

// Labels/colours only. The bpm RANGES are NOT hardcoded here any more — they come
// from the API (`zone_bounds`), which derives them from services/hr_zones (Karvonen
// %HRR off your measured max). Hardcoding them meant this modal kept printing
// "< 115 / 115-135 / ..." after the boundaries moved to 142/155/168/181.
const ZONE_META = [
  { key: "z1_min", label: "Z1 Recovery",  color: "#475569" },
  { key: "z2_min", label: "Z2 Aerobic",   color: "#0ea5e9" },
  { key: "z3_min", label: "Z3 Tempo",     color: "#10b981" },
  { key: "z4_min", label: "Z4 Threshold", color: "#f59e0b" },
  { key: "z5_min", label: "Z5 Max",       color: "#f43f5e" },
];

// bounds = {z2,z3,z4,z5} bpm -> display range per zone index
function zoneRange(bounds, i) {
  if (!bounds) return "—";
  const b = [bounds.z2, bounds.z3, bounds.z4, bounds.z5];
  if (i === 0) return `< ${b[0]}`;
  if (i === 4) return `> ${b[3]}`;
  return `${b[i - 1]}–${b[i]}`;
}

function fmtTime(isoStr) {
  if (!isoStr) return "—";
  return new Date(isoStr + (isoStr.endsWith("Z") ? "" : "Z"))
    .toLocaleString("en-US", { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function StatBox({ label, value, sub }) {
  return (
    <div className="rounded-xl bg-slate-700/60 px-3 py-2.5 text-center">
      <p className="text-xs text-slate-400 mb-0.5">{label}</p>
      <p className="text-base font-bold text-white">{value ?? "—"}</p>
      {sub && <p className="text-xs text-slate-500 mt-0.5">{sub}</p>}
    </div>
  );
}

const KG_TO_LB = 2.20462;

function fmtSet(s) {
  if (s.weight_kg != null && s.reps != null)
    return `${Math.round(s.weight_kg * KG_TO_LB)} lb × ${s.reps}`;
  if (s.reps != null) return `${s.reps} reps`;
  if (s.duration_seconds != null) return `${Math.round(s.duration_seconds)}s`;
  if (s.distance_meters != null) return `${Math.round(s.distance_meters)}m`;
  return "—";
}

export default function WorkoutDetailModal({ workoutId, onClose }) {
  const { data, isLoading } = useQuery({
    queryKey: ["workout-detail", workoutId],
    queryFn: () => api.workoutDetail(workoutId),
    enabled: !!workoutId,
  });

  const { data: hevy } = useQuery({
    queryKey: ["hevy-for-workout", workoutId],
    queryFn: () => api.hevyForWorkout(workoutId),
    enabled: !!workoutId,
  });

  const w       = data?.workout;
  const series  = data?.hr_series ?? [];
  const zones   = data?.hr_zones ?? {};

  const totalZoneMins = ZONE_META.reduce((s, z) => s + (zones[z.key] ?? 0), 0);

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/70" onClick={onClose}>
      <div
        className="w-full max-w-lg rounded-t-2xl bg-slate-800 overflow-y-auto"
        style={{ maxHeight: "90vh" }}
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div className="sticky top-0 bg-slate-800 border-b border-slate-700 px-5 py-4 flex items-start justify-between">
          <div>
            <p className="text-lg font-bold text-white">{w?.activity_type ?? "Workout"}</p>
            <p className="text-xs text-slate-400 mt-0.5">{fmtTime(w?.start_time)}</p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-white transition-colors text-2xl leading-none mt-0.5">×</button>
        </div>

        <div className="p-5 space-y-5 pb-10">
          {isLoading ? (
            <p className="text-sm text-slate-400 text-center py-8">Loading…</p>
          ) : (
            <>
              {/* Stats grid */}
              <div className="grid grid-cols-3 gap-2">
                <StatBox label="Duration"    value={w?.duration_sec != null ? fmtDuration(w.duration_sec) : null} />
                <StatBox label="TRIMP"       value={w?.trimp != null ? Math.round(w.trimp) : null} sub="HR load" />
                <StatBox label="Calories"    value={w?.active_calories != null ? `${Math.round(w.active_calories)} kcal` : null} />
                <StatBox label="RPE"         value={w?.rpe != null ? `${w.rpe}/10` : null} sub="effort" />
                <StatBox label="Avg HR"      value={w?.avg_hr != null ? `${Math.round(w.avg_hr)} bpm` : null} />
                <StatBox label="Max HR"      value={w?.max_hr != null ? `${Math.round(w.max_hr)} bpm` : null} />
                <StatBox label="HR Range"
                  value={series.length > 0
                    ? `${Math.min(...series.map(p => p.bpm))}–${Math.max(...series.map(p => p.bpm))}`
                    : null}
                  sub="bpm"
                />
              </div>

              {/* HR chart */}
              {series.length > 0 && (
                <div className="rounded-xl bg-slate-700/40 p-4">
                  <p className="text-xs font-semibold uppercase tracking-widest text-slate-400 mb-3">Heart Rate</p>
                  <ResponsiveContainer width="100%" height={180}>
                    <LineChart data={series} margin={{ top: 4, right: 8, bottom: 0, left: -16 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                      <XAxis
                        dataKey="t"
                        tick={{ fill: "#64748b", fontSize: 10 }}
                        tickFormatter={v => `${Math.floor(v)}m`}
                        interval="preserveStartEnd"
                      />
                      <YAxis
                        tick={{ fill: "#64748b", fontSize: 10 }}
                        domain={[
                          dataMin => Math.max(40, Math.floor(dataMin / 10) * 10 - 10),
                          dataMax => Math.ceil(dataMax / 10) * 10 + 10,
                        ]}
                      />
                      <Tooltip
                        contentStyle={{ background: "#1e293b", border: "1px solid #334155", borderRadius: 8, fontSize: 12 }}
                        labelStyle={{ color: "#94a3b8" }}
                        labelFormatter={v => `${Math.floor(v)}:${String(Math.round((v % 1) * 60)).padStart(2,"0")} min`}
                        formatter={v => [`${v} bpm`, "HR"]}
                      />
                      {w?.avg_hr != null && (
                        <ReferenceLine
                          y={Math.round(w.avg_hr)}
                          stroke="#818cf8"
                          strokeDasharray="4 4"
                          strokeOpacity={0.7}
                          label={{ value: `avg ${Math.round(w.avg_hr)}`, position: "insideTopRight", fill: "#818cf8", fontSize: 9 }}
                        />
                      )}
                      <Line
                        dataKey="bpm"
                        stroke="#f43f5e"
                        strokeWidth={1.5}
                        dot={false}
                        isAnimationActive={false}
                      />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              )}

              {/* Zone breakdown */}
              {totalZoneMins > 0 && (
                <div className="rounded-xl bg-slate-700/40 p-4">
                  <p className="text-xs font-semibold uppercase tracking-widest text-slate-400 mb-3">Time in Zone</p>
                  {/* Stacked bar */}
                  <div className="flex h-3 rounded-full overflow-hidden mb-3">
                    {ZONE_META.map(z => {
                      const pct = totalZoneMins > 0 ? (zones[z.key] ?? 0) / totalZoneMins * 100 : 0;
                      return pct > 0 ? (
                        <div key={z.key} style={{ width: `${pct}%`, backgroundColor: z.color }} />
                      ) : null;
                    })}
                  </div>
                  {/* Zone rows */}
                  <div className="space-y-1.5">
                    {ZONE_META.map((z, zi) => {
                      const mins = zones[z.key] ?? 0;
                      const pct = totalZoneMins > 0 ? Math.round(mins / totalZoneMins * 100) : 0;
                      return (
                        <div key={z.key} className="flex items-center gap-2 text-xs">
                          <span className="w-2 h-2 rounded-full flex-shrink-0" style={{ backgroundColor: z.color }} />
                          <span className="text-slate-300 w-28">{z.label}</span>
                          <span className="text-slate-500 w-16">{zoneRange(data?.zone_bounds, zi)} bpm</span>
                          <div className="flex-1 h-1.5 rounded-full bg-slate-600/40">
                            <div className="h-full rounded-full" style={{ width: `${pct}%`, backgroundColor: z.color }} />
                          </div>
                          <span className="text-slate-400 w-10 text-right">{mins > 0 ? `${mins.toFixed(1)}m` : "—"}</span>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}

              {/* Hevy set detail (linked strength session) */}
              {hevy?.linked && (
                <div className="rounded-xl bg-slate-700/40 p-4">
                  <div className="flex items-center justify-between mb-1">
                    <p className="text-xs font-semibold uppercase tracking-widest text-slate-400">Sets · Hevy</p>
                    <span className="text-xs text-slate-500">{hevy.hevy_title}</span>
                  </div>
                  <p className="mb-3 text-xs text-slate-500">
                    {hevy.tonnage_kg != null && `${Math.round(hevy.tonnage_kg * KG_TO_LB).toLocaleString()} lb tonnage · `}
                    {hevy.working_sets} working sets
                    {hevy.avg_top_set_rpe != null && ` · top-set RPE ${hevy.avg_top_set_rpe}`}
                  </p>
                  <div className="space-y-3">
                    {hevy.exercises.map((ex, i) => (
                      <div key={i}>
                        <p className="text-sm font-medium text-white mb-1">{ex.title}</p>
                        <div className="space-y-0.5">
                          {ex.sets.map((s, j) => (
                            <div key={j} className="flex items-center gap-2 text-xs">
                              <span className={`w-14 ${s.type === "warmup" ? "text-slate-600" : "text-slate-500"}`}>
                                {s.type === "warmup" ? "warmup" : s.type === "normal" ? `set ${j + 1}` : s.type}
                              </span>
                              <span className={s.type === "warmup" ? "text-slate-500" : "text-slate-300"}>
                                {fmtSet(s)}
                              </span>
                              {s.rpe != null && <span className="text-indigo-400">@{s.rpe}</span>}
                            </div>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
