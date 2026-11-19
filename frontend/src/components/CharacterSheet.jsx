import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  PolarAngleAxis, PolarGrid, PolarRadiusAxis, Radar, RadarChart, ResponsiveContainer,
} from "recharts";
import { api } from "../api.js";
import Collapse from "./Collapse.jsx";
import StatDetailModal from "./StatDetailModal.jsx";

// RPG character sheet — a READ-ONLY mirror over data computed elsewhere.
// Design + the invariant: docs/GAMIFICATION.md. Model: backend services/game.py.
//
// Every stat is a cumulative XP accumulator (levels never fall), so the two numbers
// on each row are different things:
//   * LEVEL  = what you've banked since the XP epoch
//   * XP/day = how hard you're pushing RIGHT NOW — the actionable signal
// Both are shown, because a high level with a low rate is exactly the "honest
// visible gap" the design surfaces without scolding.

const MAX_LEVEL = 100;

// Rank titles are a pure relabelling of `level` — a monotone function of a number
// already on screen, inventing no data. They exist so progress reads at a glance
// ("Adept" lands faster than "Lv 28") and so distant levels have a name to aim at.
// Bands are every 10 levels, not every 15: the first cut used wide bands and printed
// JOURNEYMAN on five of six stats, because levels cluster tightly (14-28 today) and
// will for a long while. Narrow bands differentiate now and keep differentiating as
// the stats spread.
export const RANKS = [
  [100, "Paragon"], [90, "Ascendant"], [80, "Champion"], [70, "Master"],
  [60, "Elite"], [50, "Expert"], [40, "Veteran"], [30, "Adept"],
  [20, "Journeyman"], [10, "Apprentice"], [0, "Novice"],
];
export const rankFor = (lvl) =>
  (RANKS.find(([min]) => (lvl ?? 0) >= min) || RANKS[RANKS.length - 1])[1];

const LEVELUP_FRESH_DAYS = 3;

function trendArrow(now, prev) {
  if (now == null || prev == null) return null;
  const d = now - prev;
  if (Math.abs(d) < 0.4) return { icon: "→", cls: "text-slate-500" };
  return d > 0 ? { icon: "↑", cls: "text-emerald-400" } : { icon: "↓", cls: "text-amber-400" };
}

function daysAgo(iso) {
  if (!iso) return Infinity;
  return (Date.now() - new Date(iso + "T00:00:00").getTime()) / 86400000;
}

function monthsSince(iso) {
  if (!iso) return null;
  const months = daysAgo(iso) / 30.44;
  if (months < 1) return "<1mo";
  if (months < 24) return `${Math.round(months)}mo`;
  return `${(months / 12).toFixed(1)}y`;
}

function hpColor(v) {
  if (v == null) return "#64748b";
  if (v >= 75) return "#34d399";
  if (v >= 50) return "#818cf8";
  if (v >= 25) return "#fbbf24";
  return "#f87171";
}

// Circular progress-to-next-level around the stat's icon. Reads as a character
// sheet rather than a settings list, and puts the icon and its progress in one
// glance instead of two.
function LevelRing({ pct, color, icon, size = 40 }) {
  const r = (size - 5) / 2;
  const circ = 2 * Math.PI * r;
  return (
    <svg width={size} height={size} className="flex-shrink-0" aria-hidden="true">
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="#1e293b" strokeWidth="3" />
      <circle
        cx={size / 2} cy={size / 2} r={r} fill="none"
        stroke={color} strokeWidth="3" strokeLinecap="round"
        strokeDasharray={circ}
        strokeDashoffset={circ * (1 - Math.max(0, Math.min(100, pct ?? 0)) / 100)}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
        style={{ transition: "stroke-dashoffset 600ms ease" }}
      />
      <text x="50%" y="50%" textAnchor="middle" dominantBaseline="central" fontSize="15">
        {icon}
      </text>
    </svg>
  );
}

