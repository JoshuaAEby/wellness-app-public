import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";
import ProfileModal from "./ProfileModal.jsx";
import DataHealth from "../tabs/insights/DataHealth.jsx";

const HOURS = Array.from({ length: 24 }, (_, i) => {
  const ampm = i >= 12 ? "pm" : "am";
  const h12  = i % 12 || 12;
  return { value: i, label: `${h12}:00 ${ampm}` };
});

function Section({ title, children }) {
  return (
    <div>
      <p className="mb-3 text-xs font-medium uppercase tracking-widest text-slate-500">{title}</p>
      <div className="space-y-3">{children}</div>
    </div>
  );
}

function Field({ label, hint, children }) {
  return (
    <div>
      <label className="mb-1 block text-xs text-slate-400">{label}</label>
      {hint && <p className="mb-1 text-xs text-slate-600">{hint}</p>}
      {children}
    </div>
  );
}

const INPUT = "w-full rounded-lg bg-slate-700 px-4 py-2 text-white outline-none focus:ring-2 focus:ring-indigo-500";

export default function SettingsModal({ onClose, onLogout }) {
  const qc = useQueryClient();
  const [showProfile, setShowProfile] = useState(false);
  const [showHealth, setShowHealth] = useState(false);
  const { data: settings } = useQuery({ queryKey: ["settings"], queryFn: api.getSettings });
  const { data: usage }    = useQuery({ queryKey: ["ai-usage"],  queryFn: api.getUsage });

  // Goals — sleep
  const [sleepTarget,      setSleepTarget]      = useState("");
  // Goals — activity
  const [stepsGoal,        setStepsGoal]        = useState("");
  const [moveGoal,         setMoveGoal]         = useState("");
  const [exerciseGoal,     setExerciseGoal]     = useState("");
  // Goals — nutrition
  const [proteinGoal,          setProteinGoal]          = useState("");
  const [fiberGoal,            setFiberGoal]            = useState("");
  const [calorieGoal,          setCalorieGoal]          = useState("");
  const [calorieMode,          setCalorieMode]          = useState("fixed");
  const [weightChangePace,     setWeightChangePace]     = useState("");
  // Goals — habits
  const [caffeineHour,     setCaffeineHour]     = useState(14);
  const [lightHour,        setLightHour]        = useState(10);
  // Training
  const [maxHr,            setMaxHr]            = useState("");
  const [goal,             setGoal]             = useState("");
  // AI
  const [aiEnabled,        setAiEnabled]        = useState(true);

  // Pre-fill once settings load
  const initialized = sleepTarget !== "";
  if (settings && !initialized) {
    setSleepTarget(String(settings.target_sleep_hours));
    setStepsGoal(String(settings.steps_goal ?? 10000));
    setMoveGoal(String(settings.move_goal ?? 500));
    setExerciseGoal(String(settings.exercise_goal ?? 30));
    setProteinGoal(settings.protein_goal_g != null ? String(settings.protein_goal_g) : "");
    setFiberGoal(settings.fiber_goal_g != null ? String(settings.fiber_goal_g) : "");
    setCalorieGoal(settings.calorie_goal   != null ? String(settings.calorie_goal)   : "");
    setCalorieMode(settings.calorie_goal_mode ?? "fixed");
    setWeightChangePace(settings.weight_change_goal_lbs_per_week != null ? String(settings.weight_change_goal_lbs_per_week) : "");
    setCaffeineHour(settings.caffeine_cutoff_hour ?? 14);
    setLightHour(settings.light_cutoff_hour ?? 10);
    setMaxHr(String(settings.max_hr));
    setGoal(settings.training_goal ?? "building");
    setAiEnabled(settings.ai_enabled ?? true);
  }

  const saveMutation = useMutation({
    mutationFn: () =>
      api.updateSettings({
        target_sleep_hours:   parseFloat(sleepTarget),
        steps_goal:           parseInt(stepsGoal),
        move_goal:            parseInt(moveGoal),
        exercise_goal:        parseInt(exerciseGoal),
        protein_goal_g:       proteinGoal !== "" ? parseInt(proteinGoal) : null,
        fiber_goal_g:         fiberGoal   !== "" ? parseInt(fiberGoal)   : null,
        calorie_goal:         calorieGoal !== "" ? parseInt(calorieGoal) : null,
        calorie_goal_mode:    calorieMode,
        weight_change_goal_lbs_per_week: weightChangePace !== "" ? parseFloat(weightChangePace) : null,
        caffeine_cutoff_hour: caffeineHour,
        light_cutoff_hour:    lightHour,
        max_hr:               parseInt(maxHr),
        training_goal:        goal,
        ai_enabled:           aiEnabled,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["settings"] });
      qc.invalidateQueries({ queryKey: ["today"] });
      qc.invalidateQueries({ queryKey: ["streaks"] });
      onClose();
    },
  });

  const recomputeMutation = useMutation({
    mutationFn: api.recompute,
    onSuccess: (data) => {
      qc.invalidateQueries();
      alert(`Recalculated ${data.recomputed} days of history.`);
    },
  });

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/60 pb-16" onClick={onClose}>
      <div
        className="w-full max-w-md rounded-t-2xl bg-slate-800 p-6 pb-10 max-h-[85vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-5 flex items-center justify-between">
          <h2 className="text-lg font-semibold text-white">Settings</h2>
          <button
            onClick={() => setShowProfile(true)}
            className="flex items-center gap-1.5 rounded-lg bg-slate-700 px-3 py-1.5 text-xs font-medium text-slate-300 hover:text-white transition-colors"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="w-3.5 h-3.5">
              <path strokeLinecap="round" strokeLinejoin="round" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
            </svg>
            Profile
          </button>
        </div>

        <div className="space-y-6">

          {/* ── Goals ─────────────────────────────────────────────────────── */}
          <Section title="Goals — Sleep">
            <Field label="Target sleep (hours)">
              <input type="number" step="0.5" value={sleepTarget}
                onChange={(e) => setSleepTarget(e.target.value)}
                className={INPUT} />
            </Field>
          </Section>

          <Section title="Goals — Activity">
            <Field label="Steps">
              <input type="number" step="500" value={stepsGoal}
                onChange={(e) => setStepsGoal(e.target.value)}
                className={INPUT} />
            </Field>
            <Field label="Move (active kcal)">
              <input type="number" step="50" value={moveGoal}
                onChange={(e) => setMoveGoal(e.target.value)}
                className={INPUT} />
            </Field>
            <Field label="Exercise (minutes / day)">
              <input type="number" step="5" value={exerciseGoal}
                onChange={(e) => setExerciseGoal(e.target.value)}
                className={INPUT} />
            </Field>
          </Section>

          <Section title="Goals — Nutrition">
            <Field label="Protein (g / day)" hint="Leave blank to auto-calculate from weight + training goal">
              <input type="number" step="5" value={proteinGoal}
                onChange={(e) => setProteinGoal(e.target.value)}
                placeholder="Auto"
                className={INPUT} />
            </Field>
            <Field label="Fiber (g / day)" hint="Leave blank to auto-calculate at 14 g per 1,000 kcal">
              <input type="number" step="1" value={fiberGoal}
                onChange={(e) => setFiberGoal(e.target.value)}
                placeholder="Auto"
                className={INPUT} />
            </Field>
            <Field label="Calories">
              <div className="mb-2 flex gap-1">
                {[["fixed", "Fixed"], ["balance", "Balance"], ["pace", "Pace"]].map(([v, lbl]) => (
                  <button key={v} type="button"
                    onClick={() => setCalorieMode(v)}
                    className={`flex-1 rounded-lg px-2 py-1.5 text-xs font-medium transition-colors ${
                      calorieMode === v ? "bg-indigo-600 text-white" : "bg-slate-700 text-slate-400 hover:text-white"
                    }`}>
                    {lbl}
                  </button>
                ))}
              </div>
              {calorieMode === "fixed" && (
                <input type="number" step="50" value={calorieGoal}
                  onChange={(e) => setCalorieGoal(e.target.value)}
                  placeholder="Not set (kcal / day)"
                  className={INPUT} />
              )}
              {calorieMode === "balance" && (
                <p className="rounded-lg bg-slate-700/50 px-3 py-2 text-xs text-slate-300">
                  Goal = estimated daily Out (RMR + active calories). Each day's target changes with your activity. Surplus/deficit shown vs. burn.
                </p>
              )}
              {calorieMode === "pace" && (
                <div className="space-y-2">
                  <input type="number" step="0.25" value={weightChangePace}
                    onChange={(e) => setWeightChangePace(e.target.value)}
                    placeholder="lbs / week (e.g. −0.5 to lose, +0.5 to gain)"
                    className={INPUT} />
                  <p className="text-xs text-slate-500">
                    Target = Out + daily offset (±500 kcal per lb/week). Goal changes each day with your activity.
                    {weightChangePace !== "" && !isNaN(parseFloat(weightChangePace)) && (
                      <> Daily offset: <span className="text-slate-300">{Math.round(parseFloat(weightChangePace) * 500) > 0 ? "+" : ""}{Math.round(parseFloat(weightChangePace) * 500)} kcal</span>.</>
                    )}
                  </p>
                </div>
              )}
            </Field>
          </Section>

          <Section title="Goals — Habits">
            <Field label="No caffeine after">
              <select value={caffeineHour} onChange={(e) => setCaffeineHour(Number(e.target.value))}
                className={INPUT}>
                {HOURS.filter(h => h.value >= 10 && h.value <= 20).map(h => (
                  <option key={h.value} value={h.value}>{h.label}</option>
                ))}
              </select>
            </Field>
            <Field label="Outdoor light before">
              <select value={lightHour} onChange={(e) => setLightHour(Number(e.target.value))}
                className={INPUT}>
                {HOURS.filter(h => h.value >= 6 && h.value <= 14).map(h => (
                  <option key={h.value} value={h.value}>{h.label}</option>
                ))}
              </select>
            </Field>
          </Section>

          {/* ── Training ──────────────────────────────────────────────────── */}
          <Section title="Training">
            <Field label="Max HR (bpm)">
              <input type="number" value={maxHr}
                onChange={(e) => setMaxHr(e.target.value)}
                className={INPUT} />
            </Field>
            <Field label="Training goal">
              <select value={goal} onChange={(e) => setGoal(e.target.value)} className={INPUT}>
                <option value="building">Building</option>
                <option value="maintaining">Maintaining</option>
                <option value="cutting">Cutting</option>
                <option value="peaking">Peaking</option>
              </select>
            </Field>
          </Section>

          {/* ── Save / cancel ─────────────────────────────────────────────── */}
          {saveMutation.isError && <p className="text-sm text-rose-400">Failed to save</p>}
          <div className="flex gap-3">
            <button onClick={onClose}
              className="flex-1 rounded-lg bg-slate-700 py-2 text-slate-300 hover:bg-slate-600 transition-colors">
              Cancel
            </button>
            <button
              onClick={() => saveMutation.mutate()}
              disabled={saveMutation.isPending}
              className="flex-1 rounded-lg bg-indigo-600 py-2 font-semibold text-white disabled:opacity-50">
              {saveMutation.isPending ? "Saving…" : "Save"}
            </button>
          </div>

          {/* ── Claude AI toggle ───────────────────────────────────────────── */}
          <div className="border-t border-slate-700 pt-5">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-xs font-medium text-slate-400">Claude AI Daily Summary</p>
                <p className="text-xs text-slate-500 mt-0.5 leading-relaxed">
                  Generates a plain-English readiness summary each morning at 8:05 AM.
                </p>
              </div>
              <button
                type="button"
                onClick={() => setAiEnabled(v => !v)}
                className={`relative ml-4 flex-shrink-0 h-6 w-11 rounded-full transition-colors duration-200 focus:outline-none ${
                  aiEnabled ? "bg-indigo-600" : "bg-slate-600"
                }`}
              >
                <span className={`absolute top-0.5 left-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform duration-200 ${
                  aiEnabled ? "translate-x-5" : "translate-x-0"
                }`} />
              </button>
            </div>
            {usage && (
              <div className="mt-2 flex gap-4 text-xs text-slate-600">
                <span>This month: {usage.this_month.calls} calls · ${usage.this_month.cost_usd.toFixed(3)}</span>
                <span>All time: ${usage.all_time.cost_usd.toFixed(3)}</span>
              </div>
            )}
          </div>

          {/* ── Data health (moved here from Insights → Analysis 2026-11-04, note 99: it is
              the app checking itself — input freshness, errors, citations, cache age) ── */}
          <div className="border-t border-slate-700 pt-5">
            <p className="mb-1 text-xs font-medium text-slate-400">Data Health</p>
            <p className="mb-3 text-xs text-slate-500 leading-relaxed">
              Which inputs are arriving, errors this week, briefing citation checks and the
              correlation cache's age.
            </p>
            <button onClick={() => setShowHealth(true)}
              className="w-full rounded-lg bg-slate-700 py-2 text-sm font-medium text-slate-300 hover:bg-slate-600 transition-colors">
              Open Data Health
            </button>
          </div>

          {/* ── Recompute ─────────────────────────────────────────────────── */}
          <div className="border-t border-slate-700 pt-5">
            <p className="mb-1 text-xs font-medium text-slate-400">Recalculate History</p>
            <p className="mb-3 text-xs text-slate-500 leading-relaxed">
              Recomputes all readiness scores, sleep debt, and training load from scratch.
              Run this after changing your sleep target or max HR.
            </p>
            <button
              onClick={() => recomputeMutation.mutate()}
              disabled={recomputeMutation.isPending}
              className="w-full rounded-lg bg-slate-700 py-2 text-sm font-medium text-amber-400 hover:bg-slate-600 disabled:opacity-50 transition-colors"
            >
              {recomputeMutation.isPending ? "Recalculating… (may take ~30s)" : "Recalculate All History"}
            </button>
            {recomputeMutation.isError && (
              <p className="mt-2 text-xs text-rose-400">Recalculation failed — check server logs</p>
            )}
          </div>

          {/* ── Sign out (moved here from the header 2026-10-21, backlog #13: rarely used,
              and a full-text button was the widest thing in the header) ───────── */}
          {onLogout && (
            <div className="border-t border-slate-700 pt-5">
              <button onClick={onLogout}
                className="w-full rounded-lg bg-slate-700 py-2 text-sm font-medium text-slate-300 hover:bg-slate-600 transition-colors">
                Sign out
              </button>
            </div>
          )}

        </div>
      </div>
      {showProfile && <ProfileModal onClose={() => setShowProfile(false)} />}
      {showHealth && (
        <div className="fixed inset-0 z-[60] overflow-y-auto bg-canvas" onClick={(e) => e.stopPropagation()}>
          <div className="mx-auto w-full max-w-md px-4 pb-12 pt-6">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-[17px] font-semibold text-ink">Data health</h2>
              <button onClick={() => setShowHealth(false)} className="text-[13px] text-ink-3 hover:text-ink">Done</button>
            </div>
            <DataHealth />
          </div>
        </div>
      )}
    </div>
  );
}
