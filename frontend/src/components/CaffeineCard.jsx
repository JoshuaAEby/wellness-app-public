import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";
import CaffeinePresetsModal from "./CaffeinePresetsModal.jsx";

// "YYYY-MM-DD" + "HH:MM" (local) -> naive-UTC ISO the API expects.
//
// `new Date(Y, Mo-1, D, h, m)` builds the instant in LOCAL time and toISOString()
// already renders that instant in UTC, so slicing it IS the conversion. Do not
// "helpfully" subtract getTimezoneOffset() first — that idiom produces an ISO string
// that *reads* as local time, the opposite need. It double-shifts: 08:20 got stored
// as 04:20 (caught in-browser).
function naiveUtc(dateStr, hhmm) {
  if (!/^\d{2}:\d{2}$/.test(hhmm || "")) return null;
  const [h, m] = hhmm.split(":").map(Number);
  const [Y, Mo, D] = dateStr.split("-").map(Number);
  return new Date(Y, Mo - 1, D, h, m, 0, 0).toISOString().slice(0, 19);
}

function localToday() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// Per-instance caffeine log: tap a preset to log a dose at the current time, then
// tap the TIME on any logged entry to correct it.
//
// Timing is the whole point here, not a detail: onboard_at_bed decays as
// 0.5^(hours/HALF_LIFE), so a dose recorded 3 h off misstates the bedtime residual
// by ~1.5x. Before the edit path existed the only recordable times were "the moment
// I tapped" and the noon backfill default, and neither could be corrected.
// `time` ("HH:MM", from the Drinks sheet's shared time, 2026-11-11): every dose logged
// here takes that time, today included, and the card's own backfill row is hidden. A time
// before 04:00 belongs to the NEXT calendar date of the food day (app/foodday.py).
function foodDayInstant(foodDate, hhmm) {
  if (hhmm >= "04:00") return naiveUtc(foodDate, hhmm);
  const d = new Date(`${foodDate}T12:00:00`); d.setDate(d.getDate() + 1);
  const next = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  return naiveUtc(next, hhmm);
}

