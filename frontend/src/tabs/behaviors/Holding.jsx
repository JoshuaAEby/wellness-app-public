import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api.js";
import { Card, Eyebrow, HeroSub, K, Sep, Foot } from "../../components/ui.jsx";
import { BEHAVIORS, TYPES, bestEvidence } from "../../lib/behaviors.js";

/**
 * The scoreboard, with each behaviour measured the way its STRUCTURE allows.
 *
 * Replaces the old 2x2 streak bins + adherence card + monthly summary. The bins
 * showed a consecutive-day streak for every behaviour, and every single current
 * streak read 0 — not a bug: at 48-72% adherence the expected run length is
 * ~1-2 days, so the headline card was mathematically doomed to read zero, and it
 * blanked every morning before the day was logged.
 *
 * Streaks survive here for AVOIDANCE behaviours only, where a long run is both
 * achievable and meaningful. Everything else gets a rate, a count against a
 * target, or nothing.
 *
 * Evidence sits INLINE on each row rather than in a separate "does it matter"
 * card, matching the rule the rest of the app follows: the claim and its
 * evidence appear together, or the claim is not shown.
 */

const SETTLED_HI = 95;   // >= this over a solid denominator == a ritual, not a decision
const SETTLED_LO = 5;

function Rho({ v }) {
  const pos = v >= 0;
  return (
    <span style={{ color: pos ? "var(--rhr)" : "var(--over)" }}>
      {pos ? "+" : ""}{v.toFixed(2)}
    </span>
  );
}

/** One line of "what this actually moves", or an honest statement of why we can't say. */
function Evidence({ ev }) {
  if (!ev) return null;
  if (ev.reason === "no_variance") {
    return (
      <p className="mt-0.5 text-[9.5px] leading-snug text-ink-3">
        No variance to analyse — nothing to learn until it changes.
      </p>
    );
  }
  if (ev.reason === "sparse") {
    return (
      <p className="mt-0.5 text-[9.5px] leading-snug text-ink-3">
        Not enough paired days yet{ev.maxN ? ` (best ${ev.maxN})` : ""} — keep logging.
      </p>
    );
  }
  return (
    <p className="mt-0.5 text-[9.5px] leading-snug text-ink-3">
      {ev.subject} → {ev.outcome} <Rho v={ev.rho} /> · n={ev.n}
      {ev.tier === "suggestive" && <span className="text-ink-3"> · suggestive</span>}
    </p>
  );
}

function Row({ emoji, label, children, ev, flag }) {
  return (
    <div className="border-t border-rule py-2 first:border-t-0">
      <div className="flex items-baseline gap-2">
        <span className="w-4 flex-none text-center text-[11px]">{emoji}</span>
        <span className="min-w-0 flex-1 truncate text-[11.5px] text-ink-2">{label}</span>
        {flag === "rare" && (
          <span className="flex-none text-[9px]" style={{ color: "var(--breakfast)" }}
                title="Almost never done — redesign the target or drop it">⚠︎</span>
        )}
        <span className="flex-none text-right">{children}</span>
      </div>
      <div className="pl-6"><Evidence ev={ev} /></div>
    </div>
  );
}

/** Avoidance: current run, with the personal best as the reference point. */
function StreakStat({ current, best }) {
  return (
    <span className="text-[11px] text-ink-3">
      <b className="text-[15px] font-semibold text-ink">{current ?? 0}</b> d
      {best ? <><Sep />best {best}</> : null}
    </span>
  );
}

