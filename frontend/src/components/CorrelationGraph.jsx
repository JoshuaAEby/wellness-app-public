import { useMemo, useState } from "react";

// Radial node-link view of the correlation engine. One outcome sits at the centre;
// its candidate drivers radiate out on curved links. Each edge encodes:
//   • width   ∝ |ρ| (normalized within the view, so differences are legible)
//   • colour  = sign of ρ
//   • style   = tier (solid/bright confirmed, dashed suggestive, faint dotted below)
// Node radius scales with n (sample size → visual confidence). Tapping a node
// surfaces ρ, n, and the 95% CI — the numbers the list view hides.

const POS = "#34d399";
const NEG = "#f87171";

function trunc(s, n = 18) {
  return s && s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function tier(e) {
  if (e.significant) return { opacity: 0.95, dash: undefined, name: "confirmed" };
  if (e.suggestive) return { opacity: 0.7, dash: "5 4", name: "suggestive" };
  return { opacity: 0.22, dash: "1 5", name: "below threshold" };
}

export default function CorrelationGraph({ results = [] }) {
  const [showAll, setShowAll] = useState(false);
  const [selected, setSelected] = useState(null);
  const [focus, setFocus] = useState(null);

  const outcomes = useMemo(() => {
    const seen = new Map();
    const score = {};
    for (const r of results) {
      if (!seen.has(r.metric_b)) seen.set(r.metric_b, r.metric_b_label || r.metric_b);
      if (r.significant || r.suggestive) score[r.metric_b] = (score[r.metric_b] || 0) + 1;
    }
    return [...seen.entries()]
      .map(([key, label]) => ({ key, label, score: score[key] || 0 }))
      .sort((a, b) => b.score - a.score);
  }, [results]);

  const focusKey = focus ?? outcomes[0]?.key;
  const focusLabel = outcomes.find((o) => o.key === focusKey)?.label ?? focusKey;

  const edges = useMemo(() => {
    const best = new Map();
    for (const r of results) {
      if (r.metric_b !== focusKey || r.spearman_r == null) continue;
      const prev = best.get(r.metric_a);
      if (!prev || Math.abs(r.spearman_r) > Math.abs(prev.spearman_r)) best.set(r.metric_a, r);
    }
    let arr = [...best.values()];
    if (!showAll) arr = arr.filter((e) => e.significant || e.suggestive);
    return arr.sort((a, b) => Math.abs(b.spearman_r) - Math.abs(a.spearman_r));
  }, [results, focusKey, showAll]);

  // Encoding ranges, normalized within the current view so strength reads clearly.
  const absR = edges.map((e) => Math.abs(e.spearman_r));
  const rMin = Math.min(...absR, 0), rMax = Math.max(...absR, 0.01);
  const ns = edges.map((e) => e.n || 0);
  const nMax = Math.max(...ns, 1);
  const strokeFor = (e) => 2.5 + ((Math.abs(e.spearman_r) - rMin) / (rMax - rMin || 1)) * 11;
  const nodeR = (e) => 4 + Math.min((e.n || 0) / nMax, 1) * 5;

  // ── Geometry ────────────────────────────────────────────────────────────────
  const W = 460, H = 430, cx = 230, cy = 205, R = 122;
  const nodes = edges.map((e, i) => {
    const ang = -Math.PI / 2 + (i * 2 * Math.PI) / Math.max(edges.length, 1);
    return { e, x: cx + R * Math.cos(ang), y: cy + R * Math.sin(ang), cos: Math.cos(ang) };
  });

  // Gentle curved link from centre to a node.
  const curve = (x, y) => {
    const mx = (cx + x) / 2, my = (cy + y) / 2;
    const dx = x - cx, dy = y - cy;
    const len = Math.hypot(dx, dy) || 1;
    const ox = (-dy / len) * len * 0.12, oy = (dx / len) * len * 0.12;
    return `M ${cx} ${cy} Q ${mx + ox} ${my + oy} ${x} ${y}`;
  };

  return (
    <div>
      {/* Outcome selector */}
      <div className="mb-2 flex flex-wrap gap-1.5">
        {outcomes.map((o) => (
          <button
            key={o.key}
            onClick={() => { setFocus(o.key); setSelected(null); }}
            className={`rounded-full px-2.5 py-1 text-xs font-medium transition-colors ${
              o.key === focusKey ? "bg-indigo-600 text-white" : "bg-slate-700 text-slate-400 hover:text-white"
            }`}
          >
            {o.label}{o.score > 0 ? ` ·${o.score}` : ""}
          </button>
        ))}
      </div>

      <div className="mb-1 flex items-center justify-between text-xs">
        <span className="text-slate-500">Drivers of <span className="text-slate-300">{focusLabel}</span></span>
        <button
          onClick={() => setShowAll((v) => !v)}
          className={`rounded-full px-2 py-0.5 transition-colors ${showAll ? "bg-slate-700 text-slate-200" : "bg-slate-800 text-slate-500"}`}
        >
          {showAll ? "All pairs" : "Notable only"}
        </button>
      </div>

      {edges.length === 0 ? (
        <p className="py-10 text-center text-sm text-slate-500">No {showAll ? "" : "notable "}drivers for this outcome yet.</p>
      ) : (
        <svg viewBox={`0 0 ${W} ${H}`} className="w-full">
          <defs>
            <filter id="glow" x="-50%" y="-50%" width="200%" height="200%">
              <feGaussianBlur stdDeviation="3.5" result="b" />
              <feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge>
            </filter>
          </defs>

          {/* Edges (curved). Entrance fade is on the group's `opacity` (keyed to the
              focused outcome so it replays on switch) — leaving each path's
              `strokeOpacity` free for prop-driven selection emphasis. */}
          <g key={focusKey}>
            <animate attributeName="opacity" from="0" to="1" dur="0.55s" fill="freeze" />
            {nodes.map(({ e, x, y }) => {
              const t = tier(e);
              const isSel = selected?.metric_a === e.metric_a;
              const color = e.spearman_r >= 0 ? POS : NEG;
              const w = strokeFor(e);
              return (
                <path
                  key={`e-${e.metric_a}`} d={curve(x, y)} fill="none"
                  stroke={color} strokeWidth={isSel ? w + 2 : w}
                  strokeOpacity={isSel ? 1 : t.opacity} strokeDasharray={t.dash} strokeLinecap="round"
                />
              );
            })}
          </g>

          {/* Centre (outcome) */}
          <circle cx={cx} cy={cy} r={34} fill="#0f172a" stroke="#818cf8" strokeWidth={2.5} filter="url(#glow)" />
          <text x={cx} y={cy - 3} textAnchor="middle" fill="#e2e8f0" fontSize={11} fontWeight="600">
            {trunc(focusLabel.split(" (")[0], 13)}
          </text>
          <text x={cx} y={cy + 11} textAnchor="middle" fill="#64748b" fontSize={8}>outcome</text>

          {/* Input nodes + labels */}
          {nodes.map(({ e, x, y, cos }) => {
            const isSel = selected?.metric_a === e.metric_a;
            const color = e.spearman_r >= 0 ? POS : NEG;
            const anchor = cos > 0.25 ? "start" : cos < -0.25 ? "end" : "middle";
            const lx = x + (cos > 0.25 ? 9 : cos < -0.25 ? -9 : 0);
            const ly = y > cy ? y + 14 : y - 11;
            const t = tier(e);
            return (
              <g key={`n-${e.metric_a}`} onClick={() => setSelected(isSel ? null : e)} style={{ cursor: "pointer" }}>
                <circle cx={x} cy={y} r={isSel ? nodeR(e) + 2 : nodeR(e)} fill={color}
                  fillOpacity={Math.max(t.opacity, 0.55)} stroke={isSel ? "#fff" : "none"} strokeWidth={1.5}
                  filter={isSel ? "url(#glow)" : undefined} />
                <text x={lx} y={ly} textAnchor={anchor} fill={isSel ? "#f1f5f9" : "#cbd5e1"} fontSize={9.5}>
                  {trunc(e.metric_a_label)}
                </text>
              </g>
            );
          })}
        </svg>
      )}

      {/* Detail panel — surfaces the CI the list view hides */}
      {selected && (
        <div className="mt-1 rounded-lg bg-slate-900/70 p-3 text-xs">
          <p className="mb-1 text-slate-200">
            <span style={{ color: (selected.spearman_r ?? 0) >= 0 ? POS : NEG }}>
              {(selected.spearman_r ?? 0) >= 0 ? "↑" : "↓"} {selected.metric_a_label}
            </span>
            <span className="text-slate-500"> → {selected.metric_b_label}</span>
          </p>
          <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-slate-400">
            <span>ρ = <span className="text-slate-200">{selected.spearman_r?.toFixed(2)}</span></span>
            {selected.ci_low != null && selected.ci_high != null && (
              <span>95% CI [{selected.ci_low.toFixed(2)}, {selected.ci_high.toFixed(2)}]</span>
            )}
            <span>n = {selected.n}{selected.n_eff != null ? ` (n_eff ${selected.n_eff})` : ""}</span>
            <span>lag {selected.lag_days}d</span>
            <span style={{ color: selected.significant ? POS : selected.suggestive ? "#fbbf24" : "#64748b" }}>
              {tier(selected).name}
            </span>
          </div>
        </div>
      )}

      {/* Legend */}
      <div className="mt-2 flex flex-wrap gap-3 text-xs text-slate-500">
        <span style={{ color: POS }}>● positive</span>
        <span style={{ color: NEG }}>● negative</span>
        <span>— confirmed</span>
        <span>– – suggestive</span>
        <span>thickness ∝ |ρ| · node ∝ n</span>
      </div>
    </div>
  );
}
