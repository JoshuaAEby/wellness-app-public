import { useEffect, useState } from "react";
import { discardFailed, failedItems, queuedItems, removeQueued, subscribe } from "../offline.js";

// Offline / sync status under the header (2026-10-21). Renders nothing when online with
// an empty queue. Offline: says how old the data on screen is and how many logs are
// waiting; tap for the list (a queued log can be cancelled, a rejected one discarded).
const hm = (d) => (d ? new Date(d).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : null);

export default function OfflineBanner({ onSyncNow }) {
  const [st, setSt] = useState(null);
  const [open, setOpen] = useState(false);
  useEffect(() => subscribe(setSt), []);
  if (!st) return null;
  const { offline, staleAt, queued, failed, syncing } = st;
  if (!offline && !queued && !failed) return null;

  const parts = [];
  if (offline) parts.push(staleAt ? `Offline — showing data from ${hm(staleAt)}` : "Offline");
  if (syncing) parts.push(`syncing ${queued}…`);
  else if (queued) parts.push(`${queued} log${queued === 1 ? "" : "s"} waiting to sync`);
  if (failed) parts.push(`${failed} couldn't sync`);

  return (
    <div className="border-b border-rule px-4 py-1.5 text-[11px]"
         style={{ background: "color-mix(in srgb, var(--breakfast, #f59e0b) 12%, var(--canvas))" }}>
      <button type="button" onClick={() => setOpen((v) => !v)} className="flex w-full items-center gap-2 text-left">
        <span className="flex-1 text-ink-2">{parts.join(" · ")}</span>
        {queued > 0 && !syncing && (
          <span role="button" tabIndex={0} onClick={(e) => { e.stopPropagation(); onSyncNow?.(); }}
                className="font-semibold text-ink">Sync now</span>
        )}
        {(queued > 0 || failed > 0) && <span className="text-ink-3">{open ? "▾" : "▸"}</span>}
      </button>
      {open && (
        <div className="mt-1.5 space-y-1 pb-1">
          {queuedItems().map((q) => (
            <div key={q.key} className="flex items-center gap-2 text-ink-3">
              <span className="flex-1 truncate">⏳ {q.label} · {hm(q.queued_at)}</span>
              <button type="button" onClick={() => removeQueued(q.key)} className="text-ink-3">cancel</button>
            </div>
          ))}
          {failedItems().map((f) => (
            <div key={f.key} className="flex items-center gap-2" style={{ color: "var(--over)" }}>
              <span className="flex-1 truncate">✕ {f.label} — {f.error}</span>
              <button type="button" onClick={() => discardFailed(f.key)} className="text-ink-3">discard</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
