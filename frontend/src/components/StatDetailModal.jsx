import { useQuery } from "@tanstack/react-query";
import {
  CartesianGrid, Line, LineChart, ResponsiveContainer, XAxis, YAxis,
} from "recharts";
import { api } from "../api.js";
import { rankFor } from "./CharacterSheet.jsx";

// Drill-down for one RPG stat: how it is computed, and where effort buys the most.
// Bottom-sheet pattern matched to MetricInfoModal.jsx.
//
// Everything here is tap-to-open rather than hover: `title=` tooltips do nothing on
// the phone, which is where this app is actually used.

const HISTORY_DAYS = 180;

// Sub-scores are 0..1 where 0.5 means "exactly at your own rolling norm" for the
// ratio-based components (tonnage, CTL). Colour the bar by that reference point so
// "at norm" reads as neutral rather than as a half-empty failure.
function barColor(score) {
  if (score == null) return "#475569";
  if (score >= 0.66) return "#34d399";
  if (score >= 0.45) return "#818cf8";
  if (score >= 0.25) return "#fbbf24";
  return "#f87171";
}

function ComponentRow({ c, isTopLever }) {
  const pct = c.score == null ? null : Math.round(c.score * 100);
  return (
    <div className={`rounded-lg p-2.5 ${isTopLever ? "bg-indigo-500/10 ring-1 ring-indigo-500/30" : "bg-slate-700/40"}`}>
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-xs font-medium text-slate-200">
          {c.label}
          {isTopLever && (
            <span className="ml-1.5 rounded bg-indigo-500/25 px-1.5 py-0.5 text-[9px] font-normal text-indigo-200">
              best lever
            </span>
          )}
        </span>
        <span className="flex-shrink-0 text-[10px] text-slate-500">
          weight {(c.weight * 100).toFixed(0)}%
        </span>
      </div>
      <div className="mt-1.5 flex items-center gap-2">
        <div className="relative h-1.5 flex-1 overflow-hidden rounded-full bg-slate-800">
          {pct != null && (
            <div className="absolute inset-y-0 left-0 rounded-full"
                 style={{ width: `${pct}%`, backgroundColor: barColor(c.score) }} />
          )}
        </div>
        <span className="w-9 flex-shrink-0 text-right text-[10px] font-semibold text-slate-300">
          {pct == null ? "—" : `${pct}%`}
        </span>
      </div>
      <p className="mt-1 text-[10px] leading-relaxed text-slate-500">{c.detail}</p>
      {c.xp_available > 0 && (
        <p className="mt-0.5 text-[10px] text-slate-400">
          up to <span className="text-emerald-400">+{c.xp_available.toFixed(1)} xp/day</span> still available here
        </p>
      )}
    </div>
  );
}

