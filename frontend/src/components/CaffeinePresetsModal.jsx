import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";
import PresetMacros, { cleanMacros, macroSummary } from "./PresetMacros.jsx";

// Editor for the caffeine preset list — recalibrate mg to what you actually
// drink, rename, add, or remove. Saving writes the custom list; Reset restores
// the built-in defaults.
export default function CaffeinePresetsModal({ presets, defaults, onClose }) {
  const qc = useQueryClient();
  const [rows, setRows] = useState(() => presets.map((p) => ({ ...p })));
  const [openMacros, setOpenMacros] = useState(null);   // index whose macros are expanded

  const save = useMutation({
    mutationFn: (list) => api.saveCaffeinePresets(list),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["caffeine-presets"] });
      qc.invalidateQueries({ queryKey: ["water-day"] });   // oz re-values past entries' fluid
      onClose();
    },
  });

  const update = (i, field, val) =>
    setRows((r) => r.map((p, j) => (j === i ? { ...p, [field]: val } : p)));
  const remove = (i) => setRows((r) => r.filter((_, j) => j !== i));
  const add = () => setRows((r) => [...r, {
    id: `custom_${Date.now()}`, label: "New", emoji: "☕", mg: 100, kcal: 0, oz: "",
  }]);

  const commit = () => {
    const clean = rows
      .filter((p) => p.label.trim() && Number(p.mg) > 0)
      // kcal and oz MUST be carried through: this list is written back wholesale, so
      // any field omitted here is silently erased for every preset. oz = drink volume,
      // for total fluids (note 78); blank means unknown, not zero.
      .map((p) => ({ id: p.id, label: p.label.trim(), emoji: p.emoji || "☕",
                     mg: Number(p.mg), kcal: Number(p.kcal) || 0,
                     ...(Number(p.oz) > 0 ? { oz: Number(p.oz) } : {}),
                     ...(cleanMacros(p.macros) ? { macros: cleanMacros(p.macros) } : {}) }));
    save.mutate(clean);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-[#0f172a]/95">
      <div className="w-full max-w-sm px-5 pt-10 pb-10">
        <div className="mb-1 flex items-center justify-between">
          <h2 className="text-lg font-bold text-white">Edit caffeine presets</h2>
          <button onClick={onClose} className="text-sm text-slate-400 hover:text-white">Cancel</button>
        </div>
        <p className="mb-4 text-xs text-slate-500">
          Set mg to match what you actually drink. Calories feed a Nutrition
          "drinks" entry automatically — leave a black coffee at 0 and none is created.
          oz is the drink's volume (liquid, not cup size): it counts toward your fluids,
          and changing it re-values every past entry of that preset.
        </p>

        <div className="space-y-2.5">
          {rows.map((p, i) => (
            <div key={p.id}>
            {/* Two lines (2026-10-25): with oz added, one line left the label ~2 chars wide. */}
            <div className="flex items-center gap-1.5">
              <input value={p.emoji} onChange={(e) => update(i, "emoji", e.target.value)}
                className="w-8 rounded-lg bg-slate-700 px-1 py-1.5 text-center text-sm text-white outline-none" />
              <input value={p.label} onChange={(e) => update(i, "label", e.target.value)}
                className="min-w-0 flex-1 rounded-lg bg-slate-700 px-2 py-1.5 text-xs text-white outline-none" />
              <button onClick={() => remove(i)} className="text-slate-600 hover:text-rose-400 text-sm">✕</button>
            </div>
            <div className="mt-1 flex items-center gap-1.5 pl-[38px]">
              <input type="number" inputMode="numeric" value={p.mg}
                onChange={(e) => update(i, "mg", e.target.value)}
                className="w-14 rounded-lg bg-slate-700 px-2 py-1.5 text-xs text-white outline-none" />
              <span className="text-[10px] text-slate-500">mg</span>
              <input type="number" inputMode="numeric" value={p.kcal ?? 0}
                onChange={(e) => update(i, "kcal", e.target.value)}
                className="w-14 rounded-lg bg-slate-700 px-2 py-1.5 text-xs text-white outline-none" />
              <span className="text-[10px] text-slate-500">kcal</span>
              <input type="number" inputMode="decimal" value={p.oz ?? ""} placeholder="?"
                onChange={(e) => update(i, "oz", e.target.value)}
                className="w-12 rounded-lg bg-slate-700 px-2 py-1.5 text-xs text-white outline-none" />
              <span className="text-[10px] text-slate-500">oz</span>
              <button onClick={() => setOpenMacros(openMacros === i ? null : i)}
                title={macroSummary(p.macros)}
                className={`ml-auto text-sm ${p.macros ? "text-indigo-400" : "text-slate-600"} hover:text-indigo-300`}>
                {openMacros === i ? "▾" : "▸"}
              </button>
            </div>
            {openMacros === i && (
              <PresetMacros macros={p.macros} onChange={(m) => update(i, "macros", m)} />
            )}
            </div>
          ))}
        </div>

        <button onClick={add} className="mt-2 text-xs text-indigo-400 hover:text-indigo-300">+ Add preset</button>

        <div className="mt-6 flex items-center gap-2">
          <button onClick={commit} disabled={save.isPending}
            className="flex-1 rounded-xl bg-indigo-600 py-2.5 text-sm font-semibold text-white hover:bg-indigo-500 disabled:opacity-50">
            {save.isPending ? "Saving…" : "Save"}
          </button>
          <button onClick={() => save.mutate(null)} disabled={save.isPending}
            className="rounded-xl bg-slate-700 px-3 py-2.5 text-xs text-slate-300 hover:bg-slate-600"
            title="Restore the built-in defaults">
            Reset
          </button>
        </div>
      </div>
    </div>
  );
}
