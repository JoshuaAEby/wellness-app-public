import { useState } from "react";

/**
 * Full-size plot for one scorecard row (note 68): tap a row on either scorecard and see
 * the whole series over time, not just a 64-px sparkline.
 *
 * Hand-drawn SVG on the design tokens, like the redesigned charts (TrainingLoadChart):
 * it themes correctly in light and dark, and tapping a point shows its value — hover
 * tooltips do nothing on the phone, which is where this is used.
 *
 * Shaded bands mark the two windows the verdict compares (the reference period and the
 * recent one), so the plot shows exactly where "before" and "now" come from. A dashed
 * line marks the target, when the row has one.
 */
const W = 340, H = 210, L = 36, R = 10, T = 16, B = 24;
const DAY = 86400000;
const GAP_DAYS = 21;          // a longer gap between points is drawn dashed, not as data

const fmtDay = (iso) =>
  new Date(`${iso}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" });
const fmtMonth = (t) => new Date(t).toLocaleDateString(undefined, { month: "short" });
const fmtShort = (t) => new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });
const nice = (v) => (Math.abs(v) >= 100 ? Math.round(v) : Math.round(v * 10) / 10);
const ms = (iso) => new Date(`${iso}T12:00:00`).getTime();

// Round tick values (1/2/2.5/5 × 10^k) covering [lo, hi] — "2.9, 15.3, 27.7" was unreadable.
function niceTicks(lo, hi, integer, want = 4) {
  const raw = (hi - lo) / want || 1;
  const mag = 10 ** Math.floor(Math.log10(raw));
  let step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((st) => st >= raw) ?? 10 * mag;
  if (integer) step = Math.max(1, Math.round(step));
  const t0 = Math.floor(lo / step) * step, t1 = Math.ceil(hi / step) * step;
  const ticks = [];
  for (let v = t0; v <= t1 + step / 2; v += step) ticks.push(Math.round(v * 1000) / 1000);
  return ticks;
}

/**
 * Axis rules (from the 2026-10-04 once-over on real data):
 *  - domain: fixed range when given ("% of days" rows use 0–100).
 *  - a padded axis never dips below 0 for data that can't (CTL read −1.8, sessions −0.5);
 *  - a zero line is drawn whenever the data crosses zero (vs break-even);
 *  - spans under ~6 weeks get day labels (the anchor plot had none);
 *  - gaps longer than GAP_DAYS are dashed (VO₂ max drew straight lines across months);
 *  - target labels live in a legend under the plot — on the plot they collided with data
 *    wherever they were placed;
 *  - bars for counts (sessions/week);
 *  - a comparison band is only shaded when it actually contains data.
 */
// Exported for the summary charts (note 66) — one chart component app-wide.
export function Plot({ points, color, unit, target, targetLabel, targetBand, bands, domain, bars, integer }) {
  const [sel, setSel] = useState(null);
  const pts = points.filter((p) => p.y != null).map((p) => ({ ...p, t: ms(p.x) }));
  if (pts.length < 2) {
    return <p className="py-10 text-center text-[11px] text-ink-3">Not enough data to plot yet.</p>;
  }
  const liveBands = bands.filter((b) => pts.some((p) => p.t >= ms(b.from) - 3 * DAY && p.t <= ms(b.to) + 3 * DAY));
  const t0 = Math.min(...pts.map((p) => p.t), ...liveBands.map((b) => ms(b.from)));
  const t1 = Math.max(...pts.map((p) => p.t), ...liveBands.map((b) => ms(b.to)));
  const dataLo = Math.min(...pts.map((p) => p.y)), dataHi = Math.max(...pts.map((p) => p.y));
  let lo, hi;
  if (domain) { [lo, hi] = domain; } else {
    lo = dataLo; hi = dataHi;
    for (const v of [target, ...(targetBand || [])]) if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    if (bars) lo = Math.min(lo, 0);
    const pad = (hi - lo || Math.abs(hi) || 1) * 0.1;
    lo -= pad; hi += pad;
    if (dataLo >= 0 && lo < 0) lo = 0;
  }
  const ticks = domain ? [0, 25, 50, 75, 100].filter((v) => v >= lo && v <= hi) : niceTicks(lo, hi, integer);
  // Decimals follow the tick STEP: a 0.05 step labelled to one decimal read "1.2, 1.2, 1.1".
  const tickStep = ticks.length > 1 ? Math.abs(ticks[1] - ticks[0]) : 1;
  const tickDp = Math.max(0, Math.min(3, -Math.floor(Math.log10(tickStep) + 1e-9)));
  const fmtTick = (v) => v.toFixed(tickDp);
  if (!domain) { lo = Math.min(lo, ticks[0]); hi = Math.max(hi, ticks[ticks.length - 1]); }
  const x = (t) => L + ((t - t0) / (t1 - t0 || 1)) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo || 1)) * (H - T - B);

  const spanDays = (t1 - t0) / DAY;
  let xTicks = [];
  if (spanDays < 45) {
    const n = Math.min(4, pts.length);
    xTicks = Array.from({ length: n }, (_, i) => pts[Math.round((i * (pts.length - 1)) / Math.max(1, n - 1))].t)
      .map((t) => ({ t, label: fmtShort(t) }));
  } else {
    const months = [];
    for (let d = new Date(t0); d.getTime() <= t1; d.setMonth(d.getMonth() + 1, 1)) {
      const m = new Date(d.getFullYear(), d.getMonth(), 1).getTime();
      if (m >= t0) months.push(m);
    }
    const step = Math.max(1, Math.ceil(months.length / 6));
    xTicks = months.filter((_, i) => i % step === 0).map((t) => ({ t, label: fmtMonth(t) }));
  }

  // Solid runs, broken (and bridged dashed) where consecutive points are far apart.
  const solid = [], dashed = [];
  let run = [pts[0]];
  for (let i = 1; i < pts.length; i++) {
    if ((pts[i].t - pts[i - 1].t) / DAY > GAP_DAYS) {
      solid.push(run); dashed.push([pts[i - 1], pts[i]]); run = [pts[i]];
    } else run.push(pts[i]);
  }
  solid.push(run);
  const path = (rs) => rs.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.y).toFixed(1)}`).join(" ");
  const barW = Math.max(2, Math.min(9, ((W - L - R) / Math.max(1, (t1 - t0) / (7 * DAY))) * 0.6));
  const s = sel != null ? pts[sel] : null;
  const crossesZero = !domain && lo < 0 && hi > 0;
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="Trend over time">
        {liveBands.map((b) => {
          const bx0 = x(ms(b.from)), bx1 = x(ms(b.to));
          return (
            <g key={`${b.label}-${b.from}`}>
              <rect x={bx0} y={T} width={Math.max(1, bx1 - bx0)} height={H - T - B}
                    fill="var(--ink-3)" opacity={b.strong ? 0.16 : 0.09} />
              {/* Unlabelled bands (trips on the steps plot) are explained in the sheet's note —
                  a 3-day strip is too narrow for a label that wouldn't collide. */}
              {b.label && (
                <text x={(bx0 + bx1) / 2} y={T - 4} textAnchor="middle" fontSize="7.5"
                      fill="var(--ink-3)" letterSpacing="0.06em">{b.label}</text>
              )}
            </g>
          );
        })}
        {targetBand && (
          <rect x={L} width={W - L - R} y={y(targetBand[1])} height={Math.max(1, y(targetBand[0]) - y(targetBand[1]))}
                fill="var(--rhr)" opacity="0.10" />
        )}
        {ticks.map((v) => (
          <g key={v}>
            <line x1={L} x2={W - R} y1={y(v)} y2={y(v)} stroke="var(--rule)" opacity="0.6" />
            <text x={L - 4} y={y(v) + 3} textAnchor="end" fontSize="8" fill="var(--ink-3)">{fmtTick(v)}</text>
          </g>
        ))}
        {crossesZero && <line x1={L} x2={W - R} y1={y(0)} y2={y(0)} stroke="var(--ink-2)" strokeWidth="1.2" opacity="0.9" />}
        {xTicks.map(({ t, label }) => {
          // Edge labels anchor inward so they aren't clipped ("Sep 2…" at the right edge).
          const px = x(t);
          const anchor = px > W - R - 18 ? "end" : px < L + 18 ? "start" : "middle";
          return <text key={t} x={px} y={H - 8} textAnchor={anchor} fontSize="8" fill="var(--ink-3)">{label}</text>;
        })}
        {target != null && (
          <line x1={L} x2={W - R} y1={y(target)} y2={y(target)} stroke="var(--ink-2)"
                strokeDasharray="4 3" strokeWidth="1" opacity="0.8" />
        )}
        {bars ? pts.map((p, i) => (
          <rect key={p.x} x={x(p.t) - barW / 2} width={barW} y={y(Math.max(p.y, 0))}
                height={Math.max(0.5, y(0) - y(Math.max(p.y, 0)))} rx="1"
                fill={color} opacity={i === sel ? 1 : 0.7} onClick={() => setSel(i === sel ? null : i)} />
        )) : (
          <>
            {dashed.map(([a, b2]) => (
              <path key={a.x} d={path([a, b2])} fill="none" stroke={color} strokeWidth="1.2"
                    strokeDasharray="3 4" opacity="0.6" />
            ))}
            {solid.map((rs) => rs.length > 1 && (
              <path key={rs[0].x} d={path(rs)} fill="none" stroke={color} strokeWidth="1.8" strokeLinejoin="round" />
            ))}
          </>
        )}
        {!bars && pts.map((p, i) => (
          <g key={p.x} onClick={() => setSel(i === sel ? null : i)} style={{ cursor: "pointer" }}>
            <circle cx={x(p.t)} cy={y(p.y)} r="9" fill="transparent" />
            <circle cx={x(p.t)} cy={y(p.y)} r={i === sel ? 3.6 : 2.2} fill={color}
                    stroke="var(--card)" strokeWidth="1" />
          </g>
        ))}
        {bars && pts.map((p, i) => (
          <rect key={`hit-${p.x}`} x={x(p.t) - 7} width="14" y={T} height={H - T - B}
                fill="transparent" onClick={() => setSel(i === sel ? null : i)} />
        ))}
      </svg>
      {(target != null || targetBand || dashed.length > 0) && (
        <p className="mt-0.5 flex flex-wrap justify-center gap-x-3 text-[9.5px] text-ink-3">
          {target != null && <span><span className="text-ink-2">- - -</span> {targetLabel ?? "target"} {nice(target)}</span>}
          {targetBand && <span><span style={{ color: "var(--rhr)" }}>▮</span> {targetLabel ?? "target range"} {targetBand[0]}–{targetBand[1]}</span>}
          {dashed.length > 0 && <span><span className="text-ink-2">┄</span> gap of 3+ weeks with no reading</span>}
        </p>
      )}
      <p className="mt-1 h-4 text-center text-[11px] tabular-nums text-ink-2">
        {s ? <>{s.label ?? `Week of ${fmtDay(s.x)}`}: <span className="font-semibold text-ink">{nice(s.y)}</span> {unit}</>
           : <span className="text-ink-3">Tap a point for its value</span>}
      </p>
    </div>
  );
}