// Momentum arrow: current 14-day pace against the stat's own 90-day norm.
//
// Points RIGHT when gaining, LEFT when falling back — along the axis the level ring
// fills, so it reads as "advancing" / "losing ground" rather than as a value going
// up or down. Size carries magnitude (small ≈ 1.5-4 xp/day off norm, large ≈ >4).
// Flat renders nothing: an arrow on every row would make the glance meaningless.
//
// This is the one thing the level cannot tell you. Cumulative XP is monotone by
// design, so backsliding shows up only as an absence of progress.
function MomentumArrow({ band, dir, momentum }) {
  if (!band || band === "flat" || !dir) return null;
  const up = dir > 0;
  const color = up ? "#34d399" : "#fb923c";
  const large = band === "large";
  const label = `${up ? "Gaining" : "Losing"} ground · ${momentum > 0 ? "+" : ""}` +
    `${(momentum * 10).toFixed(1)} xp/day vs your 90-day norm`;
  return (
    <span
      title={label}
      aria-label={label}
      className="flex-shrink-0 leading-none"
      style={{
        color,
        fontSize: large ? 17 : 12,
        opacity: large ? 1 : 0.75,
        transform: up ? "none" : "scaleX(-1)",
      }}
    >
      ➤
    </span>
  );
}

function StatRow({ s, epochStart, onOpen, freshLevel }) {
  const trend = trendArrow(s.xp_rate_7d, s.xp_rate_prev_7d);
  // A stat that started LATER THAN THE REST has had less time to bank XP; say so,
  // or its lower level reads as a character flaw rather than a short history.
  // Compared against the earliest stat rather than against "now": all stats share
  // the XP epoch, so an absolute age test would tag every row and mean nothing.
  const young = s.tracking_since && epochStart &&
    (new Date(s.tracking_since + "T00:00:00") - new Date(epochStart + "T00:00:00"))
      > 1000 * 60 * 60 * 24 * 30;

  return (
    <button
      type="button"
      onClick={() => onOpen(s)}
      className="group flex w-full items-center gap-3 rounded-xl px-2 py-2 text-left transition-colors hover:bg-slate-700/30 active:bg-slate-700/50"
    >
      <LevelRing pct={s.pct_to_next} color={s.color} icon={s.icon} />

      <div className="flex w-3 flex-shrink-0 justify-center">
        <MomentumArrow band={s.momentum_band} dir={s.momentum_dir} momentum={s.momentum} />
      </div>

      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-2">
          <span className="truncate text-sm font-medium text-slate-100">{s.label}</span>
          <span className="flex-shrink-0 text-[10px] uppercase tracking-wider text-slate-500">
            {rankFor(s.level)}
          </span>
        </div>
        <div className="mt-0.5 flex items-baseline justify-between gap-2">
          <span className="text-[11px] tabular-nums text-slate-400">
            {s.xp_rate_7d?.toFixed(1)} xp/day
            {trend && <span className={`ml-1 ${trend.cls}`}>{trend.icon}</span>}
            {young && (
              <span className="ml-1.5 text-[9px] text-slate-600">
                new · {monthsSince(s.tracking_since)}
              </span>
            )}
          </span>
          <span className="flex-shrink-0 text-[10px] tabular-nums text-slate-600">
            {s.xp_to_next != null ? `${Math.round(s.xp_to_next)} xp to next` : "max"}
          </span>
        </div>
      </div>

      <div className="flex flex-shrink-0 items-center gap-1">
        <span
          className={`text-xl font-bold tabular-nums leading-none ${freshLevel ? "animate-breathe" : ""}`}
          style={{ color: s.color }}
        >
          {s.level}
        </span>
        <span className="text-[10px] text-slate-600 transition-transform group-hover:translate-x-0.5">›</span>
      </div>
    </button>
  );
}

// Coloured, bold spoke labels. A single Radar series can't be multi-coloured, but
// the angle-axis ticks can — and that is what actually makes the shape readable,
// since otherwise every spoke is anonymous grey text.
function AngleTick({ payload, x, y, textAnchor, colorFor }) {
  return (
    <text
      x={x} y={y} textAnchor={textAnchor} dominantBaseline="central"
      fill={colorFor[payload.value] || "#64748b"} fontSize={10} fontWeight={700}
      letterSpacing={0.5}
    >
      {payload.value}
    </text>
  );
}

