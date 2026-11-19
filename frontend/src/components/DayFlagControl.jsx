import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";

// User-selectable reasons (incomplete_data is auto-only, shown read-only if present).
const REASONS = [
  { key: "travel",         label: "Traveling",  emoji: "✈️" },
  // Auto-added for 4 days after a trip of 3+ days; listed so it can be removed.
  { key: "travel_recovery", label: "Jet lag",   emoji: "🕐" },
  { key: "sick",           label: "Sick",       emoji: "🤒" },
  { key: "poor_sleep_env", label: "Bad sleep env", emoji: "🛏️" },
  { key: "high_stress",    label: "High stress", emoji: "😣" },
  { key: "other",          label: "Other",      emoji: "🚩" },
];

// Compact control to flag a single day as nonstandard (excluded from experiments
// + correlations). Reused on Today and (later) the history calendar.
// Collapsed by default (most days are standard); expands to the reason chips.
// `openClassName` is applied only while expanded — LOG puts it in a 2-column grid beside
// the nap button and uses "col-span-2" so the reason chips get the full width (note 82).
export default function DayFlagControl({ date, compact = false, defaultOpen = false, openClassName = "" }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(defaultOpen);
  const { data } = useQuery({
    queryKey: ["day-flags", date],
    queryFn: () => api.getDayFlags(date, date),
  });
  const flags = data?.flags || [];
  const active = new Set(flags.map((f) => f.reason));
  const autoReasons = new Set(flags.filter((f) => f.source === "auto").map((f) => f.reason));

  const toggle = useMutation({
    mutationFn: ({ reason, on }) =>
      on ? api.setDayFlag(date, reason) : api.clearDayFlag(date, reason),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["day-flags"] });
      qc.invalidateQueries({ queryKey: ["today"] });
    },
  });

  const anyFlag = active.size > 0;
  const activeLabels = flags.map((f) => REASONS.find((r) => r.key === f.reason)?.label || f.label).join(" · ");

  // Collapsed header: subtle on normal days, amber when the day is flagged.
  if (!open) {
    return (
      <div className={compact ? "" : "rounded-xl bg-slate-800 p-3"}>
        <button onClick={() => setOpen(true)} className="flex w-full items-center gap-2 text-left">
          <span className="text-sm">{anyFlag ? "🚩" : "🏳️"}</span>
          {anyFlag ? (
            <>
              <span className="flex-1 truncate text-xs text-amber-400">{activeLabels}</span>
              <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-amber-400">
                excluded
              </span>
            </>
          ) : (
            <span className="flex-1 truncate text-xs text-slate-500">Flag day (travel, sick, …)</span>
          )}
          <span className="text-xs text-slate-600">▸</span>
        </button>
      </div>
    );
  }

  return (
    <div className={`${compact ? "" : "rounded-xl bg-slate-800 p-3"} ${openClassName}`}>
      <button onClick={() => setOpen(false)} className="mb-2 flex w-full items-center gap-2">
        <p className="text-xs font-medium uppercase tracking-widest text-slate-400">Day flags</p>
        {anyFlag && (
          <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-amber-400">
            excluded from analysis
          </span>
        )}
        <span className="ml-auto text-xs text-slate-600">▾</span>
      </button>
      <div className="flex flex-wrap gap-1.5">
        {REASONS.map(({ key, label, emoji }) => {
          const on = active.has(key);
          const isAuto = autoReasons.has(key);
          return (
            <button
              key={key}
              onClick={() => toggle.mutate({ reason: key, on: !on })}
              disabled={toggle.isPending}
              title={isAuto ? "Auto-detected — tap to override" : undefined}
              className="rounded-lg px-2.5 py-1 text-xs font-medium transition-colors disabled:opacity-50"
              style={{
                color: on ? "#fbbf24" : "#64748b",
                backgroundColor: on ? "#fbbf2422" : "#1e293b",
                border: `1.5px solid ${on ? "#fbbf24" : "#334155"}`,
              }}
            >
              {emoji} {label}{on ? (isAuto ? " ⚙︎" : " ✓") : ""}
            </button>
          );
        })}
      </div>
      {autoReasons.size > 0 && (
        <p className="mt-1.5 text-[10px] text-slate-600">⚙︎ auto-detected — tap to override</p>
      )}
    </div>
  );
}
