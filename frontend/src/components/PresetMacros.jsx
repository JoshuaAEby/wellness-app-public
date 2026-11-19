/**
 * Optional per-serving macros for a drink preset.
 *
 * Calories alone under-count: an IPA's carbohydrate and a latte's protein are exactly
 * the macros that move a protein or fiber goal, and a drink logged as "190 kcal, no
 * protein" quietly biases those targets downward every time it is logged.
 *
 * Every field is optional. A blank stays blank — it is NOT written as 0, because
 * "unknown" and "zero grams" are different claims and only one of them is usually true.
 */

export const MACRO_FIELDS = [
  { key: "protein_g", label: "P",  title: "protein (g)" },
  { key: "carbs_g",   label: "C",  title: "carbs (g)" },
  { key: "fat_g",     label: "F",  title: "fat (g)" },
  { key: "fiber_g",   label: "Fb", title: "fiber (g)" },
  { key: "sugar_g",   label: "Sg", title: "sugar (g)" },
];

/** Strip blanks so unset macros are omitted entirely rather than stored as zero. */
export function cleanMacros(macros) {
  const out = {};
  for (const { key } of MACRO_FIELDS) {
    const v = macros?.[key];
    if (v !== "" && v != null && !Number.isNaN(Number(v))) out[key] = Number(v);
  }
  return Object.keys(out).length ? out : undefined;
}

export function macroSummary(macros) {
  const parts = MACRO_FIELDS
    .filter((f) => macros?.[f.key] != null && macros[f.key] !== "")
    .map((f) => `${f.label} ${macros[f.key]}`);
  return parts.length ? parts.join(" · ") : "no macros set";
}

export default function PresetMacros({ macros, onChange }) {
  return (
    <div className="mt-1 flex flex-wrap items-center gap-1.5 rounded-lg bg-slate-700/40 p-2">
      {MACRO_FIELDS.map((f) => (
        <label key={f.key} className="flex items-center gap-1" title={f.title}>
          <span className="text-[10px] text-slate-400">{f.label}</span>
          <input
            type="number" inputMode="decimal"
            value={macros?.[f.key] ?? ""}
            placeholder="–"
            onChange={(e) => onChange({ ...(macros || {}), [f.key]: e.target.value })}
            className="w-12 rounded-md bg-slate-700 px-1.5 py-1 text-[11px] text-white outline-none"
          />
        </label>
      ))}
      <span className="ml-auto text-[9px] text-slate-500">grams · blank = unknown</span>
    </div>
  );
}