/** Binary: rolling rate. The bar is capped at 0.6 opacity so ink stays legible. */
function RateStat({ pct, num, den, color, basePct }) {
  // A tick on the bar marks the 90-day norm, so "am I above or below my usual" is
  // readable without doing arithmetic.
  const delta = basePct == null ? null : pct - basePct;
  return (
    <span className="flex items-center justify-end gap-1.5">
      <span className="relative h-[9px] w-[52px] overflow-hidden rounded-[2px] bg-canvas">
        <span className="absolute inset-y-0 left-0 rounded-[2px]"
              style={{ width: `${pct}%`, background: color, opacity: 0.6 }} />
        {basePct != null && (
          <span className="absolute inset-y-0 w-px bg-ink-2" style={{ left: `${basePct}%` }}
                title={`90-day norm ${basePct}%`} />
        )}
      </span>
      <b className="w-8 text-right text-[13px] font-semibold text-ink">{pct}%</b>
      <span className="w-14 text-right text-[9px] text-ink-3">
        {(pct * 7 / 100).toFixed(1)}×/wk
      </span>
      <span className="w-8 text-right text-[9px]"
            style={{ color: delta == null ? "var(--ink-3)"
                     : Math.abs(delta) < 5 ? "var(--ink-3)"
                     : delta > 0 ? "var(--rhr)" : "var(--over)" }}>
        {delta == null ? "" : Math.abs(delta) < 5 ? "≈" : `${delta > 0 ? "+" : ""}${Math.round(delta)}`}
      </span>
    </span>
  );
}

/** Frequency: filled cells toward the target — a count, never a streak. */
function GoalStat({ current, target, period, color }) {
  // With no goal configured this card has no count to show — the goals endpoint only
  // returns CONFIGURED goals. Printing `0` claimed "you did this zero times", which is
  // a fabricated number, not a missing one. Say what is actually true instead.
  if (target == null) {
    return <span className="text-[10px] text-ink-3">no target set</span>;
  }
  const met = current >= target;
  return (
    <span className="flex items-center justify-end gap-1.5">
      {target <= 8 ? (
        <span className="flex gap-[2px]">
          {Array.from({ length: target }).map((_, i) => (
            <span key={i} className="h-[9px] w-[6px] rounded-[1px]"
                  style={{ background: i < current ? (met ? "var(--gold, #fbbf24)" : color) : "var(--rule)" }} />
          ))}
        </span>
      ) : (
        <span className="relative h-[9px] w-[52px] overflow-hidden rounded-[2px] bg-canvas">
          <span className="absolute inset-y-0 left-0" style={{ width: `${Math.min(100, (current/target)*100)}%`, background: met ? "var(--gold, #fbbf24)" : color, opacity: 0.7 }} />
        </span>
      )}
      <b className="text-[13px] font-semibold text-ink">{current}</b>
      <span className="text-[9px] text-ink-3">/{target} {period === "month" ? "mo" : "wk"}</span>
    </span>
  );
}

function Group({ title, note, children }) {
  return (
    <div className="mt-3">
      <div className="flex items-baseline justify-between">
        <p className="text-[9px] tracking-micro text-ink-3">{title}</p>
        <p className="text-[8.5px] text-ink-3">{note}</p>
      </div>
      <div className="mt-1">{children}</div>
    </div>
  );
}

