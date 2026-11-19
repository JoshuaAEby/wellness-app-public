import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api.js";
import { Card, Eyebrow, Hero, HeroSub, K, Sep, Foot, Legend } from "../../components/ui.jsx";
import MarkedChanges from "../../components/MarkedChanges.jsx";

/**
 * Randomised n-of-1 experiments — the only view in this tab whose claims are
 * CONFIRMATORY.
 *
 * It gets its own subtab because it answers a categorically different question
 * from the rest of Insights. Signals says what happened; Findings says what
 * co-occurs and how much to trust it. Only this view says what has actually been
 * tested against a randomised comparison — which is the only evidence that
 * licenses "do X because it causes Y".
 *
 * BLINDING
 * --------
 * Accrual is deliberately outcome-free (`/experiments/active/accrual` returns no
 * results by design). Watching an effect estimate accumulate while you are still
 * generating the data is how an n-of-1 trial gets talked into a conclusion, so
 * progress and outcome are separate endpoints and only progress is shown while a
 * trial is running.
 */

/**
 * The pre-registration record and any protocol amendments, verbatim.
 *
 * Collapsed by default but always present: a pre-registered trial is only
 * distinguishable from a fishing expedition by this text, and it was stored in the
 * database since the beginning while never being shown anywhere. An amendment nobody
 * can read is not an amendment.
 */
function Protocol({ notes }) {
  const [open, setOpen] = useState(false);
  if (!notes) return null;
  const amended = notes.includes("AMENDMENT");
  return (
    <div className="mt-3 border-t border-rule pt-2">
      <button onClick={() => setOpen((v) => !v)} className="flex w-full items-baseline gap-2">
        <span className="text-[9px] tracking-micro text-ink-3">PROTOCOL</span>
        {amended && (
          <span className="text-[9px]" style={{ color: "var(--breakfast)" }}>· amended</span>
        )}
        <span className="ml-auto text-[9px] text-ink-3">{open ? "▲" : "▼"}</span>
      </button>
      {open && (
        <pre className="prose-ui mt-1.5 whitespace-pre-wrap break-words text-[10px] leading-snug text-ink-3">
          {notes}
        </pre>
      )}
    </div>
  );
}

function pct(n, d) {
  return d > 0 ? Math.round((n / d) * 100) : 0;
}

/** Per-arm accrual against target, as one bar per arm. */
function ArmBar({ label, n, target, color }) {
  const p = Math.min(100, pct(n, target));
  return (
    <div>
      <div className="flex items-baseline justify-between text-[9.5px]">
        <span className="tracking-micro text-ink-2">{label}</span>
        <span className="text-ink-3">
          <b className="font-semibold text-ink">{n}</b> / {target}
        </span>
      </div>
      <div className="mt-1 h-[14px] overflow-hidden rounded-md bg-surface">
        <div className="h-full transition-[width] duration-300"
          style={{ width: `${p}%`, backgroundColor: color, opacity: 0.65 }} />
      </div>
    </div>
  );
}

export default function Experiments() {
  const { data: accrual } = useQuery({
    queryKey: ["experiment-accrual"],
    queryFn: api.getExperimentAccrual,
    staleTime: 1000 * 60 * 10,
  });
  const { data: experiments = [] } = useQuery({
    queryKey: ["experiments"],
    queryFn: api.getExperiments,
    staleTime: 1000 * 60 * 10,
  });

  const running = accrual?.active ? accrual.experiments : [];
  const finished = (experiments ?? []).filter((e) => e.ended_at);

  return (
    <div className="space-y-4 p-4">
      <Card>
        <Eyebrow left="EXPERIMENTS" right={running.length ? `${running.length} RUNNING` : "NONE RUNNING"} />
        <HeroSub>
          The only place in this tab where a result licenses a causal claim. Everything
          under Findings is co-occurrence; these are randomised comparisons against a
          pre-set target, analysed by intention-to-treat.
        </HeroSub>
      </Card>

      {running.map((e) => {
        const target = e.target_per_arm ?? 30;
        const behind = Math.abs(e.n_a - e.n_b);
        return (
          <Card key={e.id}>
            <Eyebrow left={e.kind?.toUpperCase() ?? "EXPERIMENT"}
              right={`DAY ${e.days_running}`} />
            <p className="prose-ui mt-2 text-[13px] font-semibold leading-snug text-ink">
              {e.label}
            </p>
            <Hero value={`${e.pct_complete}%`} unit={["ACCRUED", `TARGET ${target}/ARM`]} />
            <HeroSub>
              {e.projected_completion === "complete"
                ? <>Target reached — ready to analyse.</>
                : e.projected_completion
                  ? <>On current pace, complete around <K>{e.projected_completion}</K></>
                  : <>Not enough eligible days yet to project a finish.</>}
              {behind >= 5 && (
                <><Sep />arms are <K>{behind}</K> days apart — the shorter arm sets the
                  usable n</>
              )}
            </HeroSub>

            <div className="mt-3 space-y-2">
              <ArmBar label={e.arm_a?.toUpperCase()} n={e.n_a} target={target} color="var(--hrv)" />
              <ArmBar label={e.arm_b?.toUpperCase()} n={e.n_b} target={target} color="var(--under)" />
            </div>

            {/* Ineligible days are the hidden cost of an n-of-1: a day excluded for
                travel or illness is a day the trial did not advance, and without
                this breakdown a slow trial looks like a slow protocol rather than
                a filtering problem. */}
            {e.n_ineligible > 0 && (
              <div className="mt-3 border-t border-rule pt-3">
                <p className="text-[9px] tracking-micro text-ink-3">
                  {e.n_ineligible} DAYS NOT COUNTED
                </p>
                <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1">
                  {Object.entries(e.ineligible ?? {})
                    .sort((a, b) => b[1] - a[1])
                    .map(([reason, n]) => (
                      <span key={reason} className="text-[10px] text-ink-2">
                        <b className="font-semibold text-ink">{n}</b>{" "}
                        <span className="text-ink-3">{reason.replace(/_/g, " ")}</span>
                      </span>
                    ))}
                </div>
              </div>
            )}

            <Protocol notes={e.notes} />

            <Foot>
              Progress only — no outcome is shown while a trial runs. Watching an
              effect accumulate while you are still producing the data is how an
              n-of-1 gets argued into a conclusion, so the analysis stays behind a
              separate endpoint until the trial ends.
            </Foot>
          </Card>
        );
      })}

      {!running.length && (
        <Card>
          <Eyebrow left="NOTHING RUNNING" />
          <HeroSub>
            No experiment is currently accruing. Experiments run one question at a
            time per kind, so a new one starts when the previous finishes.
          </HeroSub>
        </Card>
      )}

      {finished.length > 0 && (
        <Card>
          <Eyebrow left="COMPLETED" right={`${finished.length}`} />
          <div className="mt-2 space-y-2">
            {finished.map((e) => (
              <div key={e.id} className="flex items-baseline gap-2 text-[11px]">
                <span className="prose-ui min-w-0 flex-1 truncate text-ink-2">{e.label}</span>
                <span className="flex-none text-[9.5px] text-ink-3">{e.ended_at}</span>
              </div>
            ))}
          </div>
          <Foot>Finished trials keep their analysis; ending a trial is what unblinds it.</Foot>
        </Card>
      )}

      <MarkedChanges />
    </div>
  );
}
