import { useState } from "react";
import { api } from "../api.js";

// Karolinska Sleepiness Scale — the standard 9-point form.
//
// The anchors ARE the instrument. The numbers alone are not the scale, and rewording
// them breaks comparability with everything published using it, so they're reproduced
// verbatim and the server keeps its own copy for labelling history.
//
// Why this sits in the battery rather than in the morning check-in: its value is the
// DISSOCIATION from the objective tests taken at the same sitting. Being measurably
// impaired on the PVT while reporting feeling fine is a documented sleep-loss state and
// the genuinely dangerous one — you can only see it if both are timestamped together.
const ANCHORS = [
  [1, "Extremely alert"],
  [2, "Very alert"],
  [3, "Alert"],
  [4, "Rather alert"],
  [5, "Neither alert nor sleepy"],
  [6, "Some signs of sleepiness"],
  [7, "Sleepy, but no effort to keep awake"],
  [8, "Sleepy, some effort to keep awake"],
  [9, "Very sleepy, great effort to keep awake, fighting sleep"],
];

export default function KssTest({ onClose, onDone }) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(false);

  const pick = (score) => {
    if (saving) return;
    setSaving(true);
    setError(false);
    api.submitKss({
      score,
      device: navigator.userAgent.includes("iPhone") ? "iPhone"
            : navigator.userAgent.includes("Mac") ? "Mac" : "other",
      started_at: new Date().toISOString(),
    })
      .then((r) => onDone?.(r))
      .catch(() => { setError(true); setSaving(false); });
  };

  return (
    <div className="fixed inset-0 z-[60] flex flex-col bg-[#0f172a] px-6 py-8 overflow-y-auto">
      <p className="text-xs font-semibold uppercase tracking-widest text-indigo-400">
        Sleepiness
      </p>
      <h2 className="mt-2 text-xl font-bold text-white">
        How alert have you felt in the last 10 minutes?
      </h2>
      <p className="mt-2 text-xs leading-relaxed text-slate-500">
        Rate how you actually felt, not how you think you should have. This is most useful
        when it disagrees with the reaction test.
      </p>

      <div className="mt-5 space-y-1.5">
        {ANCHORS.map(([n, label]) => (
          <button
            key={n}
            onClick={() => pick(n)}
            disabled={saving}
            className="flex w-full items-center gap-3 rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-2.5 text-left active:bg-slate-800 disabled:opacity-40"
          >
            <span className="w-6 shrink-0 text-center text-base font-bold tabular-nums text-indigo-400">
              {n}
            </span>
            <span className="text-[13px] leading-snug text-slate-300">{label}</span>
          </button>
        ))}
      </div>

      {error && (
        <p className="mt-4 text-sm text-rose-400">Couldn't save that — tap a number again.</p>
      )}

      <button onClick={onClose} className="mt-6 py-2 text-sm text-slate-500">
        Skip
      </button>
    </div>
  );
}
