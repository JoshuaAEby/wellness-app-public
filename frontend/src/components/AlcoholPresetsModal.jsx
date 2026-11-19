import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";
import PresetMacros, { cleanMacros, macroSummary } from "./PresetMacros.jsx";

/**
 * Editor for the alcohol preset list.
 *
 * oz and ABV are stored rather than a fixed SDU so the conversion stays auditable —
 * change the pour size and the SDU follows, instead of two numbers drifting apart.
 * SDU shown live = oz × ABV% ÷ 0.6 (US standard drink = 0.6 fl oz ethanol).
 *
 * kcal seeds the linked Nutrition "drinks" entry; 0 means no entry is created.
 */
export default function AlcoholPresetsModal({ presets, onClose }) {
  const qc = useQueryClient();
  const [rows, setRows] = useState(() => presets.map((p) => ({ ...p })));
  const [openMacros, setOpenMacros] = useState(null);

  const save = useMutation({
    mutationFn: (list) => api.saveAlcoholPresets(list),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["alcohol-presets"] }); onClose(); },
  });

  const update = (i, field, val) =>
    setRows((r) => r.map((p, j) => (j === i ? { ...p, [field]: val } : p)));
  const remove = (i) => setRows((r) => r.filter((_, j) => j !== i));
  const add = () => setRows((r) => [...r, {
    id: `custom_${Date.now()}`, label: "New", emoji: "🍺", oz: 12, abv: 5, kcal: 150,
  }]);

  const sduOf = (p) => (Number(p.oz) * Number(p.abv) / 100) / 0.6;

  // Ethanol alone sets a calorie FLOOR: 7 kcal/g, and ethanol_g = oz × abv × 0.2333
  // (density 0.789 g/mL). Showing it makes an impossible kcal visible instead of
  // silent — a 16 oz 7% IPA cannot be 200 kcal, because the alcohol is already 183.
  const alcoholKcal = (p) => 7 * (Number(p.oz) * Number(p.abv) * 0.2333);
  const macroKcal = (p) => {
    const m = p.macros || {};
    return 4 * (Number(m.carbs_g) || 0) + 4 * (Number(m.protein_g) || 0)
         + 9 * (Number(m.fat_g) || 0);
  };

  const commit = () => {
    const clean = rows
      .filter((p) => String(p.label).trim() && Number(p.oz) > 0 && Number(p.abv) > 0)
      // Carry every field through — the list is written back wholesale, so anything
      // omitted here is erased for every preset.
      .map((p) => ({
        id: p.id, label: String(p.label).trim(), emoji: p.emoji || "🍺",
        oz: Number(p.oz), abv: Number(p.abv), kcal: Number(p.kcal) || 0,
        ...(String(p.brand || "").trim() ? { brand: String(p.brand).trim() } : {}),
        ...(cleanMacros(p.macros) ? { macros: cleanMacros(p.macros) } : {}),
      }));
    save.mutate(clean);
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-start justify-center overflow-y-auto bg-[#0f172a]/95">
      <div className="w-full max-w-sm px-5 pt-10 pb-10">
        <div className="mb-1 flex items-center justify-between">
          <h2 className="text-lg font-bold text-white">Edit alcohol presets</h2>
          <button onClick={onClose} className="text-sm text-slate-400 hover:text-white">Cancel</button>
        </div>
        <p className="mb-4 text-xs text-slate-500">
          Set pour size and strength — SDU follows automatically. Calories feed a
          Nutrition "drinks" entry; ▸ adds optional macros.
        </p>

        <div className="space-y-1.5">
          {rows.map((p, i) => (
            <div key={p.id}>
              <div className="flex items-center gap-1.5">
                <input value={p.emoji} onChange={(e) => update(i, "emoji", e.target.value)}
                  className="w-8 rounded-lg bg-slate-700 px-1 py-1.5 text-center text-sm text-white outline-none" />
                <input value={p.label} onChange={(e) => update(i, "label", e.target.value)}
                  className="min-w-0 flex-1 rounded-lg bg-slate-700 px-2 py-1.5 text-xs text-white outline-none" />
                <input type="number" inputMode="decimal" value={p.oz}
                  onChange={(e) => update(i, "oz", e.target.value)}
                  className="w-12 rounded-lg bg-slate-700 px-1.5 py-1.5 text-xs text-white outline-none" />
                <span className="text-[10px] text-slate-500">oz</span>
                <input type="number" inputMode="decimal" value={p.abv}
                  onChange={(e) => update(i, "abv", e.target.value)}
                  className="w-12 rounded-lg bg-slate-700 px-1.5 py-1.5 text-xs text-white outline-none" />
                <span className="text-[10px] text-slate-500">%</span>
                <button onClick={() => setOpenMacros(openMacros === i ? null : i)}
                  title={macroSummary(p.macros)}
                  className={`text-sm ${p.macros ? "text-indigo-400" : "text-slate-600"} hover:text-indigo-300`}>
                  {openMacros === i ? "▾" : "▸"}
                </button>
                <button onClick={() => remove(i)} className="text-slate-600 hover:text-rose-400 text-sm">✕</button>
              </div>
              <div className="mt-0.5 flex flex-wrap items-center gap-2 pl-10">
                <span className="text-[10px] text-slate-500">
                  = {Number.isFinite(sduOf(p)) ? sduOf(p).toFixed(2) : "–"} SDU
                </span>
                <input type="number" inputMode="numeric" value={p.kcal ?? 0}
                  onChange={(e) => update(i, "kcal", e.target.value)}
                  className="w-14 rounded-md bg-slate-700 px-1.5 py-1 text-[11px] text-white outline-none" />
                <span className="text-[10px] text-slate-500">kcal</span>
                <input value={p.brand ?? ""} placeholder="brand" maxLength={40}
                  onChange={(e) => update(i, "brand", e.target.value)}
                  className="w-24 rounded-md bg-slate-700 px-1.5 py-1 text-[11px] text-white outline-none placeholder:text-slate-500" />
                {Number.isFinite(alcoholKcal(p)) && (
                  <span className="text-[9px]"
                    style={{ color: Number(p.kcal) < alcoholKcal(p) - 5 ? "#f59e0b" : "#64748b" }}
                    title="alcohol alone + the macros you entered">
                    ≈{Math.round(alcoholKcal(p))} from alcohol
                    {macroKcal(p) > 0 ? ` + ${Math.round(macroKcal(p))} macros` : ""}
                    {Number(p.kcal) < alcoholKcal(p) - 5 ? " — kcal looks low" : ""}
                  </span>
                )}
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
            className="flex-1 rounded-lg bg-emerald-600 py-2 text-sm font-medium text-white disabled:opacity-50">
            Save
          </button>
          <button onClick={() => save.mutate(null)} disabled={save.isPending}
            className="rounded-lg bg-slate-700 px-3 py-2 text-sm text-slate-300">
            Reset
          </button>
        </div>
      </div>
    </div>
  );
}
