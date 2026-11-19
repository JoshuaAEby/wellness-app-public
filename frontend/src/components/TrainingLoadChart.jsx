import { useMemo, useState } from "react";
import { ACWR_HIGH, ACWR_LOW } from "../lib/acwr.js";

/**
 * Training load in LOAD UNITS: CTL, ATL, and the ACWR target expressed as a band
 * around CTL — plus a forward envelope.
 *
 * WHY NOT RATIO SPACE
 * -------------------
 * An earlier version plotted ACWR alone. That is defensible on its own terms (the
 * corridor becomes two fixed lines) but it throws away the thing you actually want
 * to watch: ACWR is ATL/CTL, so plotting the ratio DIVIDES OUT CTL — the fitness
 * signal. A season of real base-building and a season of doing nothing can trace
 * the same ratio path. Keeping load units means CTL's climb is visible as a climb,
 * and the ratio is still readable as ATL's position inside the band.
 *
 * THE BAND
 * --------
 * [0.8·CTL, 2.0·CTL] (the app-wide normal range, lib/acwr.js; 1.3 before 2026-11-04). It moves with CTL by construction — that is not a flaw here,
 * it is what makes "am I inside the corridor" a containment question you can answer
 * by eye: ATL inside the band ⇔ ACWR in [0.8, 2.0].
 *
 * THE FORECAST
 * ------------
 * Two scenarios that BOUND behaviour rather than guess at it — train on none of the
 * next N days, or train on all of them at your typical session load. Every real week
 * lands inside. The band is drawn only over measured history: in the forecast there
 * are two CTL paths, so a single band there would be a fiction.
 */

const W = 340;

