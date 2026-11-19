import { useQuery } from "@tanstack/react-query";
import { api } from "../../api.js";
import { Card, Eyebrow, Foot } from "../../components/ui.jsx";

// Holdout status on each effect. `too_early` is not a judgement: most of these pairs just
// have too few days since they were first seen.
const STATUS = {
  replicating: { label: "replicated", color: "var(--rhr)" },
  consistent: { label: "holding", color: "var(--rhr)" },
  too_early: { label: "too early", color: "var(--ink-3)" },
};

const signed = (v) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(2)}`;

function Effect({ e }) {
  const s = STATUS[e.status] ?? STATUS.too_early;
  const unit = e.unit === "σ" ? "σ" : ` ${e.unit}`;
  return (
    <div className="flex items-baseline justify-between gap-3 py-[3px]">
      <span className="prose-ui min-w-0 text-[12px] text-ink-2">
        {e.outcome}
        {e.next_day && <span className="text-[10.5px] text-ink-3"> next day</span>}
      </span>
      <span className="flex flex-none items-baseline gap-2">
        <span className="text-[12px] font-semibold tabular-nums text-ink">{signed(e.effect)}{unit}</span>
        <span className="w-[62px] text-right text-[10.5px]" style={{ color: s.color }}>{s.label}</span>
      </span>
    </div>
  );
}

function Lever({ l, rank }) {
  return (
    <div className="border-t border-rule py-3 first:border-t-0 first:pt-2 last:pb-0">
      <div className="flex items-baseline gap-2">
        <span className="flex-none text-[11px] tabular-nums text-ink-3">{rank}</span>
        <span className="text-[14px] font-semibold text-ink">{l.label}</span>
        <span className="rounded border border-rule px-1 py-px text-[9.5px] font-semibold tracking-micro text-ink-2">
          {l.verb}
        </span>
        <span className="ml-auto flex-none text-[10.5px] text-ink-3">{l.per_label}</span>
      </div>
      {l.now && (
        <p className="prose-ui mt-1 text-[11.5px] text-ink-3">
          Now <span className="text-ink-2">{l.now}</span>
          {l.before && <> · before {l.before}</>}
          {l.now_days < 14 && <> · {l.now_days} days</>}
        </p>
      )}
      <div className="mt-1.5">
        {l.effects.map((e) => <Effect key={`${e.outcome}${e.next_day}`} e={e} />)}
      </div>
    </div>
  );
}

/**
 * The few things worth changing (backend services/levers.py), above the full findings list.
 * Effects are per the step shown, from the same slopes as Surviving's DETAILS. σ matches the
 * briefing: a z-score has no reliable bpm or ms equivalent here.
 */
export default function Levers() {
  const { data } = useQuery({
    queryKey: ["levers"], queryFn: () => api.getLevers(), staleTime: 1000 * 60 * 30,
  });
  const levers = data?.levers ?? [];
  if (levers.length === 0) return null;
  const weeks = Math.round((data.exposure_days ?? 28) / 7);
  return (
    <Card>
      <Eyebrow left="YOUR LEVERS" right={`LAST ${weeks} WEEKS`} />
      <div className="mt-1">
        {levers.map((l, i) => <Lever key={l.key} l={l} rank={i + 1} />)}
      </div>
      <Foot>
        Things you control, ranked: effects that held up on new data first, then the biggest
        across your usual range. Left out: findings that weakened on new data, were confounded or
        pulled against each other, and inputs that aren&apos;t real levers. &quot;Now&quot; skips
        travel and sick days, except alcohol, which counts every day. Still correlational. All of
        it stays in All findings below.
      </Foot>
    </Card>
  );
}
