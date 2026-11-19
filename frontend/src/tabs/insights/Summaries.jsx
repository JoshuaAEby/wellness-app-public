import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api.js";
import { Card, Eyebrow, HeroSub, Foot } from "../../components/ui.jsx";
import { Plot } from "../../components/ScoreChartSheet.jsx";

// `[chart: id]` on its own line (note 66). The model only NAMES a chart from the server's
// fixed menu; the data and the caption come from the database (services/summary_charts).
const CHART_RE = /^\s*\[chart:\s*([a-z_]+)\s*\]\s*$/i;

function SummaryChart({ chart }) {
  if (!chart) return null;
  return (
    <figure className="my-2 rounded-lg border border-rule bg-card px-2 pt-2 pb-1">
      <p className="px-1 text-[11px] font-semibold text-ink">{chart.label}</p>
      <Plot points={chart.points} color="var(--ink-2)" unit={chart.unit} target={chart.target}
            targetLabel={chart.targetLabel} targetBand={chart.targetBand} domain={chart.domain}
            bars={chart.bars} integer={chart.unit === "drinks" ? false : undefined} bands={chart.bands || []} />
      <figcaption className="px-1 pb-1 text-[9.5px] leading-snug text-ink-3">{chart.caption}</figcaption>
    </figure>
  );
}

/**
 * The model's written summaries — daily, weekly, monthly.
 *
 * Moved here from Signals because these are INTERPRETATION, not observation: they
 * are the model's reading of what the numbers mean, which is the same question
 * Findings asks, rather than the "what happened" that Signals reports.
 *
 * IMPORTANT CAVEAT, AND WHY THE FRAMING IS DELIBERATE
 * ---------------------------------------------------
 * Everything else in Findings passes a statistical gate: FDR correction, an
 * effective-n, a confidence interval, a synthetic control that has to fail. These
 * summaries pass none of that. They are generated prose, and sitting them beside
 * "+0.49, n=100, CI 0.29-0.65" risks them borrowing that authority by adjacency.
 *
 * So they are placed LAST in the view, kept collapsed by default, and labelled as
 * written rather than measured. The rule elsewhere in this app is that a claim
 * shows its evidence next to it; the honest version of that rule here is to say
 * plainly that there isn't any.
 */

// Moved with AISummaryText — it is the inline **bold** / `code` renderer that
// AISummaryText calls, and nothing in Trends used it once the summaries left.
function renderInline(text) {
  const parts = text.split(/(\*\*[^*]+\*\*)/g);
  return parts.map((p, i) =>
    p.startsWith("**") && p.endsWith("**")
      ? <strong key={i} className="text-white font-semibold">{p.slice(2, -2)}</strong>
      : p
  );
}

