import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";

export function scaleColor(v, invert = false) {
  const x = invert ? 6 - v : v;
  if (x < 2.5) return "#f43f5e";
  if (x < 3.5) return "#f59e0b";
  return "#10b981";
}

// Continuous 1.0–5.0 slider (0.1 steps). Same scale and anchors as the old 1–5
// buttons — a monotone refinement, so history stays comparable and rank-based
// stats are unaffected. Value is null (dash, grey) until first touched.
export function SliderPicker({ value, onChange, invert = false, lowLabel = "poor", highLabel = "great" }) {
  const active = value !== null;
  const color = active ? scaleColor(value, invert) : "#475569";
  return (
    <div>
      <div className="flex items-center gap-3">
        <input
          type="range"
          min="1"
          max="5"
          step="0.1"
          value={value ?? 3}
          onChange={e => onChange(parseFloat(e.target.value))}
          className="flex-1 h-2 rounded-full appearance-none cursor-pointer"
          style={{
            accentColor: color,
            background: active
              ? `linear-gradient(to right, ${color}66, ${color})`
              : "#1e293b",
          }}
        />
        <button
          type="button"
          onClick={() => onChange(value ?? 3)}
          className="w-12 text-right text-xl font-bold tabular-nums"
          style={{ color }}
        >
          {active ? value.toFixed(1) : "–"}
        </button>
      </div>
      <div className="flex justify-between mt-1 px-0.5">
        <span className="text-xs text-slate-600">{lowLabel}</span>
        <span className="text-xs text-slate-600">{highLabel}</span>
      </div>
    </div>
  );
}

export default function MorningCheckinModal({ onDone, onSkip }) {
  const [sleepQuality, setSleepQuality] = useState(null);
  const [energy, setEnergy]             = useState(null);
  const [mood, setMood]                 = useState(null);
  const [stress, setStress]             = useState(null);
  const [sick, setSick]                 = useState(false);
  const [soreness, setSoreness]         = useState("");
  const qc = useQueryClient();

  const submit = useMutation({
    mutationFn: api.logWellbeing,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["today"] });
      onDone();
    },
  });

  const canSubmit = sleepQuality !== null && energy !== null && mood !== null;

  function handleSubmit(e) {
    e.preventDefault();
    if (!canSubmit || submit.isPending) return;
    submit.mutate({
      wellbeing_score: Math.round(((sleepQuality + energy + mood) / 3) * 10) / 10,
      sleep_quality: sleepQuality,
      energy,
      mood,
      stress: stress ?? undefined,   // optional — omit rather than clear
      sick,
      soreness: soreness.trim() || null,
    });
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-[#0f172a] overflow-y-auto">
      <div className="w-full max-w-sm px-6 pt-14 pb-10 space-y-6">

        {/* Header */}
        <div>
          <p className="text-xs font-semibold uppercase tracking-widest text-indigo-400 mb-1">
            Morning check-in
          </p>
          <h2 className="text-2xl font-bold text-white">How are you feeling?</h2>
          <p className="mt-1.5 text-sm text-slate-400">
            Quick snapshot before you review your metrics — takes 15 seconds.
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-6">

          {/* Sleep quality */}
          <div>
            <p className="text-sm font-medium text-slate-300 mb-2.5">
              🌙 Sleep quality
            </p>
            <SliderPicker value={sleepQuality} onChange={setSleepQuality} />
          </div>

          {/* Energy */}
          <div>
            <p className="text-sm font-medium text-slate-300 mb-2.5">
              ⚡ Energy / fatigue
            </p>
            <SliderPicker value={energy} onChange={setEnergy} />
          </div>

          {/* Mood */}
          <div>
            <p className="text-sm font-medium text-slate-300 mb-2.5">
              😊 Mood
            </p>
            <SliderPicker value={mood} onChange={setMood} />
          </div>

          {/* Stress */}
          <div>
            <p className="text-sm font-medium text-slate-300 mb-2.5">
              🧠 Stress{" "}
              <span className="text-slate-500 font-normal">optional</span>
            </p>
            <SliderPicker
              value={stress}
              onChange={setStress}
              invert
              lowLabel="calm"
              highLabel="stressed"
            />
          </div>

          {/* Sick */}
          <button
            type="button"
            onClick={() => setSick(s => !s)}
            className="w-full rounded-xl py-2.5 text-sm font-medium transition-all"
            style={{
              color: sick ? "#f43f5e" : "#64748b",
              backgroundColor: sick ? "#f43f5e22" : "#1e293b",
              border: `1.5px solid ${sick ? "#f43f5e" : "#334155"}`,
            }}
          >
            🤒 {sick ? "Marked as a sick day (excluded from analyses)" : "Feeling sick? Tap to flag today"}
          </button>

          {/* Soreness */}
          <div>
            <p className="text-sm font-medium text-slate-300 mb-2.5">
              🤕 Any soreness?{" "}
              <span className="text-slate-500 font-normal">optional</span>
            </p>
            <input
              type="text"
              value={soreness}
              onChange={e => setSoreness(e.target.value)}
              placeholder="e.g. left quad, shoulders — or leave blank"
              className="w-full rounded-xl bg-slate-800 border border-slate-700 px-4 py-3 text-sm text-white placeholder-slate-600 outline-none focus:border-indigo-500 transition-colors"
            />
          </div>

          {submit.isError && (
            <p className="text-xs text-rose-400">Failed to save — tap Done to try again.</p>
          )}

          <button
            type="submit"
            disabled={!canSubmit || submit.isPending}
            className="w-full rounded-xl bg-indigo-600 py-3.5 text-sm font-semibold text-white transition hover:bg-indigo-500 disabled:opacity-40"
          >
            {submit.isPending ? "Saving…" : "Done"}
          </button>
        </form>

        <div className="text-center pt-1">
          <button
            type="button"
            onClick={onSkip ?? onDone}
            className="text-xs text-slate-600 hover:text-slate-400 transition-colors"
          >
            Skip for today →
          </button>
        </div>

      </div>
    </div>
  );
}
