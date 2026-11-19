import { windowDates, WINDOW_DAYS, fmtDate } from "../utils.js";

const WINDOWS = ["1W", "1M", "3M", "6M", "1Y"];

/**
 * Window picker with backward paging.
 *
 * The size buttons choose a span; the arrows step that span backwards a whole
 * window at a time. Paging by the window's own length keeps successive views
 * comparable — every 3M page covers 90 days, so two pages sit side by side
 * honestly. A fixed step (say a month) would make each page a different fraction
 * of the axis depending on the size selected.
 *
 * `offset`/`onOffsetChange` are optional: a caller that doesn't pass them gets
 * exactly the old control, so tabs can adopt paging one at a time.
 */
export default function TimeWindowSelector({ value, onChange, offset = 0, onOffsetChange }) {
  const paging = typeof onOffsetChange === "function";
  const { start, end } = windowDates(value, offset);

  return (
    <div>
      <div className="flex gap-1 rounded-lg bg-surface p-1">
        {WINDOWS.map((w) => (
          <button
            key={w}
            onClick={() => { onChange(w); if (paging) onOffsetChange(0); }}
            className={`flex-1 rounded-md py-1 text-[11px] font-medium transition-colors ${
              value === w
                ? "bg-card text-ink ring-1 ring-inset ring-ink-3"
                : "text-ink-3 hover:text-ink-2"
            }`}
          >
            {w}
          </button>
        ))}
      </div>

      {paging && (
        <div className="mt-1.5 flex items-center justify-between">
          {/* Functional updates: two quick taps land in one React batch, and a
              handler closing over `offset` would compute both from the same stale
              value — so a double-tap moved one page instead of two. */}
          <button
            onClick={() => onOffsetChange((o) => (typeof o === "number" ? o : offset) + 1)}
            className="rounded px-1.5 py-0.5 text-[11px] text-ink-3 hover:text-ink"
            aria-label={`Previous ${value}`}
          >◀</button>
          <span className="text-[9.5px] tracking-micro text-ink-3">
            {offset === 0
              ? `LAST ${value}`
              : `${fmtDate(start)} – ${fmtDate(end)}`}
          </span>
          <button
            onClick={() => onOffsetChange((o) => Math.max(0, (typeof o === "number" ? o : offset) - 1))}
            disabled={offset === 0}
            className="rounded px-1.5 py-0.5 text-[11px] text-ink-3 enabled:hover:text-ink disabled:opacity-30"
            aria-label={`Next ${value}`}
          >▶</button>
        </div>
      )}
    </div>
  );
}