export default function CaffeineCard({ date = null, time = null }) {
  const qc = useQueryClient();
  const todayStr = localToday();
  // `date` = the LOG view's single date picker (2026-10-21); the card's own date input
  // only appears when it is used standalone.
  const [ownDate, setLogDate] = useState(todayStr);
  const logDate = date ?? ownDate;
  const [showCustom, setShowCustom] = useState(false);
  const [customMg, setCustomMg] = useState("");
  const [editPresets, setEditPresets] = useState(false);
  const [editingId, setEditingId] = useState(null);   // instance whose time is open
  const [editTime, setEditTime] = useState("");       // "HH:MM" local
  const [backfillTime, setBackfillTime] = useState("12:00");  // "HH:MM" local, past days

  const { data: presetData } = useQuery({
    queryKey: ["caffeine-presets"],
    queryFn: api.getCaffeinePresets,
    staleTime: 5 * 60 * 1000,
  });
  const presets = presetData?.presets || [];

  const { data: day } = useQuery({
    queryKey: ["caffeine-day", logDate],
    queryFn: () => api.getCaffeineDay(logDate),
  });
  const instances = day?.instances || [];

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["caffeine-day"] });
    qc.invalidateQueries({ queryKey: ["caffeine-incomplete"] });
    qc.invalidateQueries({ queryKey: ["water-day"] });   // caffeinated drinks count as fluid
  };
  const logM = useMutation({ mutationFn: (b) => api.logCaffeine(b), onSuccess: invalidate });
  const delM = useMutation({ mutationFn: (id) => api.deleteCaffeine(id), onSuccess: invalidate });
  const patchM = useMutation({
    mutationFn: ({ id, body }) => api.updateCaffeine(id, body),
    onSuccess: () => { setEditingId(null); invalidate(); },
  });

  const saveTime = (inst) => {
    const ts = naiveUtc(logDate, editTime);
    if (!ts) return;
    patchM.mutate({ id: inst.id, body: { timestamp: ts } });
  };
  const ackM = useMutation({ mutationFn: (ack) => api.ackCaffeine(logDate, ack), onSuccess: invalidate });
  const clearAckM = useMutation({ mutationFn: () => api.clearCaffeineAck(logDate), onSuccess: invalidate });

  // Today logs at "now" (server default). A past day uses the time picked above the
  // presets — previously this hardcoded `${logDate}T12:00:00`, which the server reads
  // as naive UTC, so "noon" was actually 08:00 local. Timing is the whole point of
  // instance logging (onboard_at_bed decays from dose time to bedtime), so a silent
  // 4-hour error was not a cosmetic bug.
  const backfillTs = () => (time ? foodDayInstant(logDate, time)
    : logDate === todayStr ? null : naiveUtc(logDate, backfillTime));

  const logPreset = (kind) => {
    const body = { date: logDate, kind };
    const ts = backfillTs();
    if (ts) body.timestamp = ts;
    logM.mutate(body);
  };
  const logCustom = () => {
    const mg = Number(customMg);
    if (!mg || mg <= 0) return;
    const body = { date: logDate, kind: "custom", mg };
    const ts = backfillTs();
    if (ts) body.timestamp = ts;
    logM.mutate(body);
    setCustomMg(""); setShowCustom(false);
  };

  const total = day?.total_mg ?? 0;
  const ack = day?.ack;
  const isNone = ack === "none" && instances.length === 0;

  // ── 30-day mean, as the reference the day's bar is read against ──────────────
  //
  // Caffeine has no goal — a target intake is not a thing this app has an opinion
  // about — so the honest reference is your own habit. Getting the denominator
  // right matters more than it looks: /caffeine/range OMITS days with no logged
  // dose, and its docstring is explicit that an absent day means "not logged",
  // which is NOT the same as a genuine zero. So:
  //   • dividing by 30 would score every unlogged day as a 0 mg day (understates)
  //   • dividing by the number of returned rows averages only caffeinated days
  //     (overstates — it drops the real zeros you explicitly acked)
  // The complete days are the ones that are either logged OR acked "none", which
  // is exactly `window − incomplete`. Acked zeros then count as the 0s they are.
  const AVG_DAYS = 30;
  const avgStart = (() => { const d = new Date(); d.setDate(d.getDate() - AVG_DAYS); return d.toISOString().slice(0, 10); })();
  const avgEnd = (() => { const d = new Date(); d.setDate(d.getDate() - 1); return d.toISOString().slice(0, 10); })();
  const { data: range = [] } = useQuery({
    queryKey: ["caffeine-range", avgStart, avgEnd],
    queryFn: () => api.getCaffeineRange(avgStart, avgEnd),
    staleTime: 1000 * 60 * 30,
  });
  const { data: incomplete } = useQuery({
    queryKey: ["caffeine-incomplete", AVG_DAYS],
    queryFn: () => api.getCaffeineIncomplete(AVG_DAYS),
    staleTime: 1000 * 60 * 30,
  });
  const completeDays = incomplete ? AVG_DAYS - (incomplete.incomplete?.length ?? 0) : null;
  const avgMg = completeDays && completeDays > 0
    ? Math.round(range.reduce((s, r) => s + (r.value || 0), 0) / completeDays)
    : null;
  // Scale so the mean sits at 60% of the track: a typical day reads as "typical"
  // rather than pinning the bar, and a heavy day still has room to show it.
  const barMax = avgMg && avgMg > 0 ? Math.max(avgMg / 0.6, total * 1.05) : Math.max(total, 1);

  return (
    <div className="rounded-2xl border border-rule bg-card p-4">
      <div className="mb-2.5 flex items-center justify-between">
        <p className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">
          CAFFEINE {logDate === todayStr ? "TODAY" : logDate}
        </p>
        {date == null && (
          <div className="flex items-center gap-2">
            {logDate !== todayStr && (
              <button onClick={() => setLogDate(todayStr)} className="text-[10px] tracking-micro text-ink-2 hover:text-ink">TODAY</button>
            )}
            <input type="date" value={logDate} max={todayStr}
                   onChange={(e) => setLogDate(e.target.value)}
                   className="rounded-md bg-surface px-2 py-1 text-[11px] text-ink outline-none focus:ring-1 focus:ring-ink-3 [color-scheme:dark]" />
          </div>
        )}
      </div>

      {/* Day total against the 30-day mean — same in-track treatment as the
          nutrition bars, but the reference is a habit, not a goal. */}
      {!isNone && (
        <div className="mb-3">
          <div className="relative h-[26px] overflow-hidden rounded-md bg-surface">
            <div className="absolute inset-y-0 left-0 transition-[width] duration-300"
              style={{ width: `${Math.min(100, (total / barMax) * 100)}%`,
                       backgroundColor: "var(--drinks)", opacity: 0.6 }} />
            {avgMg != null && avgMg > 0 && (
              <div className="absolute inset-y-0 w-px bg-ink-2"
                style={{ left: `${Math.min(100, (avgMg / barMax) * 100)}%` }}
                title={`30-day mean ${avgMg} mg`} />
            )}
            <div className="relative flex h-full items-center justify-between px-2">
              <span className="text-[10px] font-semibold tracking-micro text-ink">
                {Math.round(total)} <span className="font-normal text-ink-2">mg</span>
              </span>
              {avgMg != null && (
                <span className="text-[9.5px] text-ink-2">
                  {total > avgMg ? "+" : ""}{Math.round(total - avgMg)} vs 30d mean
                </span>
              )}
            </div>
          </div>
          <p className="mt-0.5 text-[9.5px] leading-none text-ink-3">
            {avgMg != null
              ? `▲ 30-day mean ${avgMg} mg · over ${completeDays} logged days`
              : "30-day mean unavailable — not enough logged days"}
          </p>
        </div>
      )}

      {isNone ? (
        <div className="flex items-center gap-2 rounded-lg border border-slate-700/60 bg-slate-800/40 px-3 py-2">
          <span className="flex-1 text-xs italic text-slate-500">No caffeine this day</span>
          <button onClick={() => clearAckM.mutate()} className="text-xs font-medium text-slate-500 hover:text-slate-200">Undo</button>
        </div>
      ) : (
        <>
          {/* Backfill: ask for the time rather than assuming one. Only for past days —
              today's entries are stamped "now" by the server. */}
          {logDate !== todayStr && !time && (
            <div className="mb-2 flex items-center gap-2 rounded-lg border border-slate-700/60 bg-slate-900/40 px-2.5 py-1.5">
              <span className="text-[10px] uppercase tracking-wider text-slate-500">Time</span>
              <input
                type="time"
                value={backfillTime}
                onChange={(e) => setBackfillTime(e.target.value)}
                className="rounded bg-slate-700 px-2 py-0.5 text-xs text-white outline-none focus:ring-2 focus:ring-indigo-500 [color-scheme:dark]"
              />
              <span className="text-[10px] text-slate-600">applies to what you log below</span>
            </div>
          )}

          {/* Preset buttons */}
          <div className="flex flex-wrap gap-1.5">
            {presets.map((p) => (
              <button key={p.id} onClick={() => logPreset(p.id)} disabled={logM.isPending}
                className="rounded-lg bg-slate-700 px-2 py-1 text-xs text-slate-200 hover:bg-slate-600 disabled:opacity-50">
                {p.emoji} {p.label.replace("SBUX ", "")}
                <span className="ml-1 text-[10px] text-slate-500">{p.mg}</span>
              </button>
            ))}
            <button onClick={() => setShowCustom((v) => !v)}
              className="rounded-lg bg-slate-700 px-2 py-1 text-xs text-slate-400 hover:bg-slate-600">✏️ Custom</button>
            <button onClick={() => setEditPresets(true)}
              className="rounded-lg px-2 py-1 text-xs text-slate-500 hover:text-slate-300" title="Edit presets & mg">⚙</button>
          </div>
          {showCustom && (
            <div className="mt-2 flex items-center gap-2">
              <input type="number" inputMode="numeric" value={customMg} onChange={(e) => setCustomMg(e.target.value)}
                placeholder="mg" className="w-20 rounded-lg bg-slate-700 px-2 py-1 text-xs text-white outline-none" />
              <button onClick={logCustom} className="rounded-lg bg-indigo-600 px-2 py-1 text-xs text-white">Add</button>
            </div>
          )}

          {/* Instances logged */}
          {instances.length > 0 ? (
            <div className="mt-3 space-y-1">
              {instances.map((i) => {
                const p = presets.find((x) => x.id === i.kind);
                return (
                  <div key={i.id} className="rounded-lg bg-slate-700/50 px-3 py-1.5">
                    <div className="flex items-center gap-2">
                      <span className="text-xs">{p?.emoji || "☕"}</span>
                      <span className="flex-1 text-xs text-slate-300">{p?.label || "Custom"}</span>
                      <button
                        onClick={() => {
                          setEditingId(editingId === i.id ? null : i.id);
                          setEditTime(i.time);
                        }}
                        className={`rounded px-1.5 py-0.5 text-[10px] ${
                          editingId === i.id
                            ? "bg-indigo-500/20 text-indigo-300"
                            : "text-slate-500 hover:bg-slate-600/50 hover:text-slate-300"
                        }`}
                        title="Tap to correct the time"
                      >
                        {i.time} ✎
                      </button>
                      <span className="text-xs font-medium text-amber-400/90">{Math.round(i.mg)} mg</span>
                      <button onClick={() => delM.mutate(i.id)} className="text-slate-600 hover:text-rose-400 text-sm leading-none">✕</button>
                    </div>
                    {editingId === i.id && (
                      <div className="mt-1.5 flex items-center gap-2 border-t border-slate-600/40 pt-1.5">
                        <input
                          type="time"
                          value={editTime}
                          onChange={(e) => setEditTime(e.target.value)}
                          className="rounded-lg bg-slate-700 px-2 py-1 text-xs text-white outline-none focus:ring-2 focus:ring-indigo-500 [color-scheme:dark]"
                        />
                        <button
                          onClick={() => saveTime(i)}
                          disabled={patchM.isPending}
                          className="rounded-lg bg-indigo-600 px-2.5 py-1 text-xs font-medium text-white disabled:opacity-50"
                        >
                          Save
                        </button>
                        <button onClick={() => setEditingId(null)} className="text-xs text-slate-500">
                          Cancel
                        </button>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="mt-3 flex items-center gap-2">
              <button onClick={() => ackM.mutate("none")}
                className="rounded-lg bg-slate-700/60 px-2.5 py-1 text-xs text-slate-400 hover:bg-slate-600 hover:text-slate-200">
                No caffeine this day
              </button>
            </div>
          )}
        </>
      )}
      {editPresets && (
        <CaffeinePresetsModal
          presets={presets}
          defaults={presetData?.defaults || []}
          onClose={() => setEditPresets(false)}
        />
      )}
    </div>
  );
}
