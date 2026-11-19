import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";
import { SliderPicker } from "./MorningCheckinModal.jsx";
import { HABITS } from "../tabs/behaviors/grid.jsx";

// Manual habits, toggled here at the end of the day (note 64). Same definitions and
// the SAME write paths as Behaviors → Record, so the two surfaces can never disagree
// or double-count.
async function writeHabit(date, key, value) {
  return api.logHabits({ date, [key]: value });
}

function localToday() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// Evening takeover (mirrors the morning check-in): first app open at/after the
// evening hour prompts a single day-quality rating. This is a retrospective of
// the WHOLE day — a lag-0 outcome, distinct from the morning scores which
// measure overnight recovery.
export default function EveningCheckinModal({ onDone, onSkip }) {
  const [dayScore, setDayScore] = useState(null);
  const qc = useQueryClient();
  const today = localToday();

  // Pre-filled from today's record, so anything already ticked in Record shows on.
  const { data: dayRows = [] } = useQuery({
    queryKey: ["habits-calendar", today, today],
    queryFn: () => api.getHabitsCalendar(today, today),
  });
  const initial = dayRows[0] || {};
  const toggles = HABITS.filter((h) => h.manual);
  // Only keys the user TOUCHED are written. Writing every toggle would turn "not set"
  // into an explicit "missed" for each untouched manual habit, every single night.
  const [picked, setPicked] = useState({});
  const isOn = (key) => picked[key] ?? initial[key] === true;

  const submit = useMutation({
    mutationFn: async (payload) => {
      await api.logWellbeing({ date: payload.date, day_score: payload.day_score });
      const changed = Object.entries(picked).filter(([k, v]) => v !== (initial[k] === true));
      await Promise.all(changed.map(([k, v]) => writeHabit(payload.date, k, v)));
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["today"] });
      qc.invalidateQueries({ queryKey: ["habits-calendar"] });
      qc.invalidateQueries({ queryKey: ["habit-goals"] });
      onDone();
    },
  });

  function handleSubmit(e) {
    e.preventDefault();
    if (dayScore === null || submit.isPending) return;
    submit.mutate({
      date: localToday(),
      day_score: dayScore,
    });
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-[#0f172a] overflow-y-auto">
      <div className="w-full max-w-sm px-6 pt-14 pb-10 space-y-6">

        <div>
          <p className="text-xs font-semibold uppercase tracking-widest text-violet-400 mb-1">
            Evening check-in
          </p>
          <h2 className="text-2xl font-bold text-white">How was your day?</h2>
          <p className="mt-1.5 text-sm text-slate-400">
            One rating for the day as a whole — takes 5 seconds.
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-6">
          <div>
            <p className="text-sm font-medium text-slate-300 mb-2.5">🌆 Day quality</p>
            <SliderPicker
              value={dayScore}
              onChange={setDayScore}
              lowLabel="rough"
              highLabel="great"
            />
          </div>

          {toggles.length > 0 && (
            <div>
              <p className="text-sm font-medium text-slate-300 mb-2.5">Today you…</p>
              <div className="grid grid-cols-2 gap-2">
                {toggles.map((h) => {
                  const on = isOn(h.key);
                  return (
                    <button
                      key={h.key}
                      type="button"
                      onClick={() => setPicked((p) => ({ ...p, [h.key]: !on }))}
                      aria-pressed={on}
                      className="flex items-center gap-2 rounded-xl px-3 py-2.5 text-left text-sm transition-colors"
                      style={{
                        background: on ? `color-mix(in srgb, ${h.color} 22%, transparent)` : "rgba(51,65,85,.55)",
                        border: `1px solid ${on ? h.color : "transparent"}`,
                        color: on ? "#fff" : "#94a3b8",
                      }}
                    >
                      <span>{h.emoji}</span>
                      <span className="flex-1">{h.label}</span>
                      <span className="text-xs">{on ? "✓" : ""}</span>
                    </button>
                  );
                })}
              </div>
              <p className="mt-2 text-[11px] text-slate-500">
                Tap what applies. Untouched stays unlogged; you can change any day in Habits → Record.
              </p>
            </div>
          )}

          {submit.isError && (
            <p className="text-xs text-rose-400">Failed to save — tap Done to try again.</p>
          )}

          <button
            type="submit"
            disabled={dayScore === null || submit.isPending}
            className="w-full rounded-xl bg-violet-600 py-3.5 text-sm font-semibold text-white transition hover:bg-violet-500 disabled:opacity-40"
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
            Skip tonight →
          </button>
        </div>

      </div>
    </div>
  );
}
