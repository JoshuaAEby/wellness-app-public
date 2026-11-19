import { useState } from "react";
import { ReferenceLine } from "recharts";
import { FLAG_STYLE } from "./FlagBands.jsx";

// Shared chart helpers so the "glowing latest point" and "typical range" band
// look identical across every tab (Recovery / Body / Training).

// One style per KIND of change (interventions.category, Alembic 0066 — notes 32/33).
// The chart shows only the glyph; the words live in ChartEventKey below the chart.
// Shape carries the meaning as well as colour, so the key survives colour-blindness and
// the low-opacity band colours it sits beside. NULL/unknown categories draw as "other".
export const EVENT_CATEGORY = {
  supplement: { label: "Supplement", glyph: "●", color: "#f59e0b" },
  training:   { label: "Training",   glyph: "▲", color: "#e879f9" },
  diet:       { label: "Diet",       glyph: "◆", color: "#4ade80" },
  sleep:      { label: "Sleep",      glyph: "■", color: "#818cf8" },
  health:     { label: "Health",     glyph: "✚", color: "#22d3ee" },
  other:      { label: "Other",      glyph: "○", color: "#94a3b8" },
};
export const eventStyle = (iv) => EVENT_CATEGORY[iv?.category] ?? EVENT_CATEGORY.other;

// Vertical markers for flagged interventions, usable on any chart. `rows` are the chart
// data; isoKey = the raw-date field used to place the line, xKey = the x-axis category
// field. Returns an array of <ReferenceLine> — a faint dashed line topped by the
// category glyph — for interventions inside the window.
//
// Until 2026-09-14 each line carried the label cut to 10 characters, and an intervention
// dated BEFORE the window snapped onto its first row (findIndex(>= date) is 0 for any
// earlier date) — so March's creatine sat on the left edge of every 1M chart.
export function interventionMarkers(interventions, rows, isoKey = "iso", xKey = "date") {
  if (!interventions?.length || !rows?.length) return null;
  const first = rows.find((r) => r[isoKey])?.[isoKey];
  return interventions.map((iv) => {
    if (first && iv.date < first) return null;
    const i = rows.findIndex((r) => r[isoKey] && r[isoKey] >= iv.date);
    if (i < 0) return null;
    const s = eventStyle(iv);
    return (
      <ReferenceLine key={iv.id} x={rows[i][xKey]} stroke={s.color} strokeDasharray="3 3"
        strokeOpacity={0.45} ifOverflow="hidden"
        label={<MarkerLabel iv={iv} />} />
    );
  }).filter(Boolean);
}

// The glyph at the top of a marker, and TAPPING IT opens a callout with the date, full
// label and notes (note 39 — the key under the chart was not what "click the marker"
// meant). Recharts clones this element with the line's `viewBox`; state lives here, so
// each marker toggles on its own. The 24 px transparent square is the touch target — a
// 10 px glyph alone is too small to hit on a phone. The callout sits on whichever side
// of the line has room.
function MarkerLabel({ viewBox, iv }) {
  const [open, setOpen] = useState(false);
  if (!viewBox || viewBox.x == null) return null;
  const { x, y = 0, height = 120 } = viewBox;
  const s = eventStyle(iv);
  const W = 156;
  const bx = x > W + 10 ? x - W - 6 : x + 6;
  return (
    <g style={{ cursor: "pointer" }} onClick={(e) => { e.stopPropagation(); setOpen((v) => !v); }}>
      <rect x={x - 12} y={y - 2} width={24} height={24} fill="transparent" />
      <text x={x} y={y + 12} textAnchor="middle" fill={s.color} fontSize={11}>{s.glyph}</text>
      {open && (
        <foreignObject x={bx} y={y + 2} width={W} height={Math.max(40, Math.min(height - 4, 96))}>
          <div xmlns="http://www.w3.org/1999/xhtml"
            style={{ background: "var(--canvas)", border: `1px solid ${s.color}`, borderRadius: 6,
                     padding: "4px 6px", fontSize: 10, lineHeight: 1.3, color: "var(--ink-2)",
                     maxHeight: "100%", overflow: "hidden" }}>
            <div style={{ color: "var(--ink-3)" }}>
              {fmtIso(iv.date)} · <span style={{ color: s.color }}>{s.glyph} {s.label}</span>
            </div>
            <div style={{ color: "var(--ink)" }}>{iv.label}</div>
            {iv.notes && <div style={{ color: "var(--ink-3)" }}>{iv.notes}</div>}
          </div>
        </foreignObject>
      )}
    </g>
  );
}

