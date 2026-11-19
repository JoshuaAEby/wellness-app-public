import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { ReferenceArea } from "recharts";
import { api } from "../api.js";

/**
 * Shaded bands over the days you flagged nonstandard — sick, travelling, and so on.
 *
 * WHY THIS EXISTS. `day_flags` has driven correlation and experiment exclusion since it
 * shipped, and the 2026-09-07 sweep wired it into the AI summaries, but no CHART ever
 * showed it. So a week where readiness fell through the floor looked identical whether
 * it was a training problem or four days of illness, and the reader had to remember
 * which. A run of red behind the dip answers that at a glance.
 *
 * SHAPE — a hook plus a pure element factory, NOT a component. Recharts inspects the
 * TYPE of each child to decide what to render, so a custom component that returns
 * <ReferenceArea>s is silently ignored. `interventionMarkers` in chartHelpers solved
 * the same problem the same way, and this matches it:
 *
 *   const flagRuns = useFlagRuns({ rows: data, isoKey: "iso" });
 *   ...
 *   <ComposedChart data={data}>
 *     {flagBands(flagRuns, data, { xKey: "date", isoKey: "iso" })}
 *   </ComposedChart>
 *
 * The hook is called at the top of the parent like any other hook, so hook ordering
 * stays honest; the factory is pure and can be called anywhere in the JSX.
 *
 * DESIGN NOTES
 * ------------
 * • CONSECUTIVE DAYS MERGE INTO ONE BAND. Three separate stripes read as three unrelated
 *   bad days; "Sick, 4 days" is the thing that actually explains the week. Same
 *   reasoning as the run-collapsing in services/summary_context.day_flags_block.
 * • `incomplete_data` is EXCLUDED by default. It is auto-applied, common, and a
 *   statement about the food log rather than about the day — banding it would paper the
 *   chart in stripes meaning "you logged late". Opt in for nutrition-coverage charts.
 * • Low opacity on purpose: this is context BEHIND the data, never a series. It must not
 *   compete with the line it sits under.
 */

// Kept in step with DayFlagControl's REASONS and services/day_flags.REASON_LABELS.
// `incomplete_data` is auto-only and has no chip in DayFlagControl, so it is listed
// here rather than imported from there.
export const FLAG_STYLE = {
  sick:            { label: "Sick",        fill: "#f87171", opacity: 0.13 },
  travel:          { label: "Travel",      fill: "#38bdf8", opacity: 0.11 },
  poor_sleep_env:  { label: "Bad sleep",   fill: "#a78bfa", opacity: 0.11 },
  high_stress:     { label: "Stress",      fill: "#fb923c", opacity: 0.11 },
  other:           { label: "Flagged",     fill: "#94a3b8", opacity: 0.10 },
  incomplete_data: { label: "Partial log", fill: "#64748b", opacity: 0.08 },
};

const AUTO_ONLY = ["incomplete_data"];

/**
 * [{reason, startIso, endIso, days}] — consecutive dates sharing a reason, merged.
 *
 * Accepts EITHER a bare array or the `/api/day-flags` envelope
 * `{flags, reasons, labels}`. The first version of this took only an array and crashed
 * the Body tab with "{} is not iterable" the moment it saw a real response — the one
 * existing consumer (DayFlagControl) unwraps `data.flags`, and this did not. Tolerating
 * both shapes is cheaper than a second place that has to remember.
 */
export function collapseFlagRuns(input, { includeIncomplete = false } = {}) {
  const flags = Array.isArray(input) ? input : (input?.flags ?? []);
  const byReason = new Map();
  for (const f of flags) {
    if (!includeIncomplete && AUTO_ONLY.includes(f.reason)) continue;
    if (!byReason.has(f.reason)) byReason.set(f.reason, []);
    byReason.get(f.reason).push(f.date);
  }
  const runs = [];
  for (const [reason, dates] of byReason) {
    const sorted = [...new Set(dates)].sort();
    let run = null;
    for (const iso of sorted) {
      const contiguous =
        run && new Date(`${iso}T00:00:00`) - new Date(`${run.endIso}T00:00:00`) === 86400000;
      if (contiguous) {
        run.endIso = iso;
        run.days += 1;
      } else {
        if (run) runs.push(run);
        run = { reason, startIso: iso, endIso: iso, days: 1 };
      }
    }
    if (run) runs.push(run);
  }
  return runs.sort((a, b) => (a.startIso < b.startIso ? -1 : 1));
}

