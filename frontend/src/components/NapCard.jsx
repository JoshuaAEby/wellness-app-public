import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";
import { Card, Eyebrow, Foot } from "./ui.jsx";
import { Sheet, WhenRow, EntryList, localToday, nowHHMM } from "./IntakeTrio.jsx";

/**
 * Nap logging — start time and duration.
 *
 * Apple cannot supply this. `sleep_sessions` holds exactly ONE row per date (783 rows
 * across 783 dates), so a daytime sleep is folded into that night's session instead of
 * being stored separately — which is also why a few nights show impossible 16 h spans.
 * So the duration is entered, and the timestamp is when the nap STARTED.
 *
 * Naps are deliberately NOT added to sleep_total_h or sleep debt. Doing so would move
 * the readiness score and the sleep-debt model without either having been calibrated on
 * daytime sleep. They accrue as their own input (`nap_min`) and can be folded in later
 * if the data supports it.
 */
// `compact` (note 82, 2026-10-26): a one-line button the same size as the collapsed
// day-flag control, so the two sit side by side in LOG instead of stacking two cards.
export default function NapCard({ date: fixedDate = null, compact = false }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  // `fixedDate` = the LOG view's single date picker (2026-10-21).
  const [ownDate, setDate] = useState(localToday());
  const date = fixedDate ?? ownDate;
  const [time, setTime] = useState(nowHHMM());
  const [mins, setMins] = useState("");

  const today = fixedDate ?? localToday();
  const { data: todayNap } = useQuery({ queryKey: ["nap-day", today], queryFn: () => api.getNapDay(today) });
  const { data: day } = useQuery({ queryKey: ["nap-day", date], queryFn: () => api.getNapDay(date), enabled: open });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["nap-day"] });
    qc.invalidateQueries({ queryKey: ["today"] });
  };
  const log = useMutation({ mutationFn: api.logNap, onSuccess: invalidate });
  const del = useMutation({ mutationFn: api.deleteNap, onSuccess: invalidate });
  const patch = useMutation({ mutationFn: ({ id, body }) => api.patchNap(id, body), onSuccess: invalidate });

  const presets = day?.presets ?? todayNap?.presets ?? [];
  const total = todayNap?.total ?? 0;

  const Wrap = compact ? "div" : Card;
  return (
    <Wrap>
      {compact ? (
        <button onClick={() => setOpen(true)}
                className="flex w-full items-center gap-2 rounded-xl bg-slate-800 p-3 text-left">
          <span className="text-sm">😴</span>
          <span className={`flex-1 truncate text-xs ${total > 0 ? "text-ink" : "text-slate-500"}`}>
            {total > 0 ? `${Math.round(total)} min napped` : "Log nap"}
          </span>
          <span className="text-xs text-slate-600">+</span>
        </button>
      ) : (<>
      <Eyebrow left="NAPS" right={total > 0 ? `${Math.round(total)} MIN TODAY` : "NONE TODAY"} />
      <button onClick={() => setOpen(true)}
              className="mt-2 flex w-full items-center gap-3 rounded-xl bg-surface px-3 py-3 text-left transition-colors hover:bg-rule">
        <span className="text-[18px]">😴</span>
        <span className="flex-1">
          <span className="block text-[12px] text-ink">
            {total > 0 ? `${Math.round(total)} min napped` : "Log a nap"}
          </span>
          <span className="block text-[9.5px] text-ink-3">start time + duration</span>
        </span>
        <span className="text-[11px] text-ink-3">+</span>
      </button>
      </>)}

      {open && (
        <Sheet title="Log a nap" onClose={() => setOpen(false)}>
          <WhenRow date={date} setDate={fixedDate ? undefined : setDate} time={time} setTime={setTime} />
          <p className="mb-2 text-[10px] text-ink-3">Time is when the nap STARTED.</p>

          <div className="grid grid-cols-3 gap-2">
            {presets.map((p) => (
              <button key={p.id} disabled={log.isPending}
                      onClick={() => log.mutate({ date, at_time: time, kind: p.id })}
                      className="rounded-lg bg-surface px-2 py-2.5 text-center disabled:opacity-50">
                <div className="text-[15px]">{p.emoji}</div>
                <div className="mt-0.5 text-[10.5px] text-ink">{p.label}</div>
                <div className="text-[9px] text-ink-3">{p.minutes} min</div>
              </button>
            ))}
          </div>

          <div className="mt-3 border-t border-rule pt-3">
            <p className="text-[9px] tracking-micro text-ink-3">CUSTOM</p>
            <div className="mt-1.5 flex items-center gap-2">
              <input inputMode="numeric" value={mins} onChange={(e) => setMins(e.target.value)}
                     placeholder="min"
                     className="w-20 rounded-lg bg-surface px-2 py-1.5 text-[13px] text-ink outline-none" />
              <button disabled={!mins || log.isPending}
                      onClick={() => { log.mutate({ date, at_time: time, minutes: parseFloat(mins) }); setMins(""); }}
                      className="rounded-lg bg-surface px-3 py-1.5 text-[12px] text-ink disabled:opacity-40">Log</button>
            </div>
          </div>

          <p className="mt-3 text-[11px] text-ink-2">
            {date === localToday() ? "Today" : date}: <b>{Math.round(day?.total ?? 0)}</b> min
          </p>
          <EntryList entries={day?.entries} unit="min" onDelete={(id) => del.mutate(id)}
                     onPatch={(id, { at_time, amount }) => patch.mutate({ id, body: { at_time, minutes: amount } })}
                     fmt={(v) => Math.round(v)} amountLabel="min" />
          <Foot>
            Naps are tracked on their own and are NOT added to your night's sleep total or
            sleep debt — neither model was calibrated on daytime sleep.
          </Foot>
        </Sheet>
      )}
    </Wrap>
  );
}
