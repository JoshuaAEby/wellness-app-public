import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api.js";

// API spend in the header (note 51). The chip shows this month's dollars; tapping it
// opens the breakdown. Figures are the app's own per-call estimates from
// MODEL_PRICING (ai_summaries.py). Checked against Anthropic's live cost report for
// 2026-09-01..22: org $5.04 vs this app $4.64, the difference being the other apps'
// keys. The local log was silent 2026-06-07 → 09-02; that gap was filled from
// Anthropic's usage report (feature "backfill"), and `gaps` flags any new silence.

const FEATURE_LABELS = {
  food_parse: "Food parsing",
  food_tag: "Food-group tagging",
  label_parse: "Nutrition-label scans",
  recipe_estimate: "Recipe estimates",
  daily_summary: "Daily briefing",
  weekly_summary: "Weekly summary",
  monthly_summary: "Monthly summary",
  blood_parse: "Blood panel (text)",
  blood_parse_pdf: "Blood panel (PDF)",
  backfill: "Filled from Anthropic's report",
};

function usd(v) {
  if (v == null) return "—";
  if (v > 0 && v < 0.01) return "<$0.01";
  return `$${v.toFixed(2)}`;
}

function fmtDay(iso) {
  return new Date(`${iso}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

export default function SpendMenu() {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  const { data, isError } = useQuery({
    queryKey: ["ai-usage"],
    queryFn: api.getUsage,
    staleTime: 5 * 60 * 1000,
  });

  // Close on any tap outside the chip + panel.
  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [open]);

  const month = data?.this_month;
  const all = data?.all_time;
  const features = Object.entries(data?.month_by_feature ?? {})
    .sort(([, a], [, b]) => b.cost_usd - a.cost_usd);

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        aria-label="API spend"
        aria-expanded={open}
        className="rounded-lg px-1.5 py-1 text-xs tabular-nums text-slate-400 hover:text-white transition-colors"
      >
        {isError ? "$?" : month ? usd(month.cost_usd) : "$…"}
      </button>

      {open && (
        // Anchored to the VIEWPORT, not the chip: the chip sits left of Notes/Settings/
        // Sign out, so a chip-anchored panel ran 87px off the left edge at 375px.
        <div className="fixed right-3 top-14 z-50 w-72 max-w-[calc(100vw-24px)] rounded-xl border border-rule bg-card p-3 text-xs shadow-xl">
          <p className="text-[9px] tracking-micro text-ink-3">API SPEND · CLAUDE</p>
          {isError && (
            <p className="mt-2 text-[color:var(--over)]">Couldn't load usage — the request failed.</p>
          )}
          {data && (
            <>
              <div className="mt-2 grid grid-cols-2 gap-2">
                <div className="rounded-lg bg-surface px-2 py-1.5">
                  <p className="text-[8.5px] tracking-micro text-ink-3">{(data.month_label ?? "THIS MONTH").toUpperCase()}</p>
                  <p className="text-[17px] font-semibold leading-tight text-ink">{usd(month.cost_usd)}</p>
                  <p className="text-[9.5px] text-ink-3">{month.calls} calls</p>
                </div>
                <div className="rounded-lg bg-surface px-2 py-1.5">
                  <p className="text-[8.5px] tracking-micro text-ink-3">ALL TIME</p>
                  <p className="text-[17px] font-semibold leading-tight text-ink">{usd(all.cost_usd)}</p>
                  <p className="text-[9.5px] text-ink-3">
                    {all.calls} calls{data.first_call ? ` · since ${fmtDay(data.first_call)}` : ""}
                  </p>
                  {all.backfilled_usd > 0 && (
                    <p className="text-[9px] leading-snug text-ink-3">
                      incl. {usd(all.backfilled_usd)} filled in from Anthropic's usage report
                    </p>
                  )}
                </div>
              </div>

              {data.gaps?.length > 0 && (
                <p className="mt-2 leading-snug text-[color:var(--breakfast)]">
                  Not recorded{" "}
                  {data.gaps.map((g, i) => (
                    <span key={g.from}>{i > 0 && ", "}{fmtDay(g.from)} → {fmtDay(g.to)} ({g.days} d)</span>
                  ))}
                  {" "}— the all-time total is missing that period.
                </p>
              )}

              {features.length > 0 && (
                <div className="mt-3 border-t border-rule pt-2">
                  <p className="mb-1 text-[8.5px] tracking-micro text-ink-3">THIS MONTH BY FEATURE</p>
                  {features.map(([f, a]) => (
                    <div key={f} className="flex items-baseline justify-between py-0.5 tabular-nums">
                      <span className="text-ink-2">{FEATURE_LABELS[f] ?? f}</span>
                      <span className="text-ink-3">{a.calls} · <span className="text-ink">{usd(a.cost_usd)}</span></span>
                    </div>
                  ))}
                </div>
              )}

              <p className="mt-2 text-[9.5px] leading-snug text-ink-3">
                Estimated per call from token counts at list prices; cross-checked against Anthropic's bill.
              </p>
            </>
          )}
        </div>
      )}
    </div>
  );
}
