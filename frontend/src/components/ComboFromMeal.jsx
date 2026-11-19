import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, apiErrorText } from "../api.js";
import { BrandTag } from "./ui.jsx";

/**
 * "+ My Food" on a logged meal (2026-11-12): tick the entries that make one dish
 * (spinach salad + beef + ranch), name it ("Taco salad"), save. The server turns each
 * entry into a part (an existing My Food is reused at the right servings, anything else
 * becomes a new estimate-sourced food) and groups today's entries under the new name,
 * so the day reads as the dish. Next time it is one tap in this meal's My Foods.
 */
export default function ComboFromMeal({ meal, mealLabel, logs, onClose }) {
  const qc = useQueryClient();
  const [picked, setPicked] = useState(() => new Set(logs.map((l) => l.id)));
  const [name, setName] = useState("");
  const [done, setDone] = useState(null);
  const chosen = logs.filter((l) => picked.has(l.id));
  const sum = (k) => chosen.reduce((a, l) => a + (l[k] ?? 0), 0);

  const save = useMutation({
    mutationFn: () => api.comboFromItems({
      name: name.trim(), meal_type: meal, items: chosen.map((l) => ({ log_id: l.id })),
    }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["my-foods"] });
      qc.invalidateQueries({ queryKey: ["nutrition-logs"] });
      setDone(r);
    },
  });

  const toggle = (id) => setPicked((prev) => {
    const n = new Set(prev);
    if (n.has(id)) n.delete(id); else n.add(id);
    return n;
  });

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/60 pb-16" onClick={onClose}>
      <div className="flex max-h-[85vh] w-full max-w-md flex-col rounded-t-2xl border border-rule bg-card"
           onClick={(e) => e.stopPropagation()}>
        <div className="flex flex-none items-center justify-between px-4 pb-2 pt-4">
          <span className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">
            {mealLabel.toUpperCase()} → MY FOOD
          </span>
          <button onClick={onClose} className="-mr-1 px-1 text-[18px] leading-none text-ink-3" aria-label="Close">×</button>
        </div>

        {done ? (
          <div className="px-4 pb-5">
            <p className="text-[14px] text-ink">✓ “{done.combo.name}” is in My Foods</p>
            <p className="mt-1 text-[11.5px] text-ink-3">
              {Math.round(done.combo.calories ?? 0)} kcal · {done.items.length} parts. Today’s {mealLabel.toLowerCase()} now
              shows as it. Next time it’s one tap in {mealLabel}’s My Foods.
            </p>
            {done.items.some((i) => i.created) && (
              <p className="mt-2 text-[11px] text-ink-3">
                New in the library (numbers from today’s estimate):{" "}
                {done.items.filter((i) => i.created).map((i) => i.name).join(", ")}.
              </p>
            )}
            {done.items.some((i) => !i.created) && (
              <p className="mt-1 text-[11px] text-ink-3">
                Reused: {done.items.filter((i) => !i.created)
                  .map((i) => `${i.name}${i.servings !== 1 ? ` ×${i.servings}` : ""}`).join(", ")}.
              </p>
            )}
            <button onClick={onClose} className="mt-4 w-full rounded-xl border border-rule py-2.5 text-[13px] text-ink">Done</button>
          </div>
        ) : (
          <>
            <div className="min-h-0 flex-1 overflow-y-auto px-4">
              <p className="mb-2 text-[11.5px] text-ink-3">Tick what goes in the dish.</p>
              <div className="space-y-1.5">
                {logs.map((l) => {
                  const on = picked.has(l.id);
                  return (
                    <button key={l.id} onClick={() => toggle(l.id)}
                      className={`flex w-full items-center gap-2.5 rounded-lg border px-3 py-2 text-left ${
                        on ? "border-[color:var(--hrv)] bg-surface" : "border-rule"}`}>
                      <span className={`flex h-4 w-4 flex-none items-center justify-center rounded border text-[10px] ${
                        on ? "border-[color:var(--hrv)] text-ink" : "border-rule text-transparent"}`}>✓</span>
                      <span className="min-w-0 flex-1 truncate text-[12.5px] text-ink">
                        {l.name || l.description}<BrandTag brand={l.brand} />
                      </span>
                      <span className="flex-none text-[11px] tabular-nums text-ink-3">
                        {l.calories != null ? `${Math.round(l.calories)} kcal` : "—"}
                      </span>
                    </button>
                  );
                })}
              </div>
              <label className="mt-3 block">
                <span className="block text-[9px] tracking-micro text-ink-3">NAME THE DISH</span>
                <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Taco salad"
                  autoCapitalize="words"
                  className="mt-0.5 w-full rounded-lg border border-rule bg-surface px-3 py-2 text-[13px] text-ink outline-none focus:border-[color:var(--hrv)]" />
              </label>
              {save.isError && (
                <p className="mt-2 text-[11px]" style={{ color: "var(--over)" }}>{apiErrorText(save.error)}</p>
              )}
            </div>
            <div className="flex flex-none items-center gap-3 border-t border-rule px-4 py-3">
              <span className="flex-1 text-[12px] tabular-nums text-ink-3">
                <b className="text-[14px] font-semibold text-ink">{Math.round(sum("calories"))}</b> kcal
                {" · "}{Math.round(sum("protein_g"))} g protein · {chosen.length} parts
              </span>
              <button disabled={chosen.length < 2 || !name.trim() || save.isPending} onClick={() => save.mutate()}
                className="rounded-xl bg-ink px-4 py-2.5 text-[13px] font-medium text-canvas disabled:opacity-40">
                {save.isPending ? "Saving…" : "Save"}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