export function AISummaryText({ text, chartCtx }) {
  const allLines = text.split("\n").filter(l => l.trim());
  // One fetch per summary for every chart it references.
  const ids = allLines.map((l) => l.match(CHART_RE)?.[1]?.toLowerCase()).filter(Boolean);
  const { data: charts = [] } = useQuery({
    queryKey: ["summary-charts", chartCtx?.kind, chartCtx?.start, chartCtx?.end, ids.join(",")],
    queryFn: () => api.getSummaryCharts(chartCtx.kind, chartCtx.start, chartCtx.end, ids),
    enabled: !!chartCtx?.start && ids.length > 0, staleTime: 10 * 60 * 1000,
  });
  const chartById = Object.fromEntries(charts.map((c) => [c.id, c]));

  const takeawayIdx = allLines.findIndex(l =>
    /^[\*\s]*(KEY TAKEAWAYS|KEY FINDINGS)/i.test(l.trim().replace(/\*\*/g, ""))
  );
  const takeawayBullets = takeawayIdx >= 0
    ? allLines.slice(takeawayIdx + 1).filter(l => l.trim().startsWith("•") || l.trim().startsWith("- "))
    : [];
  const bodyLines = takeawayIdx >= 0
    ? allLines.filter((l, i) => {
        if (i === takeawayIdx) return false;
        if (i > takeawayIdx && (l.trim().startsWith("•") || l.trim().startsWith("- "))) return false;
        return true;
      })
    : allLines;

  const renderLine = (line, i) => {
    const t = line.trim();
    const cm = t.match(CHART_RE);
    if (cm) return <SummaryChart key={i} chart={chartById[cm[1].toLowerCase()]} />;
    const stripped = t.replace(/^\*\*(.+)\*\*$/, "$1");
    const isHeader = /^(SUMMARY|RECOMMENDATIONS|WEEK SUMMARY|TRAINING|NUTRITION|MONTHLY SUMMARY|KEY TAKEAWAYS|KEY FINDINGS)/i.test(stripped);
    const isBullet = t.startsWith("•") || t.startsWith("- ");
    const labelMatch = t.match(/^\*\*([^*]+):\*\*\s*(.*)/s);

    if (isHeader) return (
      <p key={i} className="text-xs font-semibold uppercase tracking-wider text-ink-3 pt-1">
        {stripped.replace(/:$/, "")}
      </p>
    );
    if (labelMatch) return (
      <p key={i} className="text-sm text-ink-2 leading-relaxed">
        <span className="font-semibold text-ink">{labelMatch[1]}:</span>
        {" "}{renderInline(labelMatch[2])}
      </p>
    );
    if (isBullet) return (
      <p key={i} className="text-sm text-ink-2 leading-relaxed pl-1">{renderInline(t)}</p>
    );
    return (
      <p key={i} className="text-sm text-ink-2 leading-relaxed">{renderInline(t)}</p>
    );
  };

  return (
    <div className="space-y-3">
      {takeawayBullets.length > 0 && (
        <div className="rounded-xl bg-slate-700/60 border border-slate-600/40 px-4 py-3 space-y-2">
          {takeawayBullets.map((bullet, i) => (
            <p key={i} className="text-sm font-medium text-ink leading-relaxed">
              {renderInline(bullet.trim())}
            </p>
          ))}
        </div>
      )}
      {bodyLines.length > 0 && (
        <div className="space-y-1.5">
          {bodyLines.map((line, i) => renderLine(line, i))}
        </div>
      )}
    </div>
  );
}

// ── Weekly digest ─────────────────────────────────────────────────────────────

function SummaryBlock({ title, cadence, text, open, onToggle, onRefresh, refreshing, error, loading, chartCtx }) {
  return (
    <div className="overflow-hidden rounded-lg bg-surface">
      {/* The toggle and the refresh control are SIBLINGS, not nested. A <button>
          inside a <button> is invalid DOM and the inner click target is unreliable. */}
      <div className="flex items-center gap-2 px-3 py-2.5">
        <button className="flex min-w-0 flex-1 items-center justify-between gap-2" onClick={onToggle}>
          <span className="flex min-w-0 items-center gap-2">
            <span className="flex-none text-[9.5px] font-semibold tracking-eyebrow text-ink-3">{title}</span>
            {!open && text && (
              <span className="truncate text-[10px] text-ink-3">
                {text.replace(/\*\*/g, "").split("\n")
                  .find((l) => l.trim() && !CHART_RE.test(l) && !/^(SUMMARY|RECOMMENDATIONS)/i.test(l.trim()))
                  ?.trim().slice(0, 46)}…
              </span>
            )}
          </span>
          <span className="flex-none text-[10px] text-ink-3">{open ? "▲" : "▼"}</span>
        </button>
        <button
          onClick={onRefresh}
          disabled={refreshing}
          className="flex-none text-[11px] text-ink-3 transition-colors hover:text-ink disabled:opacity-40"
          title={text ? "Regenerate" : "Generate"}
        >{refreshing ? "…" : text ? "↻" : "+"}</button>
      </div>
      {open && (
        <div className="px-3 pb-3">
          {error && (
            <p className="mb-2 text-[10.5px]" style={{ color: "var(--over)" }}>
              Failed to regenerate — check the API key in Settings
            </p>
          )}
          {loading || refreshing
            ? <p className="text-[11px] text-ink-3">Loading…</p>
            : text
              ? <AISummaryText text={text} chartCtx={chartCtx} />
              : <div className="rounded-lg border border-dashed border-rule px-3 py-2.5 text-center">
                  <p className="text-[11px] text-ink-3">{cadence}</p>
                  <p className="mt-0.5 text-[10px] text-ink-3">Tap ↻ to generate now.</p>
                </div>}
        </div>
      )}
    </div>
  );
}

