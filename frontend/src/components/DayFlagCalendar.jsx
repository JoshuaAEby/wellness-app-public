import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api.js";
import DayFlagControl from "./DayFlagControl.jsx";

const WEEKDAYS = ["S", "M", "T", "W", "T", "F", "S"];
const MONTHS = ["January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December"];

const pad = (n) => String(n).padStart(2, "0");
const iso = (y, m, d) => `${y}-${pad(m + 1)}-${pad(d)}`;

// Modal calendar to flag PAST days as nonstandard. Marks days that already carry
// a flag; tapping a day opens the shared DayFlagControl for it.
export default function DayFlagCalendar({ onClose }) {
  const now = new Date();
  const todayIso = iso(now.getFullYear(), now.getMonth(), now.getDate());
  const [view, setView] = useState({ y: now.getFullYear(), m: now.getMonth() });
  const [selected, setSelected] = useState(todayIso);

  const monthStart = iso(view.y, view.m, 1);
  const daysInMonth = new Date(view.y, view.m + 1, 0).getDate();
  const monthEnd = iso(view.y, view.m, daysInMonth);
  const firstWeekday = new Date(view.y, view.m, 1).getDay();

  const { data } = useQuery({
    queryKey: ["day-flags-month", view.y, view.m],
    queryFn: () => api.getDayFlags(monthStart, monthEnd),
  });
  const flaggedDates = new Set((data?.flags || []).map((f) => f.date));

  // Don't let the user page into the future.
  const atCurrentMonth = view.y === now.getFullYear() && view.m === now.getMonth();
  const shift = (delta) => {
    const d = new Date(view.y, view.m + delta, 1);
    if (d > now && delta > 0) return;
    setView({ y: d.getFullYear(), m: d.getMonth() });
  };

  const cells = [];
  for (let i = 0; i < firstWeekday; i++) cells.push(null);
  for (let d = 1; d <= daysInMonth; d++) cells.push(d);

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-[#0f172a]/95">
      <div className="w-full max-w-sm px-5 pt-10 pb-10">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-bold text-white">Flag past days</h2>
          <button onClick={onClose} className="text-sm text-slate-400 hover:text-white">Done</button>
        </div>
        <p className="mb-4 text-xs text-slate-500">
          Mark travel, sick, or other nonstandard days so they're excluded from your experiments and correlations.
        </p>

        <div className="rounded-xl bg-slate-800 p-3">
          {/* Month nav */}
          <div className="mb-2 flex items-center justify-between">
            <button onClick={() => shift(-1)} className="px-2 py-1 text-slate-400 hover:text-white">‹</button>
            <p className="text-sm font-medium text-white">{MONTHS[view.m]} {view.y}</p>
            <button
              onClick={() => shift(1)}
              disabled={atCurrentMonth}
              className="px-2 py-1 text-slate-400 hover:text-white disabled:opacity-30"
            >›</button>
          </div>

          {/* Weekday header */}
          <div className="mb-1 grid grid-cols-7 gap-1 text-center">
            {WEEKDAYS.map((w, i) => (
              <div key={i} className="text-[10px] font-medium text-slate-600">{w}</div>
            ))}
          </div>

          {/* Day grid */}
          <div className="grid grid-cols-7 gap-1">
            {cells.map((d, i) => {
              if (d === null) return <div key={`b${i}`} />;
              const date = iso(view.y, view.m, d);
              const isFuture = date > todayIso;
              const isSel = date === selected;
              const flagged = flaggedDates.has(date);
              return (
                <button
                  key={date}
                  disabled={isFuture}
                  onClick={() => setSelected(date)}
                  className="relative aspect-square rounded-lg text-xs transition-colors disabled:opacity-25"
                  style={{
                    backgroundColor: isSel ? "#6366f1" : flagged ? "#fbbf2422" : "#1e293b",
                    color: isSel ? "#fff" : flagged ? "#fbbf24" : "#cbd5e1",
                    border: date === todayIso ? "1.5px solid #64748b" : "1.5px solid transparent",
                  }}
                >
                  {d}
                  {flagged && !isSel && (
                    <span className="absolute bottom-1 left-1/2 h-1 w-1 -translate-x-1/2 rounded-full bg-amber-400" />
                  )}
                </button>
              );
            })}
          </div>
        </div>

        {/* Flags for the selected day */}
        <div className="mt-3">
          <p className="mb-1.5 px-1 text-xs text-slate-500">
            {new Date(selected + "T12:00:00").toLocaleDateString(undefined,
              { weekday: "long", month: "long", day: "numeric" })}
          </p>
          <DayFlagControl key={selected} date={selected} defaultOpen />
        </div>
      </div>
    </div>
  );
}
