import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";
import { fmtDate, fmtDuration } from "../utils.js";
import { SectionHeader, SegmentedTabs } from "./ui.jsx";
import WorkoutDetailModal from "./WorkoutDetailModal.jsx";

// What the session actually was, from the backend's `session_kind` (Strength label wins, else HR). Kept as a
// display map only — the classification itself lives in app/workout_categories so the
// threshold has exactly one home.
const SESSION_KIND_LABEL = { lifting: "Strength (class)", conditioning: "Conditioning" };

function hrZone(avgHr, maxHr) {
  if (!avgHr || !maxHr) return null;
  const pct = avgHr / maxHr;
  if (pct >= 0.94) return { label: "Z5", color: "#ef4444" };
  if (pct >= 0.77) return { label: "Z4", color: "#f97316" };
  if (pct >= 0.64) return { label: "Z3", color: "#eab308" };
  if (pct >= 0.57) return { label: "Z2", color: "#22c55e" };
  return               { label: "Z1", color: "#38bdf8" };
}
// ── RPE helpers ───────────────────────────────────────────────────────────────

function rpeColor(rpe) {
  if (rpe == null) return "#64748b";
  if (rpe <= 4) return "#22c55e";
  if (rpe <= 6) return "#eab308";
  if (rpe <= 8) return "#f97316";
  return "#ef4444";
}
// Classes are often both, so this is one list with a filter rather than two lists. A lifting
// class counts as strength; every other session, walks and conditioning classes included, as cardio.
const isStrength = (w) => w.session_kind === "lifting"
  || (!w.session_kind && /strength/i.test(w.activity_type ?? ""));
const FILTERS = { all: () => true, cardio: (w) => !isStrength(w), strength: isStrength };