/** Fetch + collapse the flags covering `rows`. Call at the top of the chart's owner. */
export function useFlagRuns({ rows, isoKey = "date", includeIncomplete = false }) {
  const span = useMemo(() => {
    const isos = (rows ?? []).map((r) => r?.[isoKey]).filter(Boolean).sort();
    return isos.length ? { start: isos[0], end: isos[isos.length - 1] } : null;
  }, [rows, isoKey]);

  // includeLegacySick: match what the correlation engine and the summaries see, or the
  // bands would draw a shorter illness than the briefing beside them describes.
  const { data: flags } = useQuery({
    queryKey: ["day-flags", span?.start, span?.end, "legacy-sick"],
    queryFn: () => api.getDayFlags(span.start, span.end, { includeLegacySick: true }),
    enabled: Boolean(span),
    staleTime: 1000 * 60 * 10,
  });

  return useMemo(
    () => collapseFlagRuns(flags, { includeIncomplete }),
    [flags, includeIncomplete],
  );
}

/**
 * ReferenceArea elements for `runs`, addressed in the chart's own x values.
 *
 * Charts here are categorical on x (one row per day/week/month), so a band has to land
 * on values Recharts recognises. A run that begins before the visible window is clamped
 * to the first row rather than dropped, and on weekly/monthly buckets a flagged day that
 * falls between two x values snaps outward to the enclosing buckets — otherwise a
 * two-day illness inside one week would vanish entirely.
 */
export function flagBands(runs, rows, {
  // showLabels defaults OFF since 2026-09-14 (notes 32/33): bands, like intervention
  // markers, carry no text on the chart — ChartEventKey / FlagBandLegend explains them.
  xKey = "date", isoKey = "date", showLabels = false, minDaysForLabel = 2,
} = {}) {
  if (!runs?.length || !rows?.length) return null;

  const isoToX = new Map(rows.filter((r) => r?.[isoKey]).map((r) => [r[isoKey], r[xKey]]));
  const isos = [...isoToX.keys()].sort();
  if (!isos.length) return null;
  const first = isos[0];
  const last = isos[isos.length - 1];

  const snap = (iso, dir) => {
    if (isoToX.has(iso)) return isoToX.get(iso);
    const pool = dir === "start" ? isos.filter((d) => d <= iso) : isos.filter((d) => d >= iso);
    const pick = dir === "start" ? pool[pool.length - 1] : pool[0];
    return pick != null ? isoToX.get(pick) : null;
  };

  return runs
    .map((run) => {
      if (run.endIso < first || run.startIso > last) return null;
      const x1 = snap(run.startIso < first ? first : run.startIso, "start");
      const x2 = snap(run.endIso > last ? last : run.endIso, "end");
      if (x1 == null || x2 == null) return null;
      const style = FLAG_STYLE[run.reason] ?? FLAG_STYLE.other;
      return (
        <ReferenceArea
          key={`flag-${run.reason}-${run.startIso}`}
          x1={x1}
          x2={x2}
          ifOverflow="hidden"
          fill={style.fill}
          fillOpacity={style.opacity}
          stroke="none"
          label={
            showLabels && run.days >= minDaysForLabel
              ? { value: style.label, position: "insideTop", fontSize: 8.5, fill: style.fill }
              : undefined
          }
        />
      );
    })
    .filter(Boolean);
}

/** Small inline key for cards that draw bands. Renders nothing when none apply. */
export function FlagBandLegend({ runs }) {
  if (!runs?.length) return null;
  const totals = new Map();
  for (const r of runs) totals.set(r.reason, (totals.get(r.reason) ?? 0) + r.days);
  return (
    <span className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
      {[...totals].map(([reason, days]) => {
        const style = FLAG_STYLE[reason] ?? FLAG_STYLE.other;
        return (
          <span key={reason} className="inline-flex items-center gap-1 text-[9.5px] text-ink-3">
            <span
              className="inline-block h-2 w-3 rounded-[2px]"
              style={{ background: style.fill, opacity: Math.min(style.opacity * 3, 0.5) }}
            />
            {style.label} · {days}d
          </span>
        );
      })}
    </span>
  );
}