export default function Holding() {
  const DAYS = 30;
  const BASE_DAYS = 90;
  const [showSettled, setShowSettled] = useState(false);

  const { data: adh } = useQuery({
    queryKey: ["habit-adherence", DAYS],
    queryFn: () => api.getHabitsAdherence(DAYS), staleTime: 1000 * 60 * 15,
  });
  // A rate with nothing to sit against is just a number. The 90-day figure is the
  // "typical" reference, so the 30-day headline reads as better/worse than usual
  // rather than as an isolated percentage.
  const { data: adhBase } = useQuery({
    queryKey: ["habit-adherence", BASE_DAYS],
    queryFn: () => api.getHabitsAdherence(BASE_DAYS), staleTime: 1000 * 60 * 15,
  });
  const { data: streaks } = useQuery({ queryKey: ["streaks"], queryFn: api.getStreaks, staleTime: 1000 * 60 * 10 });
  const { data: goals }   = useQuery({ queryKey: ["habit-goals"], queryFn: api.getHabitGoals, staleTime: 1000 * 60 * 10 });
  const { data: corr }    = useQuery({
    queryKey: ["correlations", 180], queryFn: () => api.getCorrelations(180), staleTime: 1000 * 60 * 30,
  });

  const results = corr?.results ?? corr ?? [];
  const byKey = Object.fromEntries((adh?.habits ?? []).map((h) => [h.key, h]));
  const baseByKey = Object.fromEntries((adhBase?.habits ?? []).map((h) => [h.key, h]));

  const ev = (key, rate) => bestEvidence(results, BEHAVIORS[key], { rate });

  const avoidance = Object.entries(BEHAVIORS).filter(([, b]) => b.type === TYPES.AVOIDANCE);
  const binary    = Object.entries(BEHAVIORS).filter(([, b]) => b.type === TYPES.BINARY);
  const frequency = Object.entries(BEHAVIORS).filter(([, b]) => b.type === TYPES.FREQUENCY);

  const isSettled = (k) => {
    const r = byKey[k];
    return r && r.den >= 14 && (r.pct >= SETTLED_HI || r.pct <= SETTLED_LO);
  };
  const liveBinary    = binary.filter(([k]) => !isSettled(k));
  const settledBinary = binary.filter(([k]) => isSettled(k));

  return (
    <Card>
      <Eyebrow left="WHAT'S HOLDING" right={`${DAYS} DAYS`} />
      <HeroSub>
        Each behaviour is measured the way its structure allows — a streak only
        where a long run is actually possible, a rate where it is not, a count
        against a target where the goal is frequency rather than every day.
      </HeroSub>

      <Group title="AVOIDANCE" note="streak — a run means something here">
        {avoidance.map(([k, b]) => {
          const r = byKey[k];
          // NB: streaks use their own key namespace, and the field is `streak`,
          // not `current` — reading the wrong one yields undefined, i.e. a
          // fabricated zero rather than a visible gap.
          const s = streaks?.[b.streakKey ?? k];
          return (
            <Row key={k} emoji={b.emoji} label={b.label} flag={r?.flag} ev={ev(k, r?.pct)}>
              <StreakStat current={s?.streak ?? s?.current} best={s?.best} />
            </Row>
          );
        })}
      </Group>

      <Group title="DAILY" note={`30d rate · tick = ${BASE_DAYS}d norm`}>
        {liveBinary.map(([k, b]) => {
          const r = byKey[k];
          if (!r) return null;
          return (
            <Row key={k} emoji={b.emoji} label={b.label} flag={r.flag} ev={ev(k, r.pct)}>
              <RateStat pct={r.pct} num={r.num} den={r.den} color={r.color} basePct={baseByKey[k]?.pct} />
            </Row>
          );
        })}
        {settledBinary.length > 0 && (
          <div className="mt-1.5 border-t border-rule pt-1.5">
            <button onClick={() => setShowSettled((v) => !v)} className="flex w-full items-baseline gap-1.5 text-left">
              <span className="text-[9.5px] text-ink-3">
                {settledBinary.length} settled — {settledBinary.map(([k]) => BEHAVIORS[k].label.toLowerCase()).join(", ")}
              </span>
              <span className="ml-auto text-[9px] text-ink-3">{showSettled ? "▲" : "▼"}</span>
            </button>
            {showSettled && (
              <div className="mt-1">
                {settledBinary.map(([k, b]) => {
                  const r = byKey[k];
                  return (
                    <Row key={k} emoji={b.emoji} label={b.label} flag={r?.flag} ev={ev(k, r?.pct)}>
                      <RateStat pct={r?.pct ?? 0} num={r?.num ?? 0} den={r?.den ?? 0} color={r?.color} basePct={baseByKey[k]?.pct} />
                    </Row>
                  );
                })}
              </div>
            )}
            <p className="mt-1 text-[8.5px] leading-snug text-ink-3">
              Settled behaviours sit at one extreme of their range. They are rituals
              rather than decisions, and a constant cannot correlate with anything —
              so they are collapsed rather than given equal billing.
            </p>
          </div>
        )}
      </Group>

      <Group title="FREQUENCY" note="count vs target — never a streak">
        {frequency.map(([k, b]) => {
          const g = goals?.[k]; const r = byKey[k];
          return (
            <Row key={k} emoji={b.emoji} label={b.label} ev={ev(k, r?.pct)}>
              <GoalStat current={g?.current ?? 0} target={g?.target} period={g?.period} color={r?.color ?? "var(--hrv)"} />
            </Row>
          );
        })}
      </Group>

      <Foot>
        Manual behaviours count an unlogged day as a miss; automatic ones exclude
        days they cannot judge. That split is why a behaviour you always do can
        still read below 100% — it is measuring the logging, not the doing.
      </Foot>
    </Card>
  );
}
