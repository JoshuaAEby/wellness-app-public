import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, apiErrorText } from "../api.js";
import { removeQueued } from "../offline.js";
import { Card, Eyebrow } from "./ui.jsx";

// One-tap re-log of what you usually eat around now (GET /api/nutrition/quick-log,
// 2026-10-21). A chip replays its most recent entry through the normal batch write — same
// numbers, servings, My Food link and combo parts — then offers Undo for a few seconds.
// Drinks with their own presets (caffeine, alcohol) stay in the intake card above.
//
// SPLIT BY MEAL (note 81, 2026-10-26): per_meal=4 files each food under the meal it is
// usually logged at, and tabs pick the meal — same height as the old single row. The
// tab opens on the meal of the top suggestion, which the server orders by time of day,
// so at 8 am it is breakfast and at 7 pm dinner without a clock rule here.
const UNDO_MS = 6000;
const MEAL_ORDER = ["breakfast", "lunch", "dinner", "snack", "dessert", "drinks"];

export default function QuickLogRow({ date }) {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["quick-log", "per-meal"],
    queryFn: () => api.getQuickLog(8, 4),
    staleTime: 5 * 60 * 1000,
  });
  const [busy, setBusy] = useState(null);       // key being logged
  const [undo, setUndo] = useState(null);       // { label, ids }
  const [err, setErr] = useState(null);
  const [meal, setMeal] = useState(null);       // null = follow the top suggestion

  useEffect(() => {
    if (!undo) return;
    const t = setTimeout(() => setUndo(null), UNDO_MS);
    return () => clearTimeout(t);
  }, [undo]);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["nutrition-logs"] });
    qc.invalidateQueries({ queryKey: ["nutrition-daily"] });
    qc.invalidateQueries({ queryKey: ["nutrition-incomplete"] });
    qc.invalidateQueries({ queryKey: ["quick-log"] });
  };

  const log = async (s) => {
    setBusy(s.key);
    setErr(null);
    try {
      const created = await api.createNutritionLogBatch({ date, meal_type: s.meal_type, items: s.items });
      // Offline: the write is queued on the phone (src/offline.js); Undo cancels it there.
      if (created?.queued) setUndo({ key: s.key, label: `${s.label} (offline — syncs later)`, queuedKey: created.key, ids: [] });
      else setUndo({ key: s.key, label: s.label, ids: (created || []).map((r) => r.id) });
      refresh();
    } catch (e) {
      setErr(apiErrorText(e));
    } finally {
      setBusy(null);
    }
  };

  const doUndo = async () => {
    const u = undo;
    setUndo(null);
    if (u.queuedKey) { removeQueued(u.queuedKey); return; }
    for (const id of u.ids) {
      try { await api.deleteNutritionLog(id); } catch { /* row may already be gone */ }
    }
    refresh();
  };

  const all = data?.suggestions ?? [];
  if (!all.length) return null;
  const meals = MEAL_ORDER.filter((m) => all.some((s) => s.meal_type === m));
  const active = meals.includes(meal) ? meal : all[0].meal_type;
  const items = all.filter((s) => s.meal_type === active);

  return (
    <Card>
      <Eyebrow left="QUICK LOG" right={`YOUR USUAL ${active.toUpperCase()}`} />
      {/* Wraps rather than scrolls: a sixth tab cut off at the edge reads as absent. */}
      <div className="mt-1 flex flex-wrap gap-x-0.5 gap-y-1">
        {meals.map((m) => (
          <button key={m} type="button" onClick={() => setMeal(m)}
                  className={`flex-none rounded-full px-1.5 py-0.5 text-[10px] capitalize ${
                    m === active ? "bg-ink text-card" : "text-ink-3 hover:text-ink-2"}`}>
            {m}
          </button>
        ))}
      </div>
      <div className="-mx-1 mt-1 flex gap-1.5 overflow-x-auto px-1 pb-1">
        {/* The tapped chip becomes its own Undo (2026-10-26) — the old Undo bar below
            appeared and vanished, moving everything under it mid-tap. */}
        {items.map((s) => undo?.key === s.key ? (
          <button key={s.key} type="button" onClick={doUndo} title={`Logged ${undo.label} — tap to undo`}
                  className="flex-none rounded-lg bg-ink px-2.5 py-1.5 text-left">
            <span className="block max-w-[8.5rem] truncate text-[12px] font-semibold text-card">✓ Undo</span>
            <span className="block max-w-[8.5rem] truncate text-[10px] text-card opacity-70">{s.label}</span>
          </button>
        ) : (
          <button
            key={s.key}
            type="button"
            disabled={busy != null}
            onClick={() => log(s)}
            title={`${s.times}× in 120 days · last ${s.last} · logs as ${s.meal_type}`}
            className="flex-none rounded-lg border border-rule bg-surface px-2.5 py-1.5 text-left disabled:opacity-50"
          >
            <span className="block max-w-[8.5rem] truncate text-[12px] text-ink">
              {busy === s.key ? "Logging…" : s.label}
            </span>
            <span className="block text-[10px] text-ink-3 tabular-nums">
              {s.kcal} kcal · {s.times}×
            </span>
          </button>
        ))}
      </div>
      {err && <p className="mt-1 text-[11px]" style={{ color: "var(--over)" }}>{err}</p>}
    </Card>
  );
}
