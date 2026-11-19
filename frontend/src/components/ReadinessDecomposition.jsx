import { useState } from "react";

/**
 * The recovery score drawn as the weighted sum it actually is.
 *
 * Ported from the recovery-decomposition design mockup.
 *
 * A ring shows the answer and hides the arithmetic. Recovery is a fixed weighted
 * mean — HRV 45%, RHR 25%, sleep debt 30% (services/metrics_engine.compute_readiness)
 * — so the score can be drawn as that sum: segment WIDTH is the input's weight,
 * segment FILL is what the input actually returned. In the footprint a ring
 * occupied you can now see which input spent its budget and which didn't, which is
 * the part that tells you what to do.
 *
 * RENORMALISATION IS NOT COSMETIC
 * -------------------------------
 * The backend drops a missing component and renormalises the remaining weights
 * rather than scoring it as average. So on a day with no sleep data, HRV is not
 * "45 of 100" — it is 45/70 = 64% of the score. This component mirrors that
 * exactly: segments are sized by EFFECTIVE weight, and a missing input is shown as
 * an explicit gap rather than silently vanishing. Drawing fixed 45/25/30 segments
 * on a renormalised day would overstate every present input.
 *
 * Suppression: if BOTH HRV and RHR are missing the backend returns no score at all
 * (sleep debt alone is not a recovery score), and this renders nothing.
 */

const BASE = [
  { key: "hrv",   weight: 0.45, name: "HRV",        short: "HRV",   color: "var(--hrv)" },
  { key: "rhr",   weight: 0.25, name: "OVERNIGHT RHR", short: "RHR",   color: "var(--rhr)" },
  { key: "sleep", weight: 0.30, name: "SLEEP DEBT", short: "SLEEP", color: "var(--slp)" },
];

const clip = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

/** Subscores, mirroring compute_readiness exactly. null = component absent. */
export function subscores(row) {
  if (!row) return { hrv: null, rhr: null, sleep: null };
  return {
    hrv:   row.hrv_z_score      != null ? clip(((row.hrv_z_score + 2) / 4) * 100, 0, 100) : null,
    rhr:   row.rhr_z_score      != null ? clip(((-row.rhr_z_score + 2) / 4) * 100, 0, 100) : null,
    sleep: row.sleep_debt_hours != null ? clip(100 - (row.sleep_debt_hours / 2) * 100, 0, 100) : null,
  };
}

