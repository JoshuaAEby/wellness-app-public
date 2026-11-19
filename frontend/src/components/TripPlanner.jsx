import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";

const fmt = (d) => new Date(d + "T12:00:00").toLocaleDateString(undefined,
  { month: "short", day: "numeric" });

// Trips = runs of `travel` day flags (note 72). Entering one ahead of time stands the
// app down for those days: no check-in takeovers, meal or backfill reminders, steps /
// coach / freshness pushes, or experiment draws; logging-dependent streaks are held.
// Whatever IS logged while away still counts. A trip of 3+ days earns a 4-day
// "back from a trip" buffer, excluded from analysis but otherwise a normal day.
export default function TripPlanner({ onClose }) {
  const qc = useQueryClient();
  const now = new Date();
  const todayIso = new Date(now.getTime() - now.getTimezoneOffset() * 60000)
    .toISOString().slice(0, 10);
  const [start, setStart] = useState(todayIso);
  const [end, setEnd] = useState(todayIso);
  const [tzName, setTzName] = useState("");

  const { data } = useQuery({ queryKey: ["trips"], queryFn: api.getTrips });
  const trips = data?.trips || [];

  const invalidate = () => {
    ["trips", "day-flags", "day-flags-month", "today", "nutrition-incomplete",
     "streaks", "habits"].forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
  };
  const add = useMutation({ mutationFn: () => api.addTrip(start, end, null, tzName.trim()),
    onSuccess: () => { setTzName(""); invalidate(); } });
  const remove = useMutation({
    mutationFn: ({ start, end }) => api.removeTrip(start, end), onSuccess: invalidate,
  });
  // Note 102: tick what wasn't logged on a trip, so those days read as unknown.
  const gapsSave = useMutation({
    mutationFn: ({ t, items }) => api.setTripUnlogged(t.start, t.end, items),
    onSuccess: () => ["trips", "trip-gaps", "streaks", "habits", "levers"]
      .forEach((k) => qc.invalidateQueries({ queryKey: [k] })),
  });
  const toggleGap = (t, item) => {
    const cur = new Set(t.unlogged || []);
    cur.has(item) ? cur.delete(item) : cur.add(item);
    gapsSave.mutate({ t, items: [...cur] });
  };

  const valid = start && end && end >= start;

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-[#0f172a]/95">
      <div className="w-full max-w-sm px-5 pt-10 pb-10">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-bold text-white">Trips</h2>
          <button onClick={onClose} className="text-sm text-slate-400 hover:text-white">Done</button>
        </div>
        <p className="mb-4 text-xs text-slate-500">
          Days away are excluded from correlations and experiments, and the app stops
          reminding and notifying you. Anything you do log still counts.
        </p>

        <div className="rounded-xl bg-slate-800 p-3">
          <p className="mb-2 text-xs font-medium uppercase tracking-widest text-slate-400">Add a trip</p>
          <div className="grid grid-cols-2 gap-2">
            <label className="text-[11px] text-slate-500">
              Leave
              <input type="date" value={start} onChange={(e) => setStart(e.target.value)}
                className="mt-1 w-full rounded-lg bg-slate-700 px-2 py-1.5 text-sm text-white" />
            </label>
            <label className="text-[11px] text-slate-500">
              Back
              <input type="date" value={end} min={start} onChange={(e) => setEnd(e.target.value)}
                className="mt-1 w-full rounded-lg bg-slate-700 px-2 py-1.5 text-sm text-white" />
            </label>
          </div>
          <label className="mt-2 block text-[11px] text-slate-500">
            Time zone there (optional)
            <input type="text" value={tzName} onChange={(e) => setTzName(e.target.value)}
              placeholder="+03:00 or Europe/Istanbul"
              className="mt-1 w-full rounded-lg bg-slate-700 px-2 py-1.5 text-sm text-white placeholder:text-slate-500" />
          </label>
          <button
            onClick={() => add.mutate()}
            disabled={!valid || add.isPending}
            className="mt-3 w-full rounded-lg bg-indigo-600 py-2 text-sm font-semibold text-white disabled:opacity-40"
          >
            {add.isPending ? "Saving…" : "Mark as away"}
          </button>
          {add.isError && <p className="mt-2 text-xs text-rose-400">{String(add.error?.message || add.error)}</p>}
        </div>

        {trips.length > 0 && (
          <div className="mt-4 space-y-2">
            {trips.map((t) => (
              <div key={t.start} className="flex items-center gap-2 rounded-xl bg-slate-800 px-3 py-2">
                <div className="flex-1">
                  <p className="text-sm text-white">
                    ✈️ {fmt(t.start)} – {fmt(t.end)}
                    <span className="ml-1.5 text-xs text-slate-500">{t.days} d</span>
                  </p>
                  <p className="text-[11px] text-slate-500">
                    {t.auto_days > 0 && t.manual_days === 0 ? "auto-detected (slept away)" :
                     t.auto_days > 0 ? `${t.auto_days} day(s) auto-detected` : "entered by you"}
                    {t.recovery_days.length > 0 && ` · jet-lag buffer to ${fmt(t.recovery_days.at(-1))}`}
                  </p>
                  <div className="mt-1.5 flex flex-wrap items-center gap-1">
                    <span className="text-[10.5px] text-slate-500">
                      {t.unlogged == null ? "Didn't log?" : "Didn't log:"}
                    </span>
                    {(data?.gap_items || []).map((item) => {
                      const on = (t.unlogged || []).includes(item);
                      return (
                        <button key={item} onClick={() => toggleGap(t, item)} disabled={gapsSave.isPending}
                          className={`rounded-full px-2 py-0.5 text-[10.5px] ${on
                            ? "bg-amber-500/20 text-amber-300" : "bg-slate-700 text-slate-400"}`}>
                          {on ? "✕ " : ""}{item}
                        </button>
                      );
                    })}
                    {t.unlogged == null && (
                      <button onClick={() => gapsSave.mutate({ t, items: [] })} disabled={gapsSave.isPending}
                        className="rounded-full bg-slate-700 px-2 py-0.5 text-[10.5px] text-emerald-300">
                        ✓ logged everything
                      </button>
                    )}
                  </div>
                </div>
                <button
                  onClick={() => remove.mutate({ start: t.start, end: t.end })}
                  disabled={remove.isPending}
                  className="text-xs text-slate-500 hover:text-rose-400"
                >Remove</button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