export default function TrainingLoadChart({ history, forecast }) {
  const [hover, setHover] = useState(null);

  const past = history.length;
  const fut = forecast?.available ? forecast.horizon : 0;

  const geom = useMemo(() => {
    const total = Math.max(1, past + fut - 1);
    const X0 = 32, X1 = W - 10, PY0 = 16, PY1 = 168;

    const vals = [];
    for (const h of history) {
      if (h.atl != null) vals.push(h.atl);
      if (h.ctl != null) vals.push(h.ctl * ACWR_HIGH);
    }
    if (forecast?.available) {
      for (const k of Object.keys(forecast.scenarios)) {
        for (const p of forecast.scenarios[k].points) {
          if (p.atl != null) vals.push(p.atl);
          if (p.ctl != null) vals.push(p.ctl * ACWR_HIGH);
        }
      }
    }
    const hi = Math.max(10, ...vals) * 1.08;
    const px = (i) => X0 + (i / total) * (X1 - X0);
    const py = (v) => PY1 - (Math.max(0, Math.min(hi, v)) / hi) * (PY1 - PY0);
    return { total, X0, X1, PY0, PY1, px, py, hi };
  }, [history, forecast, past, fut]);

  if (!history.length) return null;
  const { X0, X1, PY0, PY1, px, py, hi } = geom;
  const todayX = px(past - 1);
  const RY0 = 194, RH = 34;
  const cur = history[past - 1];

  const pts = (sel) => history.map((h, i) => (sel(h) == null ? null : [px(i), py(sel(h))]))
    .filter(Boolean);
  const line = (p) => "M " + p.map((q) => `${q[0].toFixed(1)} ${q[1].toFixed(1)}`).join(" L ");

  const atlPts = pts((h) => h.atl);
  const ctlPts = pts((h) => h.ctl);

  // Normal-range band as a filled ribbon between ACWR_LOW·CTL and ACWR_HIGH·CTL.
  const bandTop = history.map((h, i) => (h.ctl == null ? null : [px(i), py(h.ctl * ACWR_HIGH)])).filter(Boolean);
  const bandBot = history.map((h, i) => (h.ctl == null ? null : [px(i), py(h.ctl * ACWR_LOW)])).filter(Boolean);
  const bandPath = bandTop.length && bandBot.length
    ? line(bandTop) + " L " + bandBot.slice().reverse().map((q) => `${q[0].toFixed(1)} ${q[1].toFixed(1)}`).join(" L ") + " Z"
    : null;

  const sc = forecast?.available ? forecast.scenarios : null;
  const P = (t) => px(past - 1 + t);
  const fpath = (name, key) =>
    sc?.[name]?.points?.map((p) => [P(p.day), py(p[key])]) ?? [];
  const atlHi = fpath("train", "atl");
  const atlLo = fpath("rest", "atl");
  const ctlHi = fpath("train", "ctl");
  const ctlLo = fpath("rest", "ctl");

  const loadMax = Math.max(...history.map((h) => h.load || 0), 1);
  const inBand = cur.ctl != null && cur.atl != null
    && cur.atl >= ACWR_LOW * cur.ctl && cur.atl <= ACWR_HIGH * cur.ctl;

  return (
    <div className="relative">
      <svg viewBox={`0 0 ${W} 244`} className="block w-full overflow-visible" role="img"
        aria-label="Chronic and acute training load in TRIMP, with the acute-to-chronic target band around CTL and a forward envelope">
        <defs>
          <pattern id="tlFx" width="5" height="5" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
            <line x1="0" y1="0" x2="0" y2="5" stroke="var(--rule)" strokeWidth="1" />
          </pattern>
        </defs>

        {fut > 0 && (
          <rect x={todayX} y={PY0} width={X1 - todayX} height={PY1 - PY0} fill="url(#tlFx)" opacity="0.45" />
        )}

        {/* y grid */}
        {[0.25, 0.5, 0.75, 1].map((f) => (
          <g key={f}>
            <line x1={X0} y1={py(hi * f)} x2={X1} y2={py(hi * f)} stroke="var(--rule)" opacity="0.55" />
            <text x={X0 - 4} y={py(hi * f) + 3} textAnchor="end" fontSize="8" fill="var(--ink-3)">
              {Math.round(hi * f)}
            </text>
          </g>
        ))}

        {/* the ACWR target, as a band around CTL */}
        {bandPath && <path d={bandPath} fill="var(--rhr)" opacity="0.14" />}

        {/* CTL — fitness. The line that shows progress, so it reads as the base. */}
        {ctlPts.length > 1 && (
          <path d={line(ctlPts)} fill="none" stroke="var(--ink-2)" strokeWidth="2" strokeLinejoin="round" />
        )}
        {/* ATL — fatigue. ONE colour, deliberately.
            This used to recolour per segment: red above the band, blue-grey below,
            blue inside. Two problems. First it was redundant — the band is drawn
            right there, so "outside the corridor" is already visible as geometry,
            and colour was restating what position already said. Second, --under
            (blue-grey) and --hrv (blue) are close enough at 1.8px that "below" and
            "inside" were indistinguishable, which made the whole scheme read as
            decoration rather than encoding. Position carries the state; the line
            keeps its identity. Only the current-value dot takes a state colour. */}
        {atlPts.length > 1 && (
          <path d={line(atlPts)} fill="none" stroke="var(--hrv)" strokeWidth="1.8"
            strokeLinejoin="round" strokeLinecap="round" />
        )}

        {/* forward envelope: ATL cone (shaded) + CTL cone (thin dashes) */}
        {sc && atlHi.length > 0 && atlLo.length > 0 && (
          <>
            <path d={`M ${todayX} ${py(cur.atl)} L ${atlHi.map((p) => p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" L ")} L ${
              atlLo.slice().reverse().map((p) => p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" L ")} Z`}
              fill="var(--hrv)" opacity="0.13" />
            <path d={`M ${todayX} ${py(cur.atl)} L ${atlHi.map((p) => p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" L ")}`}
              fill="none" stroke="var(--hrv)" strokeWidth="1.3" strokeDasharray="4 3" />
            <path d={`M ${todayX} ${py(cur.atl)} L ${atlLo.map((p) => p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" L ")}`}
              fill="none" stroke="var(--hrv)" strokeWidth="1.3" strokeDasharray="4 3" />
            <path d={`M ${todayX} ${py(cur.ctl)} L ${ctlHi.map((p) => p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" L ")}`}
              fill="none" stroke="var(--ink-2)" strokeWidth="1.2" strokeDasharray="3 3" opacity="0.8" />
            <path d={`M ${todayX} ${py(cur.ctl)} L ${ctlLo.map((p) => p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" L ")}`}
              fill="none" stroke="var(--ink-2)" strokeWidth="1.2" strokeDasharray="3 3" opacity="0.8" />
          </>
        )}

        {/* tomorrow: where one typical session vs a rest day puts fatigue (note 49) */}
        {sc && atlHi.length > 0 && atlLo.length > 0 && (
          <>
            <circle cx={atlHi[0][0]} cy={atlHi[0][1]} r="2.4" fill="var(--hrv)" stroke="var(--card)" strokeWidth="1" />
            <circle cx={atlLo[0][0]} cy={atlLo[0][1]} r="2.4" fill="var(--card)" stroke="var(--hrv)" strokeWidth="1.2" />
          </>
        )}

        {/* today */}
        <line x1={todayX} y1={PY0 - 5} x2={todayX} y2={RY0 + RH} stroke="var(--ink-3)" strokeWidth="1" opacity="0.7" />
        {cur.atl != null && (
          <circle cx={todayX} cy={py(cur.atl)} r="3.4" fill={inBand ? "var(--hrv)" : "var(--over)"}
            stroke="var(--card)" strokeWidth="1.5" />
        )}
        {cur.ctl != null && (
          <circle cx={todayX} cy={py(cur.ctl)} r="3" fill="var(--ink-2)" stroke="var(--card)" strokeWidth="1.4" />
        )}
        <text x={todayX - 4} y={PY0 - 8} textAnchor="end" fontSize="7.5" letterSpacing=".14em" fill="var(--ink-3)">MEASURED</text>
        {fut > 0 && <text x={todayX + 4} y={PY0 - 8} fontSize="7.5" letterSpacing=".14em" fill="var(--ink-3)">IF…</text>}

        {/* daily load rail */}
        <text x={X0} y={RY0 - 6} fontSize="8" letterSpacing=".16em" fill="var(--ink-3)" fontWeight="600">DAILY LOAD · TRIMP</text>
        {history.map((h, i) => {
          const hh = ((h.load || 0) / loadMax) * RH;
          const w = Math.max(((X1 - X0) / geom.total) * 0.72, 1);
          return <rect key={i} x={px(i) - w / 2} y={RY0 + RH - hh} width={w}
            height={Math.max(hh, 0.5)} fill="var(--ink-2)" opacity="0.4" />;
        })}
        <line x1={X0} y1={RY0 + RH} x2={X1} y2={RY0 + RH} stroke="var(--rule)" />
        <text x={X0} y={RY0 + RH + 12} fontSize="7.5" fill="var(--ink-3)">{history[0]?.label}</text>
        {/* End-anchored left of the today line, like MEASURED above it: with a 7-day
            horizon the forecast strip is ~20px wide, and a centred label ran into "+7d". */}
        <text x={todayX - 3} y={RY0 + RH + 12} textAnchor="end" fontSize="7.5" fill="var(--ink-3)">TODAY</text>
        {fut > 0 && <text x={X1} y={RY0 + RH + 12} textAnchor="end" fontSize="7.5" fill="var(--ink-3)">+{fut}d</text>}

        {history.map((h, i) => (
          <rect key={`h${i}`} x={px(i) - 3} y={PY0} width={6} height={PY1 - PY0} fill="transparent"
            onPointerEnter={() => setHover({ i, h })} onPointerLeave={() => setHover(null)} />
        ))}
        {hover && (
          <line x1={px(hover.i)} y1={PY0} x2={px(hover.i)} y2={PY1} stroke="var(--ink-3)" strokeWidth="1" opacity="0.6" />
        )}
      </svg>

      {hover && hover.h.atl != null && (
        <div className="pointer-events-none absolute top-0 rounded-md border border-rule bg-canvas px-2 py-1.5 text-[10px] shadow-lg"
          style={{ left: `${Math.min(72, Math.max(0, (px(hover.i) / W) * 100 - 14))}%` }}>
          <div className="tracking-micro text-ink-3">{hover.h.label}</div>
          <div className="text-ink">ATL <b>{hover.h.atl?.toFixed(1)}</b> · CTL <b>{hover.h.ctl?.toFixed(1)}</b></div>
          <div className="text-ink-2">
            ACWR {hover.h.acwr?.toFixed(2) ?? "—"} · load {Math.round(hover.h.load || 0)}
          </div>
        </div>
      )}
    </div>
  );
}