export default function ReadinessDecomposition({ row, history = [], readings = {}, onInfo }) {
  const [hover, setHover] = useState(null);
  if (!row) return null;

  const sub = subscores(row);
  // Both autonomic markers absent → the backend suppresses the score entirely.
  if (sub.hrv == null && sub.rhr == null) return null;

  const present = BASE.filter((c) => sub[c.key] != null);
  const missing = BASE.filter((c) => sub[c.key] == null);
  const wsum = present.reduce((s, c) => s + c.weight, 0);
  // Effective weight in POINTS of the final 0–100 score.
  const eff = Object.fromEntries(present.map((c) => [c.key, (c.weight / wsum) * 100]));
  const delivered = Object.fromEntries(present.map((c) => [c.key, (eff[c.key] * sub[c.key]) / 100]));
  const score = Math.round(present.reduce((s, c) => s + delivered[c.key], 0));

  // Rank by SHARE of its own budget returned, not by raw points — otherwise HRV
  // (45 pts) would almost always look "strongest" purely for being the heaviest.
  const share = (k) => delivered[k] / eff[k];
  const ranked = present.slice().sort((a, b) => share(b.key) - share(a.key));
  const best = present.length > 1 ? ranked[0].key : null;
  const worstKey = present.length > 1 ? ranked[ranked.length - 1].key : null;

  const W = 340, X0 = 14, X1 = W - 14, SPAN = X1 - X0, GAP = 4;
  const BAR_Y = 16, BAR_H = 36;

  let x = X0;
  const usable = SPAN - GAP * (present.length - 1);
  const segs = present.map((c) => {
    const w = (usable * eff[c.key]) / 100;
    const seg = { ...c, x, w, fill: (w * sub[c.key]) / 100 };
    x += w + GAP;
    return seg;
  });

  // Trajectory
  const hist = history.map((h) => h.score).filter((v) => v != null);

  // ── Noise floor ───────────────────────────────────────────────────────────
  //
  // Each component maps z in [-2, +2] onto 0-100, so ONE standard deviation of an
  // input is worth 25 points of its component. HRV's median night-to-night move is
  // about 1 sigma, which at 45% weight is ~11 points of score before anything has
  // actually changed about the athlete. Measured over 205 scored days: sd 16.2, but
  // the MEDIAN day-over-day change is 13.4 points — the score moves nearly as much
  // between consecutive mornings as it does across its whole range.
  //
  // So a bare integer overstates its own precision, and a drop that reads as
  // alarming is usually just last night's HRV being a normal amount different from
  // the night before. This band is the honest floor: a move that stays inside it
  // has not said anything.
  //
  // Derived from the displayed history rather than a constant — if the inputs get
  // less noisy, or the weights are refit (see the pre-registered plan in docs/READINESS_REFIT.md),
  // the band narrows on its own instead of lying.
  //
  // Deliberately NOT a fix to the score itself: the pre-registration forbids
  // reweighting before the trigger n, and this is presentation, not model.
  const deltas = hist.slice(1).map((v, i) => Math.abs(v - hist[i]));
  const noise = deltas.length >= 10
    ? [...deltas].sort((a, b) => a - b)[Math.floor(deltas.length / 2)]
    : null;
  const TR_Y = 108, TR_H = 38;
  const lo = hist.length ? Math.min(...hist) : 0;
  const hi = hist.length ? Math.max(...hist) : 100;
  const pad = Math.max(4, (hi - lo) * 0.18);
  const y0 = Math.max(0, lo - pad), y1 = Math.min(100, hi + pad);
  const ty = (v) => TR_Y + TR_H - ((v - y0) / (y1 - y0 || 1)) * TR_H;
  const step = hist.length > 1 ? SPAN / (hist.length - 1) : 0;
  const pts = hist.map((v, i) => [X0 + step * i, ty(v)]);

  return (
    <div className="relative">
      <svg viewBox={`0 0 ${W} 168`} className="block w-full overflow-visible" role="img"
        aria-label="Recovery score decomposed into its weighted inputs, each showing points delivered against points available">
        <text x={X0} y={BAR_Y - 6} fontSize="8" letterSpacing=".16em" fill="var(--ink-3)" fontWeight="600">
          POINTS DELIVERED
        </text>
        <text x={X1} y={BAR_Y - 6} textAnchor="end" fontSize="8.5" fill="var(--ink-2)">{score} / 100</text>

        {segs.map((s) => (
          <g key={s.key} onPointerEnter={() => setHover(s.key)} onPointerLeave={() => setHover(null)}>
            <rect x={s.x} y={BAR_Y} width={s.w} height={BAR_H} rx="3"
              fill="var(--surface)" stroke="var(--rule)" />
            <clipPath id={`clip-${s.key}`}>
              <rect x={s.x} y={BAR_Y} width={s.w} height={BAR_H} rx="3" />
            </clipPath>
            <rect clipPath={`url(#clip-${s.key})`} x={s.x} y={BAR_Y}
              width={Math.max(s.fill, 1.5)} height={BAR_H} fill={s.color} opacity="0.9" />
            {/* Label INSIDE the segment: which input this is, and what it returned
                out of what it could. Ink at full weight reads on both the filled and
                unfilled halves, so it never needs to move as the fill changes. */}
            <text x={s.x + s.w / 2} y={BAR_Y + 15} textAnchor="middle" fontSize="8"
              letterSpacing=".08em" fontWeight="600" fill="var(--ink)">
              {s.short}
            </text>
            <text x={s.x + s.w / 2} y={BAR_Y + 27} textAnchor="middle" fontSize="10.5"
              fontWeight="600" fill="var(--ink)">
              {delivered[s.key].toFixed(0)}
              <tspan fontSize="8" fontWeight="400" fill="var(--ink)" opacity="0.7">
                /{Math.round(eff[s.key])}
              </tspan>
            </text>
          </g>
        ))}

        {/* Per-input rows */}
        {present.map((c, i) => {
          const y = 70 + i * 0;   // rows are HTML below; keep SVG to the bar + trend
          return null;
        })}

        {/* Trajectory */}
        {hist.length > 1 && (
          <>
            <text x={X0} y={TR_Y - 6} fontSize="8" letterSpacing=".16em" fill="var(--ink-3)" fontWeight="600">
              LAST {hist.length} DAYS
            </text>
            {/* The three band edges the score's own labels use (Low 25 / Moderate 50 /
                High 75) — with only one of them drawn you can see that a point is
                below 50 but not whether it is near Low or near High. */}
            {[25, 50, 75].filter(v => v > y0 && v < y1).map(v => (
              <g key={v}>
                <line x1={X0} y1={ty(v)} x2={X1} y2={ty(v)} stroke="var(--rule)" strokeDasharray="3 3" />
                <text x={X0 - 2} y={ty(v) + 3} textAnchor="end" fontSize="7" fill="var(--ink-3)">{v}</text>
              </g>
            ))}
            {/* Noise floor around today: anything landing in here is not news.
                Drawn before the line so the trace stays legible on top of it. */}
            {noise != null && (() => {
              // Edges as well as a fill. A fill alone was tried at opacity 0.055 and
              // was invisible: the trajectory already lays a `var(--ink)` area at 0.05
              // under the line, so the band had nothing to separate it from. The
              // dashed edges are what make it read as a boundary rather than a smudge.
              const top = ty(Math.min(y1, hist[hist.length - 1] + noise));
              const bot = ty(Math.max(y0, hist[hist.length - 1] - noise));
              return (
                <g>
                  <rect x={X0} y={top} width={SPAN} height={Math.max(1, bot - top)}
                        fill="var(--ink)" opacity="0.13" />
                  {[top, bot].map((yy, i) => (
                    <line key={i} x1={X0} y1={yy} x2={X1} y2={yy}
                          stroke="var(--ink-3)" strokeWidth="0.8" strokeDasharray="2 2"
                          opacity="0.85" />
                  ))}
                </g>
              );
            })()}
            <path d={`M ${pts.map((p) => p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" L ")} L ${X1} ${TR_Y + TR_H} L ${X0} ${TR_Y + TR_H} Z`}
              fill="var(--ink)" opacity="0.05" />
            <path d={`M ${pts.map((p) => p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" L ")}`}
              fill="none" stroke="var(--ink)" strokeWidth="1.6" strokeLinejoin="round" opacity="0.8" />
            <circle cx={pts[pts.length - 1][0]} cy={pts[pts.length - 1][1]} r="3.4"
              fill="var(--ink)" stroke="var(--card)" strokeWidth="1.4" />
            <text x={X1} y={TR_Y + TR_H + 11} textAnchor="end" fontSize="7.5" fill="var(--ink-3)">TODAY</text>
            {noise != null && (
              <text x={X0} y={TR_Y + TR_H + 11} fontSize="7.5" fill="var(--ink-3)">
                SHADED = ±{Math.round(noise)} PT NOISE FLOOR
              </text>
            )}
          </>
        )}
      </svg>

      {noise != null && (
        <p className="mt-1.5 text-[9.5px] leading-snug text-ink-3">
          Half of all day-to-day moves are <b className="font-semibold text-ink-2">
          {Math.round(noise)} points or smaller</b> — that is what a normal night's
          variation in HRV and resting HR is worth, not a change in how recovered you
          are. Read the shaded band as "no news", and the direction over a week rather
          than any single morning.
        </p>
      )}

      {/* Per-input detail, in HTML so the numbers align and wrap properly */}
      <div className="mt-2 space-y-1.5">
        {present.map((c) => (
          <div key={c.key}
            className="flex items-baseline gap-2"
            onPointerEnter={() => setHover(c.key)} onPointerLeave={() => setHover(null)}>
            <span className="h-[18px] w-[3px] flex-none rounded-full" style={{ background: c.color }} />
            <button type="button"
              onClick={() => onInfo?.({ hrv: "hrv", rhr: "rhr", sleep: "sleep_debt" }[c.key])}
              className="flex flex-none items-center gap-1 text-[9.5px] font-semibold tracking-micro"
              style={{ color: hover === c.key ? "var(--ink)" : "var(--ink-2)" }}>
              {c.name}
              {onInfo && <span className="text-[8px] text-ink-3">ⓘ</span>}
            </button>
            <span className="truncate text-[9.5px] text-ink-3">{readings[c.key] ?? ""}</span>
            {c.key === best && (
              <span className="flex-none rounded px-1 py-px text-[8px] tracking-micro"
                style={{ background: "color-mix(in srgb, var(--rhr) 18%, transparent)", color: "var(--rhr)" }}>
                STRONGEST
              </span>
            )}
            {c.key === worstKey && (
              <span className="flex-none rounded px-1 py-px text-[8px] tracking-micro"
                style={{ background: "color-mix(in srgb, var(--over) 18%, transparent)", color: "var(--over)" }}>
                WEAKEST
              </span>
            )}
            <span className="ml-auto flex-none text-[12px] font-semibold text-ink">
              {Math.round(share(c.key) * 100)}%
            </span>
          </div>
        ))}
        {missing.map((c) => (
          <div key={c.key} className="flex items-baseline gap-2 opacity-60">
            <span className="h-[18px] w-[3px] flex-none rounded-full bg-[var(--rule)]" />
            <span className="flex-none text-[9.5px] font-semibold tracking-micro text-ink-3">{c.name}</span>
            <span className="text-[9.5px] text-ink-3">not recorded — weight redistributed</span>
          </div>
        ))}
      </div>

    </div>
  );
}