export default function ScoreChartSheet({ open, onClose, area, title, value, verdict, color,
                                          points, unit, target, targetLabel, targetBand, domain, bars, integer, bands = [], more, extra }) {
  if (!open) return null;
  const lineColor = !color || color === "var(--ink-3)" ? "var(--ink-2)" : color;
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/60" onClick={onClose}>
      <div className="w-full max-w-md rounded-t-2xl border border-rule bg-card p-4 pb-8"
           onClick={(e) => e.stopPropagation()}>
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <p className="text-[8.5px] font-semibold tracking-micro text-ink-3">{area}</p>
            <p className="text-[15px] font-semibold text-ink">{title}</p>
          </div>
          <div className="text-right tabular-nums">
            <p className="text-[14px] font-semibold text-ink">{value}</p>
            <p className="text-[10px] font-semibold" style={{ color: color ?? "var(--ink-2)" }}>{verdict}</p>
          </div>
          <button onClick={onClose} className="ml-1 px-1 text-ink-3" aria-label="Close">✕</button>
        </div>
        <div className="mt-3">
          <Plot points={points || []} color={lineColor} unit={unit} target={target} targetLabel={targetLabel}
                targetBand={targetBand} domain={domain} bars={bars} integer={integer} bands={bands} />
        </div>
        {more && <p className="mt-2 text-[10.5px] leading-snug text-ink-3">{more}</p>}
        {extra}
      </div>
    </div>
  );
}
