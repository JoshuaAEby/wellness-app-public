import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";

// The supplement regimen — what's being taken, and since when.
//
// Distinct from the daily "supplements" habit toggle, which only says you took today's
// stack. That boolean can't say WHAT the stack is, which is why the correlation engine's
// habit_supplements input lumps every compound together and can never attribute an
// effect to one of them.
//
// Starting or stopping writes an Intervention at the same date, which is most of the
// point: without a dated record, something started six months ago is invisible to every
// chart, so the before/after question can't even be asked.
const TIMINGS = ["am", "pm", "with_food", "as_needed"];

export default function SupplementStack() {
  const qc = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState({ name: "", dose: "", unit: "mg", timing: "am",
                                     started_on: "", mark_intervention: true });
  const [busy, setBusy] = useState(false);

  const { data } = useQuery({ queryKey: ["supplements"], queryFn: api.getSupplements });
  const refresh = () => qc.invalidateQueries({ queryKey: ["supplements"] });

  const submit = () => {
    if (!form.name.trim() || busy) return;
    setBusy(true);
    api.addSupplement({
      name: form.name.trim(),
      dose: form.dose === "" ? null : Number(form.dose),
      unit: form.unit || null,
      timing: form.timing || null,
      started_on: form.started_on || null,
      mark_intervention: form.mark_intervention,
    })
      .then(() => { setAdding(false);
                    setForm({ name: "", dose: "", unit: "mg", timing: "am",
                              started_on: "", mark_intervention: true });
                    refresh(); })
      .finally(() => setBusy(false));
  };

  const stop = (s) => {
    if (!confirm(`Stop ${s.name}? The entry is kept with an end date.`)) return;
    api.stopSupplement(s.id).then(refresh);
  };

  const active = data?.active || [];
  const ended = data?.ended || [];

  return (
    <div className="rounded-xl bg-slate-800 p-4">
      <div className="mb-2 flex items-baseline justify-between">
        <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">
          Stack
        </p>
        <button onClick={() => setAdding((v) => !v)} className="text-[11px] text-indigo-400">
          {adding ? "Cancel" : "+ Add"}
        </button>
      </div>

      {active.length === 0 && !adding && (
        <p className="text-xs text-slate-500">Nothing recorded yet.</p>
      )}

      <div className="space-y-1.5">
        {active.map((s) => (
          <div key={s.id} className="flex items-center gap-2 border-b border-slate-800 pb-1.5">
            <span className="flex-1 text-[13px] text-slate-200">
              {s.name}
              {s.dose != null && (
                <span className="text-slate-500"> · {s.dose}{s.unit || ""}</span>
              )}
              {s.timing && <span className="text-slate-600"> · {s.timing.replace("_", " ")}</span>}
            </span>
            <span className="text-[10px] tabular-nums text-slate-600">
              since {s.started_on}
            </span>
            <button onClick={() => stop(s)} className="text-[10px] text-slate-500">stop</button>
          </div>
        ))}
      </div>

      {adding && (
        <div className="mt-3 space-y-2 rounded-lg bg-slate-900/60 p-3">
          <input
            value={form.name} autoFocus placeholder="Name (e.g. L-theanine)"
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            className="w-full rounded bg-slate-800 px-2 py-1.5 text-[13px] text-white placeholder-slate-600"
          />
          <div className="flex gap-2">
            <input
              value={form.dose} inputMode="decimal" placeholder="Dose"
              onChange={(e) => setForm({ ...form, dose: e.target.value })}
              className="w-20 rounded bg-slate-800 px-2 py-1.5 text-[13px] text-white placeholder-slate-600"
            />
            <input
              value={form.unit} placeholder="mg"
              onChange={(e) => setForm({ ...form, unit: e.target.value })}
              className="w-16 rounded bg-slate-800 px-2 py-1.5 text-[13px] text-white placeholder-slate-600"
            />
            <select
              value={form.timing}
              onChange={(e) => setForm({ ...form, timing: e.target.value })}
              className="flex-1 rounded bg-slate-800 px-2 py-1.5 text-[13px] text-white"
            >
              {TIMINGS.map((t) => <option key={t} value={t}>{t.replace("_", " ")}</option>)}
            </select>
          </div>
          <label className="block text-[11px] text-slate-500">
            Start date (blank = today)
            <input
              type="date" value={form.started_on}
              onChange={(e) => setForm({ ...form, started_on: e.target.value })}
              className="mt-1 w-full rounded bg-slate-800 px-2 py-1.5 text-[13px] text-white"
            />
          </label>
          {/* Off for backfilled history: a marker at a guessed date is worse than none. */}
          <label className="flex items-center gap-2 text-[11px] text-slate-500">
            <input
              type="checkbox" checked={form.mark_intervention}
              onChange={(e) => setForm({ ...form, mark_intervention: e.target.checked })}
            />
            Mark this date on the trend charts
          </label>
          <button
            onClick={submit} disabled={busy || !form.name.trim()}
            className="w-full rounded-lg bg-indigo-600 py-2 text-[13px] font-semibold text-white active:bg-indigo-500 disabled:opacity-40"
          >
            Add
          </button>
        </div>
      )}

      {ended.length > 0 && (
        <details className="mt-3">
          <summary className="cursor-pointer text-[11px] text-slate-600">
            Discontinued ({ended.length})
          </summary>
          <div className="mt-1.5 space-y-1">
            {ended.map((s) => (
              <p key={s.id} className="text-[11px] text-slate-600">
                {s.name} · {s.started_on} → {s.ended_on}
              </p>
            ))}
          </div>
        </details>
      )}

      <p className="mt-3 text-[10px] leading-relaxed text-slate-600">
        Chart markers support before/after reading, which is confounded with everything
        else that changed at the same date. Treat them as hypotheses — a randomized
        experiment is what actually tests one.
      </p>
    </div>
  );
}
