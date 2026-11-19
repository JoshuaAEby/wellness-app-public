import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";
import { EVENT_CATEGORY, eventStyle } from "./chartHelpers.jsx";
import { Card, Eyebrow, HeroSub } from "./ui.jsx";

// Marked changes (interrupted time series): a behaviour change you flag on a date, then a
// before/after analysis of every metric. Moved out of Insights → Analysis → Trends when that
// view was removed (note 99, 2026-11-04): it is the only place a change can be added, and its
// markers show on the Fitness and Body charts. It sits under Experiments as the weaker
// relative of a trial: not randomised, so concurrent changes and season still apply.
// The modal and report are moved verbatim from Trends.jsx.

function MarkChangeModal({ onClose, onSave, pending, initial }) {
  const now = new Date();
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  const [date, setDate] = useState(initial?.date ?? today);
  const [label, setLabel] = useState(initial?.label ?? "");
  const [notes, setNotes] = useState(initial?.notes ?? "");
  // Sets the chart glyph (0066). Uncategorised rows keep NULL until one is picked.
  const [category, setCategory] = useState(initial?.category ?? null);
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/60 px-4 pb-6" onClick={onClose}>
      <div className="w-full max-w-md rounded-2xl bg-slate-800 p-5 space-y-3" onClick={e => e.stopPropagation()}>
        <p className="text-base font-semibold text-white">{initial ? "Edit change" : "Mark a change"}</p>
        <p className="text-xs text-slate-500">Flag a behavioral change (new supplement, training block, sleep routine…). Once ~2 weeks of data accrue, tap it for a before/after analysis.</p>
        <div>
          <label className="text-xs text-slate-400">Date it started</label>
          <input type="date" value={date} onChange={e => setDate(e.target.value)}
            className="mt-1 w-full rounded-lg bg-slate-700 px-3 py-2 text-sm text-white" />
        </div>
        <div>
          <label className="text-xs text-slate-400">Short name <span className="text-slate-600">· shown in each chart's key</span></label>
          <input type="text" value={label} onChange={e => setLabel(e.target.value)} maxLength={40}
            placeholder="e.g. creatine"
            className="mt-1 w-full rounded-lg bg-slate-700 px-3 py-2 text-sm text-white placeholder-slate-500" />
        </div>
        <div>
          <label className="text-xs text-slate-400">Kind <span className="text-slate-600">· sets its marker on charts</span></label>
          <div className="mt-1 flex flex-wrap gap-1.5">
            {Object.entries(EVENT_CATEGORY).map(([k, s]) => (
              <button key={k} type="button" onClick={() => setCategory(category === k ? null : k)}
                className={`rounded-lg px-2.5 py-1 text-xs transition-colors ${
                  category === k ? "bg-slate-600 text-white" : "bg-slate-700 text-slate-400 hover:text-slate-200"}`}>
                <span style={{ color: s.color }}>{s.glyph}</span> {s.label}
              </button>
            ))}
          </div>
        </div>
        <div>
          <label className="text-xs text-slate-400">Notes (optional)</label>
          <textarea value={notes} onChange={e => setNotes(e.target.value)} rows={2}
            className="mt-1 w-full rounded-lg bg-slate-700 px-3 py-2 text-sm text-white" />
        </div>
        <div className="flex gap-2 pt-1">
          <button onClick={onClose} className="flex-1 rounded-xl bg-slate-700 py-2.5 text-sm font-medium text-slate-400">Cancel</button>
          <button onClick={() => label.trim() && onSave(date, label.trim(), notes.trim() || null, category)}
            disabled={!label.trim() || pending}
            className="flex-1 rounded-xl bg-indigo-600 py-2.5 text-sm font-medium text-white disabled:opacity-40">
            {pending ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}

function InterventionReport({ analysis, loading, onDelete }) {
  const SENT = { good: "#34d399", bad: "#f87171", neutral: "#94a3b8" };
  if (loading) return <p className="py-2 pl-1 text-xs text-slate-500">Analysing before/after…</p>;
  const results = analysis?.results ?? [];
  const sig = results.filter(r => r.significant);
  const washout = analysis?.washout ?? 7;
  return (
    <div className="mt-1 mb-2 space-y-2 pl-1">
      {sig.length === 0 ? (
        <p className="text-xs text-slate-500">No metric changed significantly beyond its existing trend — effects may still be emerging (needs ≥2 weeks each side).</p>
      ) : (
        <div className="space-y-1">
          {sig.map(r => (
            <div key={r.key} className="flex items-center gap-2 text-xs">
              <span className="w-3 text-center font-bold" style={{ color: SENT[r.sentiment] }}>
                {r.effect === "level" ? (r.level > 0 ? "↑" : "↓") : (r.slope_per_week > 0 ? "↗" : "↘")}
              </span>
              <span className="flex-1 truncate text-slate-300">{r.label}</span>
              <span className="text-slate-400">{r.pre_mean} → {r.post_mean}{r.unit}</span>
              <span className="text-slate-600">p={(r.effect === "level" ? r.p_level : r.p_slope)}</span>
            </div>
          ))}
        </div>
      )}
      <p className="text-[10px] leading-snug text-slate-600">
        Segmented regression at the flagged date (washout {washout}d), block-bootstrap gated. The pre-existing trend is subtracted out, so this is the change <em>beyond</em> what was already happening. Confounds (concurrent changes, seasonality) still apply — this isn't proof of cause.
      </p>
      <button onClick={onDelete} className="text-[10px] text-rose-400/70 hover:text-rose-400">Delete this intervention</button>
    </div>
  );
}

export default function MarkedChanges() {
  const qc = useQueryClient();
  const [showMark, setShowMark] = useState(false);
  const [editingIv, setEditingIv] = useState(null);
  const [selectedIv, setSelectedIv] = useState(null);
  const { data: interventions = [] } = useQuery({
    queryKey: ["interventions"],
    queryFn: api.listInterventions,
  });
  const { data: ivAnalysis, isLoading: loadingIv } = useQuery({
    queryKey: ["intervention-analysis", selectedIv],
    queryFn: () => api.interventionAnalysis(selectedIv),
    enabled: selectedIv != null,
  });
  const createIv = useMutation({
    mutationFn: ({ date, label, notes, category }) => api.createIntervention(date, label, notes, category),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["interventions"] }); setShowMark(false); },
  });
  const updateIv = useMutation({
    mutationFn: ({ id, date, label, notes, category }) => api.updateIntervention(id, date, label, notes, category),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["interventions"] });
      qc.invalidateQueries({ queryKey: ["intervention-analysis"] });
      setEditingIv(null);
    },
  });
  const deleteIv = useMutation({
    mutationFn: (id) => api.deleteIntervention(id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["interventions"] }); setSelectedIv(null); },
  });

  return (
    <Card>
      <Eyebrow left="MARKED CHANGES" right="NOT RANDOMISED" />
      <div className="mt-1 flex items-start justify-between gap-3">
        <HeroSub>Flag a change you made; after ~2 weeks, tap it for a before/after look at every metric.</HeroSub>
        <button onClick={() => setShowMark(true)}
          className="flex-none rounded-lg border border-rule bg-surface px-2.5 py-1 text-[11px] text-ink">+ Mark change</button>
      </div>
      <div className="mt-2">
        {interventions.length === 0 ? (
          <p className="text-[12px] text-ink-3">No changes flagged yet.</p>
        ) : (
          <div className="space-y-1">
            {interventions.map(iv => {
              const open = selectedIv === iv.id;
              return (
                <div key={iv.id}>
                  <div className="flex items-center gap-1">
                    <button onClick={() => setSelectedIv(open ? null : iv.id)}
                      className="flex flex-1 items-center gap-2 rounded py-1 text-left transition-colors hover:bg-surface">
                      <span className="w-3 flex-shrink-0 text-center text-xs" style={{ color: eventStyle(iv).color }}
                        title={eventStyle(iv).label}>{eventStyle(iv).glyph}</span>
                      <span className="flex-1 truncate text-[13px] text-ink">{iv.label}</span>
                      <span className="flex-shrink-0 text-[11px] text-ink-3">
                        {new Date(iv.date + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "2-digit" })}
                      </span>
                      <span className="w-3 flex-shrink-0 text-center text-xs text-ink-3">{open ? "▾" : "▸"}</span>
                    </button>
                    <button onClick={() => setEditingIv(iv)} className="flex-shrink-0 px-1 text-xs text-ink-3 hover:text-ink-2" aria-label="Edit">✎</button>
                  </div>
                  {open && <InterventionReport analysis={ivAnalysis} loading={loadingIv} onDelete={() => deleteIv.mutate(iv.id)} />}
                </div>
              );
            })}
          </div>
        )}
      </div>
      {(showMark || editingIv) && (
        <MarkChangeModal
          initial={editingIv}
          pending={createIv.isPending || updateIv.isPending}
          onClose={() => { setShowMark(false); setEditingIv(null); }}
          onSave={(date, label, notes, category) => editingIv
            ? updateIv.mutate({ id: editingIv.id, date, label, notes, category })
            : createIv.mutate({ date, label, notes, category })}
        />
      )}
    </Card>
  );
}