export default function Summaries() {
  const qc = useQueryClient();
  const [dailyOpen, setDailyOpen] = useState(false);
  const [weeklyOpen, setWeeklyOpen] = useState(false);
  const [monthlyOpen, setMonthlyOpen] = useState(false);

  const { data: todayData, isLoading } = useQuery({ queryKey: ["today"], queryFn: api.today });
  const inval = { onSuccess: () => qc.invalidateQueries({ queryKey: ["today"] }) };
  const refreshDaily   = useMutation({ mutationFn: api.refreshDailySummary, ...inval });
  const refreshWeekly  = useMutation({ mutationFn: api.refreshWeeklySummary, ...inval });
  const refreshMonthly = useMutation({ mutationFn: api.refreshMonthlySummary, ...inval });

  const weekly = todayData?.weekly_summary;
  const monthly = todayData?.monthly_summary;

  // NB: the payload field is `summary_text`, not `summary` — and the period comes
  // from week_start/week_end (monthly: month_start), not a pre-formatted label.
  const md = (d) => new Date(`${d}T12:00:00`).toLocaleDateString("en-US", { month: "short", day: "numeric" });
  const weeklyRange = weekly?.week_start && weekly?.week_end
    ? `${md(weekly.week_start)}\u2013${md(weekly.week_end)}` : null;
  const monthLabel = monthly?.month_start
    ? new Date(`${monthly.month_start}T12:00:00`).toLocaleDateString("en-US", { month: "long", year: "numeric" })
    : null;

  return (
    <Card>
      <Eyebrow left="WRITTEN SUMMARIES" right="NOT MEASURED" />
      <HeroSub>
        The model's interpretation, in prose. Unlike everything above it, none of this
        passes a statistical gate — no correction, no interval, no control. Treat it as
        a readable account of the data, not as evidence about you.
      </HeroSub>

      <div className="mt-3 space-y-2">
        <SummaryBlock
          title="DAILY" cadence="Generated each morning at 8:05."
          text={todayData?.metrics?.ai_summary}
          open={dailyOpen} onToggle={() => setDailyOpen((v) => !v)}
          onRefresh={() => refreshDaily.mutate()} refreshing={refreshDaily.isPending}
          error={refreshDaily.isError} loading={isLoading}
        />
        <SummaryBlock
          title={weeklyRange ? `WEEKLY · ${weeklyRange}` : "WEEKLY"}
          cadence="Generated Sundays at noon."
          text={weekly?.summary_text}
          chartCtx={weekly?.week_start ? { kind: "weekly", start: weekly.week_start, end: weekly.week_end } : null}
          open={weeklyOpen} onToggle={() => setWeeklyOpen((v) => !v)}
          onRefresh={() => refreshWeekly.mutate()} refreshing={refreshWeekly.isPending}
          error={refreshWeekly.isError} loading={isLoading}
        />
        <SummaryBlock
          title={monthLabel ? `MONTHLY · ${monthLabel.toUpperCase()}` : "MONTHLY"}
          cadence="Generated on the 1st."
          text={monthly?.summary_text}
          chartCtx={monthly?.month_start ? { kind: "monthly", start: monthly.month_start, end: monthly.month_end } : null}
          open={monthlyOpen} onToggle={() => setMonthlyOpen((v) => !v)}
          onRefresh={() => refreshMonthly.mutate()} refreshing={refreshMonthly.isPending}
          error={refreshMonthly.isError} loading={isLoading}
        />
      </div>

      <Foot>
        Written by a language model from the same data shown above. It can restate a
        coincidence as confidently as a confirmed finding, so where it disagrees with
        the numbers, the numbers win.
      </Foot>
    </Card>
  );
}
