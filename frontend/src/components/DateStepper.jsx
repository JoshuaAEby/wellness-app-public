import { addDays } from "../utils.js";

// Date input with prev/next day arrows, so stepping through days does not require
// opening the native calendar every time.
//
// The forward arrow is hidden (not removed) at `max` — using `visibility` keeps the
// control from shifting sideways as you step, which on a small inline widget is more
// disorienting than the arrow simply going blank.
//
// Stepping goes through utils.addDays, which anchors at local noon: naive date maths
// on an ISO string rolls over a day in either direction across a DST boundary.
export default function DateStepper({
  value,
  onChange,
  max = null,
  min = null,
  className = "",
  inputClassName = "rounded-lg bg-slate-700 px-2 py-1 text-xs text-white outline-none focus:ring-2 focus:ring-indigo-500",
}) {
  const atMax = max != null && value >= max;
  const atMin = min != null && value <= min;
  const btn = "px-1.5 py-1 text-sm leading-none text-slate-400 transition-colors " +
              "hover:text-white disabled:opacity-30 disabled:hover:text-slate-400";

  return (
    <div className={`flex items-center gap-0.5 ${className}`}>
      <button
        type="button"
        onClick={() => onChange(addDays(value, -1))}
        disabled={atMin}
        aria-label="Previous day"
        className={btn}
      >
        ‹
      </button>
      <input
        type="date"
        value={value}
        max={max ?? undefined}
        min={min ?? undefined}
        onChange={(e) => e.target.value && onChange(e.target.value)}
        className={`${inputClassName} [color-scheme:dark]`}
      />
      <button
        type="button"
        onClick={() => onChange(addDays(value, 1))}
        disabled={atMax}
        aria-label="Next day"
        className={btn}
        style={atMax ? { visibility: "hidden" } : undefined}
      >
        ›
      </button>
    </div>
  );
}
