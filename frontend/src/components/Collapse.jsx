import { useState } from "react";

// Collapsible section wrapper. Extracted from Trends.jsx 2026-08-04 so the Today
// tab's character sheet can use the same affordance; Trends imports it from here.
export default function Collapse({ title, hint, defaultOpen = false, onOpenChange, children }) {
  const [open, setOpen] = useState(defaultOpen);
  // `open` is reported upward so a parent can react to it — the Today tab pairs
  // the character sheet and the battery in a 2-up grid, and an expanded card has
  // to break out to full width or its charts render into ~170px.
  const toggle = () => {
    // NOT inside the setState updater: React may call an updater more than once and
    // requires it to be pure, so firing a parent setState from in there can warn or
    // double-fire. Compute the next value here and notify explicitly.
    const next = !open;
    setOpen(next);
    onOpenChange?.(next);
  };
  return (
    <div className="rounded-2xl border border-rule bg-card">
      <button
        type="button"
        onClick={toggle}
        className="flex w-full items-center justify-between px-3 py-2.5"
      >
        <span className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">
          {title}
          {hint && <span className="ml-2 tracking-normal text-ink-3/70">{hint}</span>}
        </span>
        <span className="text-[10px] text-ink-3">{open ? "▾" : "▸"}</span>
      </button>
      {open && <div className="px-0 pb-1 space-y-4">{children}</div>}
    </div>
  );
}
