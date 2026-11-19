import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";

/**
 * Editor for the water preset list.
 *
 * Water used to be one "Refill" button sized from a field buried in Settings, which
 * made it the odd one out next to caffeine and alcohol — and a glass at a restaurant
 * is not a refill of your bottle. Same shape as the other two now.
 */
export default function WaterPresetsModal({ presets, onClose }) {
  const qc = useQueryClient();
  const [rows, setRows] = useState(() => presets.map((p) => ({ ...p })));

  const save = useMutation({
    mutationFn: (list) => api.saveWaterPresets(list),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["water-day"] });
      qc.invalidateQueries({ queryKey: ["water-presets"] });
      onClose();
    },
  });

  const update = (i, field, val) =>
    setRows((r) => r.map((p, j) => (j === i ? { ...p, [field]: val } : p)));
  const remove = (i) => setRows((r) => r.filter((_, j) => j !== i));
  const add = () => setRows((r) => [...r, {
    id: `custom_${Date.now()}`, label: "New", emoji: "💧", oz: 12,
  }]);

  const commit = () => save.mutate(
    rows.filter((p) => String(p.label).trim() && Number(p.oz) > 0)
        .map((p) => ({ id: p.id, label: String(p.label).trim(),
                       emoji: p.emoji || "💧", oz: Number(p.oz) })));

  return (
    <div className="fixed inset-0 z-[60] flex items-start justify-center overflow-y-auto bg-[#0f172a]/95">
      <div className="w-full max-w-sm px-5 pt-10 pb-10">
        <div className="mb-1 flex items-center justify-between">
          <h2 className="text-lg font-bold text-white">Edit water presets</h2>
          <button onClick={onClose} className="text-sm text-slate-400 hover:text-white">Cancel</button>
        </div>
        <p className="mb-4 text-xs text-slate-500">
          One tap logs the size shown. Your bottle is just the first preset.
        </p>

        <div className="space-y-1.5">
          {rows.map((p, i) => (
            <div key={p.id} className="flex items-center gap-1.5">
              <input value={p.emoji} onChange={(e) => update(i, "emoji", e.target.value)}
                className="w-8 rounded-lg bg-slate-700 px-1 py-1.5 text-center text-sm text-white outline-none" />
              <input value={p.label} onChange={(e) => update(i, "label", e.target.value)}
                className="min-w-0 flex-1 rounded-lg bg-slate-700 px-2 py-1.5 text-xs text-white outline-none" />
              <input type="number" inputMode="decimal" value={p.oz}
                onChange={(e) => update(i, "oz", e.target.value)}
                className="w-16 rounded-lg bg-slate-700 px-2 py-1.5 text-xs text-white outline-none" />
              <span className="text-[10px] text-slate-500">oz</span>
              <button onClick={() => remove(i)} className="text-slate-600 hover:text-rose-400 text-sm">✕</button>
            </div>
          ))}
        </div>

        <button onClick={add} className="mt-2 text-xs text-indigo-400 hover:text-indigo-300">+ Add preset</button>

        <div className="mt-6 flex items-center gap-2">
          <button onClick={commit} disabled={save.isPending}
            className="flex-1 rounded-lg bg-emerald-600 py-2 text-sm font-medium text-white disabled:opacity-50">
            Save
          </button>
          <button onClick={() => save.mutate(null)} disabled={save.isPending}
            className="rounded-lg bg-slate-700 px-3 py-2 text-sm text-slate-300">Reset</button>
        </div>
      </div>
    </div>
  );
}