export default function StatDetailModal({ stat, onClose }) {
  const { data: history = [] } = useQuery({
    queryKey: ["game-history", stat?.stat, HISTORY_DAYS],
    queryFn: () => api.gameHistory(stat.stat, HISTORY_DAYS),
    enabled: !!stat,
    staleTime: 1000 * 60 * 15,
  });

  if (!stat) return null;

  // Levers are already ranked by marginal XP on the server; fall back to raw
  // components if a stat has none scored today.
  const ranked = stat.levers?.length ? stat.levers : stat.components || [];
  const topKey = stat.top_lever?.key;
  const curve = history.filter((h) => h.level != null && h.cum_xp > 0);

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/60 pb-16" onClick={onClose}>
      <div
        className="max-h-[85vh] w-full max-w-md overflow-y-auto rounded-t-2xl bg-slate-800 p-6 pb-10"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="mb-1 flex items-start justify-between">
          <h2 className="flex items-center gap-2 text-lg font-semibold text-white">
            <span>{stat.icon}</span>{stat.label}
          </h2>
          <button onClick={onClose} className="text-xl leading-none text-slate-400 hover:text-white">✕</button>
        </div>

        <div className="mb-4 flex items-baseline gap-3">
          <span className="text-3xl font-bold tabular-nums" style={{ color: stat.color }}>
            {stat.level}
          </span>
          <div className="min-w-0">
            <p className="text-xs font-semibold uppercase tracking-widest"
               style={{ color: stat.color }}>{rankFor(stat.level)}</p>
            <p className="text-[11px] tabular-nums text-slate-400">
              {stat.xp?.toLocaleString()} xp
              {stat.xp_to_next != null && ` · ${Math.round(stat.xp_to_next)} to Lv ${stat.level + 1}`}
            </p>
          </div>
        </div>

        <div className="mb-4">
          <div className="relative h-2 overflow-hidden rounded-full bg-slate-700">
            <div className="absolute inset-y-0 left-0 rounded-full"
                 style={{ width: `${stat.pct_to_next ?? 0}%`, backgroundColor: stat.color }} />
          </div>
          <div className="mt-1.5 flex justify-between text-[10px] text-slate-500">
            <span>{stat.xp_rate_7d?.toFixed(1)} xp/day over the last week</span>
            {stat.tracking_since && <span>tracking since {stat.tracking_since}</span>}
          </div>
          {/* The arrow on the row shows direction and rough size; the numbers behind
              it belong here rather than cluttering the glance. */}
          {stat.momentum != null && stat.xp_rate_90d != null && (
            <p className="mt-1 text-[10px] text-slate-500">
              Momentum:{" "}
              <span className={stat.momentum_dir > 0 ? "text-emerald-400"
                : stat.momentum_dir < 0 ? "text-orange-400" : "text-slate-400"}>
                {stat.xp_rate_14d?.toFixed(1)} xp/day over 14d vs {stat.xp_rate_90d.toFixed(1)} over 90d
              </span>
              {stat.momentum_band === "flat" && " — holding steady"}
            </p>
          )}
        </div>

        {/* What moved — the exact decomposition of the momentum arrow.
            Framed on score_delta (which way the BEHAVIOUR went), not on the xp delta:
            share-based attribution can give a component a negative delta while its
            score rose, because a sibling rose more. Calling that a problem would be
            false, so colour and wording follow the score. */}
        {stat.momentum_drivers?.length > 0 && (
          <>
            <p className="mb-2 text-xs font-medium uppercase tracking-widest text-slate-400">
              What moved {stat.momentum_dir < 0 ? "against you" : "for you"}
            </p>
            <div className="mb-4 space-y-1.5">
              {stat.momentum_drivers.map((d) => {
                const sd = d.score_delta;
                const better = sd != null && sd > 0.02;
                const worse = sd != null && sd < -0.02;
                return (
                  <div key={d.key} className="flex items-baseline justify-between gap-3 text-[11px]">
                    <span className="min-w-0 flex-1 truncate text-slate-300">{d.label}</span>
                    {sd != null && (
                      <span className={`flex-shrink-0 tabular-nums ${
                        better ? "text-emerald-400" : worse ? "text-orange-400" : "text-slate-500"}`}>
                        {(d.score_baseline * 100).toFixed(0)}% → {(d.score_recent * 100).toFixed(0)}%
                      </span>
                    )}
                    <span className="w-14 flex-shrink-0 text-right tabular-nums text-slate-500">
                      {d.delta > 0 ? "+" : ""}{(d.delta * 10).toFixed(1)} xp
                    </span>
                  </div>
                );
              })}
              {Math.abs(stat.momentum_other ?? 0) >= 0.005 && (
                <div className="flex items-baseline justify-between gap-3 text-[11px] text-slate-600">
                  <span className="flex-1">everything else</span>
                  <span className="w-14 flex-shrink-0 text-right tabular-nums">
                    {stat.momentum_other > 0 ? "+" : ""}{(stat.momentum_other * 10).toFixed(1)} xp
                  </span>
                </div>
              )}
            </div>
            <p className="mb-4 text-[10px] leading-relaxed text-slate-600">
              Percentages are each component vs your own norm, 14 days against 90.
              The xp column splits the momentum and sums to it — a component can lose
              share while improving, if another improved more.
            </p>
          </>
        )}

        {/* How it's computed */}
        <p className="mb-2 text-xs font-medium uppercase tracking-widest text-slate-400">
          How it's computed
        </p>
        <p className="mb-3 text-[11px] leading-relaxed text-slate-500">{stat.basis}.
          {" "}Each component is scored against your own rolling history, then blended by
          the weights below. <span className="text-slate-400">50% means exactly at your
          own norm</span> — so holding steady keeps earning, and beating your norm earns more.
        </p>
        <div className="space-y-2">
          {ranked.map((c) => (
            <ComponentRow key={c.key} c={c} isTopLever={c.key === topKey} />
          ))}
        </div>

        {/* Level history */}
        {curve.length > 5 && (
          <>
            <p className="mb-2 mt-5 text-xs font-medium uppercase tracking-widest text-slate-400">
              Level history
            </p>
            <ResponsiveContainer width="100%" height={120}>
              {/* left margin stays >= 0: the app's usual -20 pulls the Y axis out of
                  the plot area, and level labels here are 1-2 digits, so they clip. */}
              <LineChart data={curve} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                <XAxis dataKey="date" tick={{ fill: "#64748b", fontSize: 9 }}
                       tickFormatter={(d) => d.slice(5)} minTickGap={40} />
                <YAxis tick={{ fill: "#64748b", fontSize: 9 }} width={28}
                       allowDecimals={false} />
                {/* isAnimationActive={false} — same trap as the radar: this chart
                    mounts inside a conditionally-rendered modal, so the container
                    reports zero size on the first frame and the animation never
                    recovers. */}
                <Line dataKey="level" stroke={stat.color} strokeWidth={2} dot={false}
                      isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </>
        )}

        <p className="mt-4 text-[10px] leading-relaxed text-slate-600">
          Levels never fall. XP accrues daily and is never taken away — a bad week only
          slows the rate. Travel and sick days are held, not penalised.
        </p>
      </div>
    </div>
  );
}