export default function WorkoutHistory({ start, defaultFilter = "all" }) {
  const [filter, setFilter] = useState(defaultFilter);
  const [rpeEditing, setRpeEditing] = useState(null);
  const [selectedWorkoutId, setSelectedWorkoutId] = useState(null);
  const qc = useQueryClient();

  const { data: workouts = [], isLoading: loadingWorkouts } = useQuery({
    queryKey: ["workouts"],
    queryFn: () => api.workouts(200),
  });

  const { data: settings } = useQuery({
    queryKey: ["settings"],
    queryFn: api.getSettings,
  });
  const rpeSet = useMutation({
    mutationFn: ({ id, rpe }) => api.setWorkoutRpe(id, rpe),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["workouts"] });
      qc.invalidateQueries({ queryKey: ["workouts-recent"] });
      setRpeEditing(null);
    },
  });

  const filteredWorkouts = workouts.filter(w => w.start_time >= start).filter(FILTERS[filter]);

  return (
    <>
      {/* ── Workout History ────────────────────────────────────────────────── */}
      <SectionHeader label="Workout History" />

      <div className="rounded-2xl border border-rule bg-card p-4">
        <div className="mb-3 flex items-baseline justify-between">
          <p className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">
            Sessions ({filteredWorkouts.length})
          </p>
          <SegmentedTabs
            className="!border-0 gap-4"
            value={filter} onChange={setFilter}
            options={[{ value: "all", label: "ALL" }, { value: "cardio", label: "CARDIO" }, { value: "strength", label: "STRENGTH" }]}
          />
        </div>
        {loadingWorkouts ? (
          <p className="text-sm text-ink-2">Loading…</p>
        ) : filteredWorkouts.length === 0 ? (
          <p className="text-sm text-ink-3">No workouts in this period</p>
        ) : (
          <div className="space-y-2 max-h-[32rem] overflow-y-auto">
            {filteredWorkouts.map((w) => {
              const zone = hrZone(w.avg_hr, settings?.max_hr);
              const editing = rpeEditing === w.id;
              return (
                <div key={w.id} className="rounded-lg bg-surface px-3 py-2 space-y-2">
                  <div className="flex items-center justify-between">
                    {/* Tappable left side opens detail modal */}
                    <button
                      className="flex-1 text-left min-w-0"
                      onClick={() => setSelectedWorkoutId(w.id)}
                    >
                      <p className="text-sm font-medium text-ink">
                        {/* The gym labels every class "High Intensity Interval
                            Training", so the raw label tells you nothing about which of
                            two very different sessions this was. `session_kind` is
                            HR-derived on the backend (workout_categories); null means
                            genuinely unclassifiable, so it falls back to the raw label
                            rather than guessing. The gym's own class name from Hevy
                            leads when there is one (note 31) — the best label there is;
                            the hard-minute chip stays beside it as the evidence. */}
                        {w.hevy_title || (SESSION_KIND_LABEL[w.session_kind] ?? w.activity_type ?? "Workout")}
                        {w.session_kind && (
                          <span className="ml-1.5 align-middle text-[9px] tracking-micro text-ink-3">
                            {w.high_intensity_min != null
                              ? `${Math.round(w.high_intensity_min)}m HARD`
                              : "DERIVED"}
                          </span>
                        )}
                        {w.label_agrees_with_hr === false && (
                          <span className="ml-1.5 align-middle text-[9px] tracking-micro"
                                style={{ color: "var(--breakfast)" }}>
                            LABEL≠HR
                          </span>
                        )}
                      </p>
                      <p className="text-xs text-ink-2">{fmtDate(w.start_time?.slice(0, 10))}</p>
                    </button>
                    <div className="flex items-center gap-2 flex-shrink-0">
                      <button
                        className="text-right text-xs text-ink-2 space-y-0.5"
                        onClick={() => setSelectedWorkoutId(w.id)}
                      >
                        {w.active_calories != null && <p>{Math.round(w.active_calories)} kcal</p>}
                        {w.avg_hr != null && (
                          <p>
                            {Math.round(w.avg_hr)}
                            {w.max_hr != null && <span className="text-ink-3">/{Math.round(w.max_hr)}</span>}
                            {" bpm"}
                            {zone && <span className="ml-1 font-semibold" style={{ color: zone.color }}>{zone.label}</span>}
                          </p>
                        )}
                        {w.duration_sec != null && <p>{fmtDuration(w.duration_sec)}</p>}
                        {w.trimp != null && (
                          <p className="font-semibold text-[color:var(--hrv)]">{Math.round(w.trimp)} TRIMP</p>
                        )}
                      </button>
                      {/* RPE badge — stopPropagation so it doesn't open the modal */}
                      <button
                        onClick={(e) => { e.stopPropagation(); setRpeEditing(editing ? null : w.id); }}
                        className="flex-shrink-0 rounded-md px-2 py-1 text-xs font-semibold transition-colors"
                        style={w.rpe != null
                          ? { color: rpeColor(w.rpe), backgroundColor: `${rpeColor(w.rpe)}22` }
                          : { color: "#475569", backgroundColor: "#1e293b" }
                        }
                        title={w.rpe != null ? "Edit RPE" : "Set RPE"}
                      >
                        {w.rpe != null ? `RPE ${w.rpe}` : "+ RPE"}
                      </button>
                    </div>
                  </div>
                  {/* Inline RPE picker */}
                  {editing && (
                    <div className="flex items-center gap-1 flex-wrap pt-0.5">
                      {[1,2,3,4,5,6,7,8,9,10].map(n => (
                        <button
                          key={n}
                          disabled={rpeSet.isPending}
                          onClick={() => rpeSet.mutate({ id: w.id, rpe: n })}
                          className="w-7 h-7 rounded text-xs font-bold transition-all disabled:opacity-40"
                          style={{
                            color: rpeColor(n),
                            backgroundColor: w.rpe === n ? `${rpeColor(n)}33` : "#0f172a",
                            border: `1px solid ${w.rpe === n ? rpeColor(n) : "#334155"}`,
                          }}
                        >
                          {n}
                        </button>
                      ))}
                      {w.rpe != null && (
                        <button
                          disabled={rpeSet.isPending}
                          onClick={() => rpeSet.mutate({ id: w.id, rpe: null })}
                          className="ml-1 text-xs text-ink-3 hover:text-rose-400 transition-colors disabled:opacity-40"
                        >
                          clear
                        </button>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
      {selectedWorkoutId && (
        <WorkoutDetailModal workoutId={selectedWorkoutId} onClose={() => setSelectedWorkoutId(null)} />
      )}
    </>
  );
}
