import { useQuery } from "@tanstack/react-query";
import { api } from "../../api.js";
import { Card, Eyebrow, HeroSub, K, Foot } from "../../components/ui.jsx";
import { BEHAVIORS, bestEvidence } from "../../lib/behaviors.js";

/**
 * The two reasons a behaviour has nothing to say — which look identical on screen
 * and mean opposite things.
 *
 *   NO VARIANCE   the behaviour never changes, so it is uncorrelatable BY
 *                 CONSTRUCTION. A constant has no covariance with anything. More
 *                 logging will never help; only doing it differently would.
 *   TOO SPARSE    there simply are not enough paired days yet. This one IS
 *                 fixable, and the fix is to keep logging.
 *
 * Collapsing those two into a shared blank (or worse, into "no effect") is how a
 * ritual you have performed 77 times out of 77 ends up looking like a failed
 * hypothesis. It also hides the genuinely useful third case: a behaviour that
 * varies plenty and still shows nothing is exactly what an n-of-1 experiment is
 * for, so that list is the experiment queue.
 */

const MIN_N = 30;
const SETTLED_HI = 98, SETTLED_LO = 2;

function Line({ emoji, label, right, note }) {
  return (
    <div className="border-t border-rule py-1.5 first:border-t-0">
      <div className="flex items-baseline gap-2">
        <span className="w-4 flex-none text-center text-[11px]">{emoji}</span>
        <span className="min-w-0 flex-1 truncate text-[11px] text-ink-2">{label}</span>
        <span className="flex-none text-[10px] text-ink-3">{right}</span>
      </div>
      {note && <p className="mt-0.5 pl-6 text-[9px] leading-snug text-ink-3">{note}</p>}
    </div>
  );
}

export default function NotEvaluable() {
  const { data: adh } = useQuery({
    queryKey: ["habit-adherence", 30], queryFn: () => api.getHabitsAdherence(30), staleTime: 9e5 });
  const { data: corr } = useQuery({
    queryKey: ["correlations", 180], queryFn: () => api.getCorrelations(180), staleTime: 18e5 });

  const results = corr?.results ?? corr ?? [];
  const byKey = Object.fromEntries((adh?.habits ?? []).map((h) => [h.key, h]));

  const noVariance = [], sparse = [], candidates = [];

  for (const [key, b] of Object.entries(BEHAVIORS)) {
    const r = byKey[key];
    const rate = r?.pct;
    const solid = r && r.den >= 14;
    const ev = bestEvidence(results, b, { rate });

    if (solid && (rate >= SETTLED_HI || rate <= SETTLED_LO)) {
      noVariance.push({ key, b, rate });
    } else if (ev?.reason === "sparse") {
      sparse.push({ key, b, maxN: ev.maxN });
    } else if (ev && !ev.reason && ev.tier === "weak" && solid) {
      candidates.push({ key, b, ev });
    }
  }

  if (!noVariance.length && !sparse.length && !candidates.length) return null;

  return (
    <Card>
      <Eyebrow left="NOT YET EVALUABLE" right={`${noVariance.length + sparse.length} BEHAVIOURS`} />
      <HeroSub>
        Two different kinds of silence. One is fixable by logging more; the other
        never will be, because a behaviour that never varies cannot correlate with
        anything by construction.
      </HeroSub>

      {noVariance.length > 0 && (
        <div className="mt-3">
          <p className="text-[9px] tracking-micro text-ink-3">NO VARIANCE — NOTHING TO LEARN</p>
          <div className="mt-1">
            {noVariance.map(({ key, b, rate }) => (
              <Line key={key} emoji={b.emoji} label={b.label}
                    right={<K>{rate}%</K>}
                    note={rate >= SETTLED_HI
                      ? "Done essentially every day — a ritual, not a decision."
                      : "Essentially never done — the target needs redesigning, or dropping."} />
            ))}
          </div>
          <p className="mt-1.5 text-[9px] leading-snug text-ink-3">
            These are not failures of evidence. A constant has no covariance with
            anything, so no amount of further logging will make them analysable.
          </p>
        </div>
      )}

      {sparse.length > 0 && (
        <div className="mt-3 border-t border-rule pt-3">
          <p className="text-[9px] tracking-micro text-ink-3">TOO SPARSE — KEEP LOGGING</p>
          <div className="mt-1">
            {sparse.map(({ key, b, maxN }) => (
              <Line key={key} emoji={b.emoji} label={b.label}
                    right={<span className="text-ink-3">{maxN ?? 0}/{MIN_N} days</span>}
                    note={`About ${Math.max(0, MIN_N - (maxN ?? 0))} more paired days before this can be tested.`} />
            ))}
          </div>
        </div>
      )}

      {candidates.length > 0 && (
        <div className="mt-3 border-t border-rule pt-3">
          <p className="text-[9px] tracking-micro text-ink-3">WORTH TESTING DIRECTLY</p>
          <div className="mt-1">
            {candidates.map(({ key, b, ev }) => (
              <Line key={key} emoji={b.emoji} label={b.label}
                    right={<span className="text-ink-3">n={ev.n}</span>}
                    note="Varies enough to analyse, but nothing survives correction — an n-of-1 would settle it faster than more observation." />
            ))}
          </div>
        </div>
      )}

      <Foot>
        Observation can only ever show co-occurrence. Where a behaviour varies and
        still shows nothing, a randomised trial is the shorter path — that is what
        the Experiments view is for.
      </Foot>
    </Card>
  );
}
