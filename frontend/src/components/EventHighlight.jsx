import { addDays } from "../utils.js";

// Behaviour-conditioned highlighting for time-series charts. Instead of cluttering
// a chart with vertical lines, the user picks ONE behaviour (alcohol / hard
// workout); the chart then fades the full series to a ghost and foregrounds the days
// associated with that behaviour, plus a subset-mean vs rest-mean contrast so the
// effect is legible, not just located.
//
// Lag-aware: overnight-recovery metrics (HRV, RHR, resp rate) are affected the
// *morning after* a behaviour, so the caller passes lagDays=1 to shift the highlight
// to D+1; same-day metrics (weight) pass lagDays=0.

export const EVENT_TYPES = [
  { key: "hard_workout", label: "Hard workout", color: "#818cf8" },
  { key: "alcohol",      label: "Alcohol",      color: "#f59e0b" },
];

const BY_KEY = Object.fromEntries(EVENT_TYPES.map((t) => [t.key, t]));

export function countEventTypes(events = []) {
  const c = {};
  for (const e of events) c[e.type] = (c[e.type] || 0) + 1;
  return c;
}

/**
 * Single-select behaviour filter. Clicking the active chip clears it.
 */
export function EventFilterPills({ events = [], active, onChange, lagNote }) {
  const counts = countEventTypes(events);
  return (
    <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
      <span className="text-ink-3">Highlight:</span>
      {EVENT_TYPES.map((t) => {
        const on = active === t.key;
        const n = counts[t.key] || 0;
        return (
          <button
            key={t.key}
            disabled={n === 0}
            onClick={() => onChange(on ? null : t.key)}
            className={`flex items-center gap-1 rounded-full px-2 py-0.5 transition-all ${
              on ? "ring-1" : "opacity-70 hover:opacity-100"
            } ${n === 0 ? "opacity-25" : ""}`}
            style={{ color: t.color, ...(on ? { background: t.color + "22", "--tw-ring-color": t.color } : {}) }}
          >
            <span className="inline-block h-2 w-2 rounded-full" style={{ background: t.color }} />
            {t.label}{n > 0 ? ` (${n})` : ""}
          </button>
        );
      })}
      {active && lagNote && <span className="text-ink-3">· {lagNote}</span>}
    </div>
  );
}

/**
 * Augment a chart's data array with a `_hl` field (the metric value on highlighted
 * days, else null) and compute subset/rest means.
 *
 * @returns { data, subsetMean, otherMean, nSubset, color, label, active }
 */
export function computeHighlight({ events = [], activeType, lagDays = 0, data = [], isoKey = "iso", valueKey }) {
  if (!activeType) return { data, active: false };

  const hlSet = new Set(
    events.filter((e) => e.type === activeType).map((e) => addDays(e.date, lagDays))
  );

  let sSum = 0, sN = 0, oSum = 0, oN = 0;
  const out = data.map((d) => {
    const v = d[valueKey];
    const isHl = hlSet.has(d[isoKey]);
    if (v != null) {
      if (isHl) { sSum += v; sN += 1; }
      else      { oSum += v; oN += 1; }
    }
    return { ...d, _hl: isHl ? v : null };
  });

  const t = BY_KEY[activeType] || {};
  return {
    data: out,
    active: true,
    nSubset: sN,
    subsetMean: sN ? sSum / sN : null,
    otherMean: oN ? oSum / oN : null,
    color: t.color,
    label: t.label,
  };
}

/**
 * One-line contrast summary, e.g. "Alcohol days (next morning): 41 vs 49 (others)".
 * `fmt` formats a number with units. `goodHigh`: whether a higher value is better
 * (HRV yes, RHR no) — colours the difference; null leaves it neutral.
 */
export function HighlightContrast({ hl, lagDays = 0, fmt = (x) => Math.round(x), goodHigh = null }) {
  if (!hl?.active) return null;
  if (!hl.nSubset || hl.subsetMean == null || hl.otherMean == null) {
    return <p className="mb-1 text-xs text-ink-3">No paired data for {hl.label?.toLowerCase()} days in this range.</p>;
  }
  const delta = hl.subsetMean - hl.otherMean;
  const deltaColor = goodHigh == null ? "var(--ink-2)"
    : (delta >= 0) === goodHigh ? "var(--rhr)" : "var(--over)";
  return (
    <p className="mb-1 text-xs">
      <span style={{ color: hl.color }}>{hl.label} days{lagDays ? " (next morning)" : ""}</span>
      <span className="text-ink-3">: </span>
      <span className="font-semibold text-ink">{fmt(hl.subsetMean)}</span>
      <span className="text-ink-3"> vs {fmt(hl.otherMean)} on others </span>
      <span style={{ color: deltaColor }}>
        ({delta >= 0 ? "+" : ""}{fmt(delta)})
      </span>
      <span className="text-ink-3"> · n={hl.nSubset}</span>
    </p>
  );
}