const fmtIso = (iso) =>
  new Date(`${iso}T00:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" });

// The key for everything drawn BEHIND a chart's data: intervention glyphs and flagged-day
// bands (FlagBands). One line of chips — only what is inside this chart's window — that
// taps open to a dated list with each change's full label and notes. Pass `runs` only
// when the chart draws bands. Renders nothing when neither applies.
export function ChartEventKey({ interventions, runs, rows, isoKey = "iso" }) {
  const [open, setOpen] = useState(false);
  const isos = (rows ?? []).map((r) => r?.[isoKey]).filter(Boolean).sort();
  if (!isos.length) return null;
  const first = isos[0];
  const last = isos[isos.length - 1];
  const ivs = (interventions ?? []).filter((iv) => iv.date >= first && iv.date <= last);
  const rs = (runs ?? []).filter((r) => r.endIso >= first && r.startIso <= last);
  if (!ivs.length && !rs.length) return null;

  const cats = new Map();
  for (const iv of ivs) {
    const k = EVENT_CATEGORY[iv.category] ? iv.category : "other";
    cats.set(k, (cats.get(k) ?? 0) + 1);
  }
  const reasons = new Map();
  for (const r of rs) reasons.set(r.reason, (reasons.get(r.reason) ?? 0) + r.days);
  const entries = [
    ...ivs.map((iv) => ({ iso: iv.date, iv })),
    ...rs.map((r) => ({ iso: r.startIso, run: r })),
  ].sort((a, b) => (a.iso < b.iso ? 1 : -1));

  return (
    <div className="mt-1.5">
      <button onClick={() => setOpen((v) => !v)}
        className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-left text-[9.5px] text-ink-3 hover:text-ink-2">
        {[...cats].map(([k, n]) => (
          <span key={k} className="inline-flex items-center gap-1">
            <span style={{ color: EVENT_CATEGORY[k].color }}>{EVENT_CATEGORY[k].glyph}</span>
            {EVENT_CATEGORY[k].label}{n > 1 ? ` ×${n}` : ""}
          </span>
        ))}
        {[...reasons].map(([reason, days]) => {
          const st = FLAG_STYLE[reason] ?? FLAG_STYLE.other;
          return (
            <span key={reason} className="inline-flex items-center gap-1">
              <span className="inline-block h-2 w-3 rounded-[2px]"
                style={{ background: st.fill, opacity: Math.min(st.opacity * 3, 0.5) }} />
              {st.label} · {days}d
            </span>
          );
        })}
        <span>{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <ul className="mt-1.5 space-y-1 border-l border-rule pl-2">
          {entries.map((e) => {
            if (e.iv) {
              const s = eventStyle(e.iv);
              return (
                <li key={`iv-${e.iv.id}`} className="text-[11px] leading-snug text-ink-2">
                  <span className="tabular-nums text-ink-3">{fmtIso(e.iso)}</span>{" "}
                  <span style={{ color: s.color }}>{s.glyph}</span> {e.iv.label}
                  {e.iv.notes && <span className="block pl-10 text-[10px] text-ink-3">{e.iv.notes}</span>}
                </li>
              );
            }
            const st = FLAG_STYLE[e.run.reason] ?? FLAG_STYLE.other;
            return (
              <li key={`fl-${e.run.reason}-${e.run.startIso}`} className="text-[11px] leading-snug text-ink-2">
                <span className="tabular-nums text-ink-3">
                  {fmtIso(e.run.startIso)}{e.run.days > 1 ? `–${fmtIso(e.run.endIso)}` : ""}
                </span>{" "}
                <span className="inline-block h-2 w-3 rounded-[2px] align-middle"
                  style={{ background: st.fill, opacity: Math.min(st.opacity * 3, 0.5) }} />{" "}
                {st.label}{e.run.days > 1 ? ` (${e.run.days} days)` : ""}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

// Index of the last non-null value for a key — where the glow dot goes.
export function lastValidIndex(data, key) {
  for (let i = data.length - 1; i >= 0; i--) {
    if (data[i]?.[key] != null) return i;
  }
  return -1;
}

// Recharts `dot` render-prop: draws an Apple-style glowing marker on ONLY the
// last valid point (a soft halo + solid core), nothing on the others.
export function glowDot(color, lastIndex) {
  return function GlowDot(props) {
    const { cx, cy, index } = props;
    if (index !== lastIndex || cx == null || cy == null) return null;
    return (
      <g key={`glow-${index}`} style={{ pointerEvents: "none" }}>
        <circle cx={cx} cy={cy} r={7} fill={color} fillOpacity={0.22} />
        <circle cx={cx} cy={cy} r={3.5} fill={color} stroke="var(--card)" strokeWidth={1} />
      </g>
    );
  };
}

// Map a tab's window selector to the trend-detection lookback (days), so a chart's
// trend badge reflects the same span the user is looking at.
export function windowToTrendDays(window) {
  return { "1M": 30, "3M": 90, "6M": 180, "1Y": 365 }[window] ?? 90;
}

// Inline trend badge for a chart header. `trend` is one row from /api/insights/trends
// (keyed by metric). Renders nothing for steady/missing metrics.
const _TREND_SENTIMENT = { good: "var(--rhr)", bad: "var(--over)", neutral: "var(--ink-3)" };
export function TrendBadge({ trend }) {
  if (!trend || trend.tier === "flat") return null;
  const color = _TREND_SENTIMENT[trend.sentiment] ?? "var(--ink-3)";
  const v = trend.per_week;
  const rate = `${v > 0 ? "+" : ""}${Math.abs(v) >= 10 ? Math.round(v) : v.toFixed(2)}${trend.unit}/wk`;
  return (
    <span className="inline-flex items-center gap-0.5 text-xs font-semibold" style={{ color }}
      title={`${trend.tier} trend (Mann–Kendall, FDR-corrected)`}>
      {trend.direction === "rising" ? "↑" : "↓"} {rate}
    </span>
  );
}

// Add a typical-range `band: [lo, hi]` and a directional `danger` threshold per day.
//   center  = row[centerKey] if given (e.g. the backend EWMA baseline) else rolling mean
//   width   = k · trailing-window SD  (k=1 ⇒ ±1σ ⇒ z=±1, the recovery-alarm boundary)
//   badDir  = "low" | "high"  → which side trips the alarm
//   absOffset = fixed offset from center for absolute-threshold alarms (e.g. resp +1.5 bpm,
//               wrist temp +1.5°F) instead of a σ-based one.
// The `danger` line is the exact alarm threshold in the metric's own units, so a reading
// crossing it is literally the event that flips the composite Recovery status.
export function addBands(data, valueKey, { win = 30, k = 1, badDir = null, absOffset = null, centerKey = null } = {}) {
  const r2 = (x) => parseFloat(x.toFixed(2));
  return data.map((row, i) => {
    const slice = data.slice(Math.max(0, i - win + 1), i + 1)
      .map((r) => r[valueKey]).filter((v) => v != null);
    if (slice.length < 5) return { ...row, band: null, danger: null };
    const mean = slice.reduce((a, b) => a + b, 0) / slice.length;
    const sd = Math.sqrt(slice.reduce((a, b) => a + (b - mean) ** 2, 0) / slice.length);
    const center = (centerKey && row[centerKey] != null) ? row[centerKey] : mean;
    // Band half-width = the alarm offset (absolute) or k·σ. Keeping band width == alarm
    // distance means the alarm line always sits exactly on the band's bad edge (no gap).
    const width = absOffset != null ? absOffset : k * sd;
    let danger = null;
    if (badDir === "low")  danger = r2(center - width);
    if (badDir === "high") danger = r2(center + width);
    return { ...row, band: [r2(center - width), r2(center + width)], danger };
  });
}

// Glow color for a TRENDING metric (no band): green/red/neutral by the trend's sentiment
// (from /api/insights/trends), else the metric's own color. Directional cue without a band.
export function trendGlowColor(trendRow, fallback) {
  if (!trendRow || trendRow.tier === "flat") return fallback;
  return { good: "var(--rhr)", bad: "var(--over)", neutral: "var(--ink-3)" }[trendRow.sentiment] ?? fallback;
}

// True when the latest reading sits on the alarm side of its threshold. Exported so
// callers can ask the question directly instead of string-comparing a colour — the
// old idiom (`glow === "#f87171"`) silently broke the moment colours became tokens.
export function isAlarming(data, valueKey, badDir) {
  const i = lastValidIndex(data, valueKey);
  if (i < 0 || badDir == null) return false;
  const v = data[i][valueKey], d = data[i].danger;
  if (v == null || d == null) return false;
  return badDir === "low" ? v < d : v > d;
}

// Glow color for the latest point: red if it's on the bad side of the alarm threshold,
// otherwise the metric's own (good) color. Ties the endpoint marker to the alarm.
export function glowColorFor(data, valueKey, badDir, goodColor) {
  const i = lastValidIndex(data, valueKey);
  if (i < 0) return goodColor;
  const v = data[i][valueKey], d = data[i].danger;
  if (d == null || badDir == null) return goodColor;
  const bad = badDir === "low" ? v < d : v > d;
  return bad ? "var(--over)" : goodColor;
}
