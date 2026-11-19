import { useState } from "react";
import { Card, Eyebrow } from "./ui.jsx";
import { AISummaryText } from "../tabs/insights/Summaries.jsx";

// The written summaries, surfaced on Today when they are fresh (2026-10-21):
//   • DAILY   — today's briefing, whenever it exists (it used to live only in the push and
//               at the bottom of Insights → Findings, where it was effectively invisible)
//   • WEEKLY  — Sunday (generated at noon) and Monday
//   • MONTHLY — the 1st to the 3rd
// The card starts collapsed to one row; inside, weekly/monthly (long, with charts) are
// collapsed again. Same renderer as the
// Summaries view, charts included; the full archive stays in Findings.
const DAY = 86400000;
const daysSince = (iso, todayIso) =>
  Math.round((new Date(`${todayIso}T12:00:00`) - new Date(`${iso}T12:00:00`)) / DAY);
const md = (d) => new Date(`${d}T12:00:00`).toLocaleDateString("en-US", { month: "short", day: "numeric" });

function Expandable({ label, sub, text, chartCtx }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-t border-rule pt-2">
      <button type="button" onClick={() => setOpen((v) => !v)} className="flex w-full items-baseline gap-2 text-left">
        <span className="text-[8.5px] font-semibold tracking-micro text-ink-3">{label}</span>
        <span className="flex-1 text-[11px] text-ink-2">{sub}</span>
        <span className="text-[11px] text-ink-3">{open ? "▾" : "▸ Read"}</span>
      </button>
      {open && <div className="mt-1.5"><AISummaryText text={text} chartCtx={chartCtx} /></div>}
    </div>
  );
}

export default function TodaySummaries({ todayData, todayStr }) {
  const m = todayData?.metrics;
  const daily = m?.date === todayStr ? m?.ai_summary : null;
  const w = todayData?.weekly_summary;
  const mo = todayData?.monthly_summary;
  const showWeekly = w?.summary_text && w.week_end && daysSince(w.week_end, todayStr) >= 0
    && daysSince(w.week_end, todayStr) <= 1;
  const showMonthly = mo?.summary_text && mo.month_end && daysSince(mo.month_end, todayStr) >= 1
    && daysSince(mo.month_end, todayStr) <= 3;
  const [open, setOpen] = useState(false);
  if (!daily && !showWeekly && !showMonthly) return null;
  const parts = [daily && "today's briefing", showWeekly && "this week", showMonthly && "last month"].filter(Boolean);

  // Collapsed by default (2026-10-21): one row saying what's inside; tap to read.
  if (!open) {
    return (
      <button type="button" onClick={() => setOpen(true)}
        className="flex w-full items-center gap-2 rounded-2xl border border-rule bg-card px-3 py-2.5 text-left">
        <span className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">SUMMARIES</span>
        <span className="flex-1 truncate text-[11px] text-ink-2">{parts.join(" · ")}</span>
        <span className="text-[10px] text-ink-3">▸</span>
      </button>
    );
  }

  return (
    <Card>
      <button type="button" onClick={() => setOpen(false)} className="block w-full text-left">
        <Eyebrow left="SUMMARIES" right="WRITTEN BY AI · NOT MEASURED ▾" />
      </button>
      {daily && (
        <div className="mt-1.5">
          <p className="mb-1 text-[8.5px] font-semibold tracking-micro text-ink-3">TODAY'S BRIEFING</p>
          <AISummaryText text={daily} />
        </div>
      )}
      <div className={daily ? "mt-2 space-y-2" : "mt-1.5 space-y-2"}>
        {showWeekly && (
          <Expandable label="WEEK" sub={`${md(w.week_start)}–${md(w.week_end)}`} text={w.summary_text}
            chartCtx={{ kind: "weekly", start: w.week_start, end: w.week_end }} />
        )}
        {showMonthly && (
          <Expandable label="MONTH"
            sub={new Date(`${mo.month_start}T12:00:00`).toLocaleDateString("en-US", { month: "long", year: "numeric" })}
            text={mo.summary_text} chartCtx={{ kind: "monthly", start: mo.month_start, end: mo.month_end }} />
        )}
      </div>
    </Card>
  );
}