export default function CharacterSheet({ onOpenChange }) {
  const [selected, setSelected] = useState(null);
  const { data, isLoading } = useQuery({
    queryKey: ["game"],
    queryFn: api.game,
    staleTime: 1000 * 60 * 15,
  });

  if (isLoading || !data?.stats?.length) return null;

  const stats = data.stats;
  const cond = data.condition || {};
  const ailments = cond.ailments || [];
  const levelUps = data.level_ups || [];
  const fresh = levelUps.filter((l) => daysAgo(l.date) <= LEVELUP_FRESH_DAYS);
  const freshByStat = new Set(fresh.map((l) => l.stat));

  const radar = stats.map((s) => ({ short: s.label.slice(0, 3), level: s.level }));
  const colorFor = Object.fromEntries(stats.map((s) => [s.label.slice(0, 3), s.color]));

  const epochStart = stats.reduce(
    (min, s) => (s.tracking_since && (!min || s.tracking_since < min) ? s.tracking_since : min),
    null,
  );

  // Radar scale. A fixed 0-100 domain renders early levels as a dot in the middle
  // and hides the relative shape, which is the only thing the radar is for. Quantise
  // to 25s instead: the axis is stable for long stretches (so the shape stays
  // comparable across weeks) and only rescales at 25/50/75/100.
  const maxLevel = Math.max(...stats.map((s) => s.level ?? 0), 1);
  const axisMax = Math.min(MAX_LEVEL, Math.max(25, Math.ceil((maxLevel + 5) / 25) * 25));

  // Headline: lowest XP RATE, not lowest level — the rate is what you can act on
  // today, and the level is mostly a function of how long the stat has existed.
  const lagging = [...stats].sort((a, b) => (a.xp_rate_7d ?? 0) - (b.xp_rate_7d ?? 0))[0];
  const hp = cond.hp?.value;

  return (
    <Collapse
      onOpenChange={onOpenChange}
      title="Character sheet"
      hint={fresh.length ? `· ${fresh[0].label} reached Lv ${fresh[0].level}` : undefined}
    >
      <div className="space-y-4 px-4 pb-4">

        {/* Level-up banner — only for the last few days, so it stays an event */}
        {fresh.length > 0 && (
          <div className="relative overflow-hidden rounded-xl border border-indigo-400/25 bg-gradient-to-r from-indigo-500/15 via-indigo-500/5 to-transparent px-3 py-2.5">
            <div className="pointer-events-none absolute inset-y-0 -left-1/3 w-1/3 animate-sheen bg-gradient-to-r from-transparent via-white/10 to-transparent" />
            <p className="relative text-[10px] font-semibold uppercase tracking-widest text-indigo-300">
              Level up
            </p>
            {fresh.map((l) => (
              <div key={l.stat} className="relative mt-0.5">
                <p className="text-xs text-slate-200">
                  {l.label} reached Lv {l.level}
                </p>
                {l.drivers?.length > 0 && (
                  <>
                    {/* "What earned it", never "what you did": for Discipline /
                        Strength / Attunement these are behaviours and naming them
                        reinforces what worked, but for Vitality they are outcomes
                        (HRV rose). Attributing that rise to a behaviour would be a
                        causal claim — the correlation engine's job, not this card's. */}
                    <p className="mt-1 text-[10px] text-slate-400">
                      What earned it:{" "}
                      {l.drivers.map((d, i) => (
                        <span key={d.key}>
                          {i > 0 && " · "}
                          <span className="text-slate-300">{d.label}</span>{" "}
                          {Math.round(d.share * 100)}%
                        </span>
                      ))}
                    </p>
                  </>
                )}
              </div>
            ))}
          </div>
        )}

        {/* Condition — today's state, never a permanent stat change */}
        <div>
          <div className="mb-1.5 flex items-baseline justify-between">
            <span className="text-[10px] font-medium uppercase tracking-widest text-slate-500">
              Condition
            </span>
            <span className="text-[10px] tabular-nums text-slate-400">
              {hp != null ? `${hp}/100 · ${cond.hp.state}` : "—"}
            </span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-slate-800">
            <div
              className="h-full rounded-full transition-all duration-700"
              style={{ width: `${hp ?? 0}%`, backgroundColor: hpColor(hp) }}
            />
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[10px]">
            {cond.mana?.value != null && (
              <span className="rounded-full border border-sky-400/20 bg-sky-500/10 px-2 py-0.5 text-sky-300">
                Energy {cond.mana.value}/5
              </span>
            )}
            {cond.caffeine?.mg > 0 && (
              <span className="rounded-full border border-amber-400/20 bg-amber-500/10 px-2 py-0.5 text-amber-300">
                ☕ {Math.round(cond.caffeine.mg)} mg
              </span>
            )}
            {ailments.map((a) => (
              <span key={a.key}
                    className="rounded-full border border-rose-400/25 bg-rose-500/10 px-2 py-0.5 text-rose-200">
                {a.icon} {a.label}
              </span>
            ))}
            {!ailments.length && (
              <span className="rounded-full border border-emerald-400/20 bg-emerald-500/10 px-2 py-0.5 text-emerald-300">
                ✓ No ailments
              </span>
            )}
          </div>
        </div>

        {/* Radar — 6 spokes. Shared inputs mean the spokes are not fully
            independent (see the design doc); read the shape, not the area. */}
        <div className="relative -mx-2">
          <span className="absolute right-2 top-0 z-10 text-[9px] tabular-nums text-slate-600">
            outer ring = Lv {axisMax}
          </span>
          <ResponsiveContainer width="100%" height={200}>
            <RadarChart data={radar} outerRadius="70%">
              <defs>
                <radialGradient id="csRadarFill" cx="50%" cy="50%" r="50%">
                  <stop offset="0%" stopColor="#a5b4fc" stopOpacity={0.5} />
                  <stop offset="100%" stopColor="#6366f1" stopOpacity={0.12} />
                </radialGradient>
                <filter id="csRadarGlow" x="-50%" y="-50%" width="200%" height="200%">
                  <feGaussianBlur stdDeviation="2.2" result="blur" />
                  <feMerge>
                    <feMergeNode in="blur" />
                    <feMergeNode in="SourceGraphic" />
                  </feMerge>
                </filter>
              </defs>
              <PolarGrid stroke="#1e293b" />
              <PolarAngleAxis
                dataKey="short"
                tick={(props) => <AngleTick {...props} colorFor={colorFor} />}
              />
              <PolarRadiusAxis domain={[0, axisMax]} tick={false} axisLine={false} />
              {/* isAnimationActive={false}: the chart mounts inside a collapsible
                  that was just opened, so ResponsiveContainer reports zero size on
                  the first frame. Recharts starts the grow-from-centre animation
                  against that degenerate geometry and never recovers — every
                  polygon point stays pinned at the centre. Verified in-browser. */}
              <Radar
                dataKey="level"
                stroke="#c7d2fe" strokeWidth={2}
                fill="url(#csRadarFill)"
                filter="url(#csRadarGlow)"
                isAnimationActive={false}
              />
            </RadarChart>
          </ResponsiveContainer>
        </div>

        <div className="space-y-0.5">
          {stats.map((s) => (
            <StatRow key={s.stat} s={s} epochStart={epochStart} onOpen={setSelected}
                     freshLevel={freshByStat.has(s.stat)} />
          ))}
        </div>

        {lagging && (
          <div className="rounded-xl border border-slate-700/60 bg-slate-900/40 px-3 py-2.5">
            <p className="text-[10px] font-medium uppercase tracking-widest text-slate-500">
              Biggest opportunity
            </p>
            <p className="mt-1 text-[11px] leading-relaxed text-slate-300">
              <span style={{ color: lagging.color }}>{lagging.label}</span> is earning
              slowest at {lagging.xp_rate_7d?.toFixed(1)} xp/day
              {lagging.top_lever && (
                <> — most available from{" "}
                  <span className="text-slate-200">{lagging.top_lever.label}</span>
                  {" "}(+{lagging.top_lever.xp_available.toFixed(1)} xp/day)</>
              )}.
            </p>
            <p className="mt-1 text-[10px] text-slate-600">
              Tap any stat to see how it's computed.
            </p>
          </div>
        )}
      </div>

      {selected && (
        <StatDetailModal
          stat={stats.find((s) => s.stat === selected.stat) || selected}
          onClose={() => setSelected(null)}
        />
      )}
    </Collapse>
  );
}
