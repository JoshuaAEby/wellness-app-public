import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, apiErrorText } from "../api.js";

/**
 * Describe a drink, check it, log it — the Drinks sheet's AI box (2026-11-11: every
 * drink should get what any food gets). Same parser as Log Food (note 100: it returns
 * caffeine_mg / alcohol_abv / alcohol_oz per item), saved as one Drinks batch at the sheet's
 * time; the server links each dose to its food row (food_log._log_parsed_doses), so a parsed
 * latte is one entry that carries both its macros and its caffeine.
 *
 * Everything the parser guessed is editable here: calories, macros, and the doses. Water
 * belongs in the water quick-adds, not here: it has no macros and no reason to cost a parse.
 */
const MACROS = [["protein_g", "P"], ["carbs_g", "C"], ["fat_g", "F"]];

const num = (v) => {
  const n = parseFloat(v);
  return Number.isFinite(n) ? n : null;
};

export default function DrinkParse({ date, time }) {
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const [items, setItems] = useState([]);
  const [parsedText, setParsedText] = useState(null);
  const [err, setErr] = useState(null);

  const parse = useMutation({
    mutationFn: (t) => api.parseFood(t, "drinks"),
    onSuccess: (r, t) => {
      setItems((r?.items || []).map((it, i) => ({ ...it, _key: `${Date.now()}-${i}` })));
      setParsedText(t); setErr(null);
    },
    onError: (e) => setErr(apiErrorText(e)),
  });

  const save = useMutation({
    mutationFn: () => api.createNutritionLogBatch({
      date, at_time: time, meal_type: "drinks",
      // Remembered for a retype only while every item came from that one parse.
      source_text: parsedText,
      items: items.map((it) => ({
        name: it.name || null, brand: it.brand || null, description: it.description || it.name,
        portion_quantity: num(it.portion_quantity), portion_unit: it.portion_unit || null,
        calories: num(it.calories), protein_g: num(it.protein_g), carbs_g: num(it.carbs_g),
        fat_g: num(it.fat_g), fiber_g: num(it.fiber_g), sugar_g: num(it.sugar_g),
        sodium_mg: num(it.sodium_mg), ai_notes: it.notes || null, confidence: it.confidence,
        omitted_kcal: it.omitted_kcal ?? null,
        restaurant: typeof it.restaurant === "boolean" ? it.restaurant : null,   // note 84
        caffeine_mg: num(it.caffeine_mg),
        alcohol_abv: num(it.alcohol_oz) != null ? num(it.alcohol_abv) : null,
        alcohol_oz: num(it.alcohol_abv) != null ? num(it.alcohol_oz) : null,
      })),
    }),
    onSuccess: () => {
      for (const k of ["caffeine-day", "caffeine-trend", "caffeine-incomplete", "alcohol-day", "alcohol-trend",
                       "water-day", "usual-intake", "nutrition-logs", "nutrition-daily", "today"])
        qc.invalidateQueries({ queryKey: [k] });
      setItems([]); setText(""); setParsedText(null); setErr(null);
    },
    onError: (e) => setErr(apiErrorText(e)),
  });

  const set = (key, field, value) =>
    setItems((prev) => prev.map((x) => (x._key === key ? { ...x, [field]: value } : x)));
  const field = (it, f, label, w = "w-12") => (
    <label className="flex items-center gap-1">
      <span className="text-[9.5px] text-ink-3">{label}</span>
      <input inputMode="decimal" value={it[f] ?? ""} onChange={(e) => set(it._key, f, e.target.value)}
             className={`${w} rounded-md bg-canvas px-1.5 py-1 text-[12px] tabular-nums text-ink outline-none`} />
    </label>
  );
  const sdu = (it) => {
    const oz = num(it.alcohol_oz), abv = num(it.alcohol_abv);
    return oz != null && abv != null ? (oz * abv / 100) / 0.6 : null;
  };

  return (
    <div>
      <div className="flex gap-2">
        <input value={text} onChange={(e) => setText(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Enter" && text.trim()) parse.mutate(text.trim()); }}
               placeholder="Describe a drink — e.g. venti oat latte, two IPAs"
               className="min-w-0 flex-1 rounded-lg bg-surface px-3 py-2 text-[13px] text-ink outline-none placeholder:text-ink-3" />
        <button disabled={!text.trim() || parse.isPending} onClick={() => parse.mutate(text.trim())}
                className="flex-none rounded-lg bg-surface px-3 py-2 text-[12px] text-ink disabled:opacity-40">
          {parse.isPending ? "…" : "Parse"}
        </button>
      </div>
      {err && <p className="mt-1 text-[11px]" style={{ color: "var(--over)" }}>{err}</p>}

      {items.length > 0 && (
        <div className="mt-2 space-y-2">
          {items.map((it) => (
            <div key={it._key} className="rounded-lg bg-surface p-2.5">
              <div className="flex items-baseline gap-2">
                <input value={it.name ?? ""} onChange={(e) => set(it._key, "name", e.target.value)}
                       className="min-w-0 flex-1 bg-transparent text-[13px] text-ink outline-none" />
                <button onClick={() => setItems((p) => p.filter((x) => x._key !== it._key))}
                        className="flex-none px-1 text-[12px] text-ink-3" aria-label="Remove">✕</button>
              </div>
              <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1.5">
                {field(it, "calories", "kcal", "w-14")}
                {MACROS.map(([f, l]) => <span key={f}>{field(it, f, l)}</span>)}
              </div>
              <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1.5 border-t border-rule pt-1.5">
                {it.caffeine_mg != null ? (
                  <span className="flex items-center gap-1">
                    {field(it, "caffeine_mg", "☕ mg", "w-14")}
                    <button onClick={() => set(it._key, "caffeine_mg", null)} className="text-[11px] text-ink-3"
                            aria-label="Don't log caffeine">✕</button>
                  </span>
                ) : (
                  <button onClick={() => set(it._key, "caffeine_mg", "")} className="text-[10.5px] text-ink-3">+ caffeine</button>
                )}
                {it.alcohol_abv != null || it.alcohol_oz != null ? (
                  <span className="flex items-center gap-1">
                    {field(it, "alcohol_oz", "🍺 oz")}
                    {field(it, "alcohol_abv", "%")}
                    {sdu(it) != null && <span className="text-[10px] tabular-nums text-ink-3">= {sdu(it).toFixed(1)} drinks</span>}
                    <button onClick={() => { set(it._key, "alcohol_abv", null); set(it._key, "alcohol_oz", null); }}
                            className="text-[11px] text-ink-3" aria-label="Don't log alcohol">✕</button>
                  </span>
                ) : (
                  <button onClick={() => { set(it._key, "alcohol_oz", ""); set(it._key, "alcohol_abv", ""); }}
                          className="text-[10.5px] text-ink-3">+ alcohol</button>
                )}
              </div>
            </div>
          ))}
          <button disabled={save.isPending} onClick={() => save.mutate()}
                  className="w-full rounded-lg border border-rule py-2 text-[13px] font-medium text-ink disabled:opacity-40">
            {save.isPending ? "Logging…" : `Log ${items.length === 1 ? "it" : `all ${items.length}`} at ${time}`}
          </button>
        </div>
      )}
    </div>
  );
}
