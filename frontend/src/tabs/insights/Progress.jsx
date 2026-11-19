import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../../api.js";
import { Card, Eyebrow, HeroSub, K, Foot, EmptyState } from "../../components/ui.jsx";
import ScoreChartSheet from "../../components/ScoreChartSheet.jsx";
import { ACWR_HIGH, ACWR_LABEL, ACWR_LOW, ACWR_VERY_HIGH } from "../../lib/acwr.js";

/**
 * PROGRESS — absolute change since training began (note 56, 2026-09-25).
 *
 * Every other view in the app is deliberately stationary: HRV and RHR are z-scores
 * against a rolling baseline, and readiness is built on them, so real improvement is
 * absorbed into the baseline and the daily numbers sit near zero. Measured: the HRV
 * baseline rose ~10% and the RHR baseline fell ~5% while the monthly z-scores
 * stayed within ±0.2. This view compares against FIXED references instead: the same dates
 * last year decide a verdict where they exist (2026-11-02; the four weeks before training
 * fell in a winter trough, so a rebound read as a gain), and those four weeks are context.
 *
 * Colour carries state only (see the design-system rule): a change is coloured only
 * when its 95% interval excludes zero. "Unclear" is grey, not a softer green.
 */

const VERDICT = {
  improved: { text: "improved", color: "var(--rhr)" },
  worsened: { text: "worse", color: "var(--over)" },
  unclear: { text: "unclear", color: "var(--ink-3)" },
  insufficient: { text: "too little data", color: "var(--ink-3)" },
  rebound: { text: "rebound", color: "var(--ink-2)" },
  within_range: { text: "in past range", color: "var(--ink-3)" },
  up: { text: "up", color: "var(--ink-2)" },
  down: { text: "down", color: "var(--ink-2)" },
};

const fmtDay = (iso) =>
  iso ? new Date(`${iso}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "—";
const sign = (v) => (v == null ? "—" : `${v > 0 ? "+" : ""}${v}`);

function Spark({ monthly, color }) {
  const vals = (monthly || []).map((m) => m.median).filter((v) => v != null);
  if (vals.length < 2) return <span className="inline-block w-16" />;
  const lo = Math.min(...vals), hi = Math.max(...vals), W = 64, H = 18;
  const pts = vals.map((v, i) => `${(i / (vals.length - 1)) * W},${H - ((v - lo) / (hi - lo || 1)) * (H - 2) - 1}`);
  return (
    <svg width={W} height={H} className="flex-none" aria-hidden="true">
      <polyline points={pts.join(" ")} fill="none" stroke={color} strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}

// The ANCHOR LIFTS · NEXT SESSION card left Progress 2026-11-04: next-session targets are an
// action, and Today (Rho's card, on lifting days) and the class board already give them. The
// Scorecard's anchor row below keeps the progress question.

// SCORECARD (note 67): one row per area, at a glance. CTL/ACWR are one row each, not the
// headline — they measure the WORK, and once the routine is steady CTL plateaus at the
// mean daily load, so it stops saying anything about improvement. The result rows
// (resting HR, HRV, VO2max, anchors) say whether the same work is making you fitter.
// Colour carries state only: results use the improved/worse/unclear verdicts; load rows
// are neutral except ACWR when it is elevated (a genuine warning state).
const ACWR_COLOR = { light: "var(--ink-2)", normal: "var(--ink-2)", high: "var(--over)", very_high: "var(--over)" };

function ScoreRow({ area, label, value, sub, verdict, color, series, more, chart, extra }) {
  // Tap opens the full-size plot (note 68) — the detail text moved into the sheet.
  const [open, setOpen] = useState(false);
  return (
    <div className="border-t border-rule first:border-t-0">
      <button type="button" onClick={() => setOpen(true)}
              className="flex w-full items-center gap-2 py-2 text-left">
        <div className="min-w-0 flex-1">
          <p className="text-[8.5px] font-semibold tracking-micro text-ink-3">{area}</p>
          <p className="text-[12px] text-ink">{label} <span className="text-ink-3">›</span></p>
          {sub && <p className="text-[9.5px] leading-snug text-ink-3 tabular-nums">{sub}</p>}
        </div>
        <Spark monthly={series} color={color === "var(--ink-3)" ? "var(--ink-3)" : color} />
        <div className="w-28 text-right tabular-nums">
          <p className="text-[12px] font-semibold text-ink">{value}</p>
          <p className="text-[9.5px] font-semibold" style={{ color }}>{verdict}</p>
        </div>
      </button>
      <ScoreChartSheet open={open} onClose={() => setOpen(false)} area={area} title={label}
        value={value} verdict={verdict} color={color} more={more} extra={extra}
        points={chart?.points} unit={chart?.unit} target={chart?.target} targetLabel={chart?.targetLabel}
        targetBand={chart?.targetBand} domain={chart?.domain} bars={chart?.bars} integer={chart?.integer}
        bands={chart?.bands} />
    </div>
  );
}

const addDays = (iso, n) => {
  const d = new Date(`${iso}T12:00:00`);
  d.setDate(d.getDate() + n);
  return d.toISOString().slice(0, 10);
};
const weeklyPts = (rows) => (rows || []).map((w) => ({ x: w.week_of, y: w.value }));

// Anchors: progress vs the last session ON OR BEFORE the program began (the baseline the
// backend returns in since_start). Index series = each session's load ÷ that lift's
// baseline, averaged per date, so four lifts in different units share one curve.
function anchorSummary(anchors) {
  const withBase = anchors.filter((a) => a.since_start?.baseline_load != null);
  const tracked = withBase.filter((a) => a.since_start.sessions >= 1);
  const up = tracked.filter((a) => {
    const { baseline_load: b, last_load: l } = a.since_start;
    return a.assisted ? l < b : l > b;
  });
  const byDate = {};
  for (const a of withBase) {
    const b = a.since_start.baseline_load;
    for (const { date, load } of a.since_start.loads || []) {
      if (!b || load == null) continue;
      const r = a.assisted ? b / Math.max(load, 1) : load / b;   // up = better either way
      (byDate[date] ??= []).push(r);
    }
  }
  const dates = Object.keys(byDate).sort();
  const avg = (d) => byDate[d].reduce((x, y) => x + y, 0) / byDate[d].length;
  const series = dates.map((d) => ({ median: avg(d) }));
  const points = dates.map((d) => ({ x: d, y: Math.round(avg(d) * 100) / 100, label: fmtDay(d) }));
  const lines = withBase.map((a) => {
    const s = a.since_start;
    const u = a.assisted ? " lb assist" : " lb";
    return s.sessions ? `${a.short} ${s.baseline_load}→${s.last_load}${u}`
                      : `${a.short}: no session since ${fmtDay(s.baseline_date)}`;
  });
  return { tracked, up, series, points, lines };
}

function Scorecard({ p }) {
  const aq = useQuery({ queryKey: ["anchors"], queryFn: () => api.getAnchors(), staleTime: 10 * 60 * 1000 });
  // Trips (3+ days) shade the steps plot — the weeks CTL reads as near-zero training.
  const tq = useQuery({ queryKey: ["trips"], queryFn: api.getTrips, staleTime: 10 * 60 * 1000 });
  const tripBands = (tq.data?.trips ?? []).filter((x) => x.days >= 3)
    .map((x) => ({ label: "", from: x.start, to: x.end }));
  const act = p.activity;
  const anc = anchorSummary(aq.data?.anchors ?? []);
  const t = p.training || {};
  const w = p.weight;
  const improved = p.metrics.filter((m) => m.verdict === "improved").length;
  const AREA = { rhr: "AEROBIC FITNESS", hrv: "RECOVERY CAPACITY", hrr: "RECOVERY SPEED",
                 walk_hr: "AEROBIC FITNESS", vo2: "AEROBIC CAPACITY" };
  const nowBand = { label: "NOW", from: addDays(p.end, -27), to: p.end, strong: true };
  const beforeBand = (x) => (x?.baseline_label && x.baseline_label !== "before training"
    ? { label: "FIRST MONTH", from: p.start, to: addDays(p.start, 27) }
    : { label: "BEFORE", from: p.baseline_from, to: p.start });
  const bands = (x) => [beforeBand(x), nowBand];
  const vs = (x) => x.baseline_label === "before training"
    ? `${fmtDay(p.baseline_from)}–${fmtDay(p.start)}` : `your ${x.baseline_label}`;
  const byYear = (x) => x.verdict_basis === "last_year";
  // VO₂ has no 90-day year check; its year-over-year verdict (same dates since training began,
  // and it must beat its best earlier month) used to live on a separate VS LAST YEAR card,
  // folded in here 2026-11-04 so Progress gives one answer per marker.
  const y = p.vs_last_year || {};
  const vo2Year = y.vo2 && y.vo2.verdict !== "insufficient" ? y.vo2 : null;
  const rowSub = (x) => {
    if (byYear(x)) return `Last ${x.year.days ?? 90} d ${x.year.now} vs ${x.year.last_year} ${x.unit} a year ago · ${x.before} before training`;
    if (x.key === "vo2" && vo2Year) return `Since ${fmtDay(y.start)} ${vo2Year.now} vs ${vo2Year.last_year} a year ago · best earlier month ${vo2Year.past_best ?? "—"}`;
    return x.before != null ? `${x.before} ${x.unit} in ${vs(x)}` : null;
  };
  const rowMore = (x) => {
    const c = byYear(x) ? x.year : { ci_low: x.change_ci_low, ci_high: x.change_ci_high };
    const against = byYear(x) ? " against the same dates last year" : "";
    const beforeLine = byYear(x) && x.change != null
      ? `Against ${vs(x)}: ${sign(x.change)} ${x.unit}${x.change_ci_low != null ? ` (${sign(x.change_ci_low)} to ${sign(x.change_ci_high)})` : ""}. ` : "";
    return <>{c.ci_low != null && `95% interval ${sign(c.ci_low)} to ${sign(c.ci_high)} ${x.unit}${against}. `}{beforeLine}{x.note}</>;
  };
  return (
    <Card>
      <Eyebrow left="SCORECARD" right="VS SAME DATES LAST YEAR" />
      <HeroSub>
        <K>{improved}</K> of {p.metrics.length} fitness markers clearly improved. Where last year exists it
        decides: <K>rebound</K> means better than {fmtDay(p.baseline_from)}–{fmtDay(p.start)}, but not better
        than a year ago. Tap a row for detail.
      </HeroSub>
      <div className="mt-1">
        {p.metrics.map((x) => {
          const yearVo2 = x.key === "vo2" && vo2Year;
          const v = VERDICT[yearVo2 ? vo2Year.verdict : x.verdict] ?? VERDICT.unclear;
          return (
            <ScoreRow key={x.key} area={AREA[x.key] ?? ""} label={x.label} series={x.monthly} color={v.color}
              value={`${x.now ?? "—"} ${x.unit}`}
              sub={rowSub(x)}
              verdict={`${sign(byYear(x) ? x.year.change : yearVo2 ? vo2Year.change : x.change)} · ${v.text}`}
              more={<>{rowMore(x)}{yearVo2 && <> VO₂ must also beat its best month before training ({vo2Year.past_best}): the estimate wanders by several points without training.</>}</>}
              extra={x.key === "walk_hr" ? <YearChart y={y} /> : null}
              chart={{ points: weeklyPts(x.weekly), unit: x.unit, bands: bands(x) }} />
          );
        })}
        <ScoreRow area="STRENGTH" label="Anchor lifts" series={anc.series}
          value={anc.tracked.length ? `${anc.up.length} of ${anc.tracked.length} up` : "—"}
          sub="vs your last session on or before Sep 25"
          verdict={!anc.tracked.length ? "no sessions since" : anc.up.length ? "progressing" : "holding"}
          color={anc.up.length ? "var(--rhr)" : "var(--ink-3)"}
          more={anc.lines.length ? `${anc.lines.join(" · ")}. The plot is load ÷ each lift's starting load, averaged per session date (1.0 = where you started; assisted lifts inverted so up is always better).` : "No anchor lifts logged yet."}
          chart={{ points: anc.points, unit: "× start", target: 1, targetLabel: "start", bands: [] }} />
        <ScoreRow area="CONSISTENCY" label="Gym sessions / week"
          series={(t.sessions_weekly || []).map((x) => ({ median: x.n }))}
          value={`${t.sessions_per_week ?? "—"} / ${t.weekly_target ?? 3}`}
          sub="average of the last 4 complete weeks"
          verdict={t.weeks_on_target != null ? `${t.weeks_on_target} of 4 weeks on target` : ""}
          color={t.weeks_on_target >= 3 ? "var(--rhr)" : "var(--ink-3)"}
          more={`Last 12 weeks: ${(t.sessions_weekly || []).slice(-12).map((x) => x.n).join(" · ")}. Gym sessions only — walks don't count.`}
          chart={{ points: (t.sessions_weekly || []).map((x) => ({ x: x.week_of, y: x.n })), unit: "sessions",
                   target: t.weekly_target, targetLabel: "weekly target", bars: true, integer: true,
                   bands: [{ label: "LAST 4 WK", from: addDays(p.end, -27), to: p.end, strong: true }] }} />
        <ScoreRow area="TRAINING LOAD" label="Fitness load (CTL)" series={t.ctl_monthly} color="var(--ink-2)"
          value={t.ctl_now ?? "—"}
          sub={t.ctl_peak != null ? `peak ${t.ctl_peak} on ${fmtDay(t.ctl_peak_date)}` : null}
          verdict={t.ctl_trend ? `${t.ctl_trend} over 8 wk${t.ctl_change_8w != null ? ` (${sign(t.ctl_change_8w)})` : ""}` : ""}
          more="A ~6-week rolling average of daily training load (heart-rate based). At a steady routine it levels off at your average daily load — that's expected, not a stall. It barely registers strength work."
          chart={{ points: weeklyPts(t.ctl_weekly), unit: "CTL", bands: bands(null) }} />
        <ScoreRow area="RAMP" label="Acute : chronic (ACWR)" series={t.acwr_monthly}
          value={t.acwr_now ?? "—"} sub={`${ACWR_LOW}–${ACWR_HIGH.toFixed(1)} is your normal range`}
          verdict={t.acwr_band ? (ACWR_LABEL[t.acwr_band] ?? t.acwr_band) : ""} color={ACWR_COLOR[t.acwr_band] ?? "var(--ink-2)"}
          more={`This week's load against your 6-week norm. A guardrail for ramping up too fast, not a fitness score. Rho eases effort above ${ACWR_HIGH.toFixed(1)} and keeps it easy above ${ACWR_VERY_HIGH}; a light week below ${ACWR_LOW} is fine. The textbook 0.8–1.3 assumes a smoother ratio than yours, which has an SD of about 0.7.`}
          chart={{ points: weeklyPts(t.acwr_weekly), unit: "ACWR", targetBand: [ACWR_LOW, ACWR_HIGH], targetLabel: "normal range",
                   bands: bands(null) }} />
        {act && act.n > 0 && (
          <ScoreRow area="MOVEMENT" label="Daily steps" series={act.monthly} color="var(--ink-2)"
            value={act.now != null ? Math.round(act.now).toLocaleString() : "—"}
            sub={act.before != null ? `${Math.round(act.before).toLocaleString()} in ${vs(act)} · not scored` : null}
            verdict={act.change != null ? `${act.change > 0 ? "+" : act.change < 0 ? "−" : "±"}${Math.abs(Math.round(act.change)).toLocaleString()}` : ""}
            more={act.note}
            chart={{ points: weeklyPts(act.weekly), unit: "steps/day", integer: true,
                     bands: [...bands(act), ...tripBands] }} />
        )}
        {w && (
          <ScoreRow area="BODY" label="Weight" series={w.monthly} color="var(--ink-2)"
            value={`${w.now ?? "—"} lb`}
            sub={w.before != null ? `${w.before} lb in ${fmtDay(p.baseline_from)}–${fmtDay(p.start)} · not scored` : null}
            verdict={sign(w.change)} more={w.note}
            chart={{ points: weeklyPts(w.weekly), unit: "lb", bands: bands(w) }} />
        )}
      </div>
      <Foot>
        Rows with a year-ago value compare the last 90 days with the same dates last year, which cancels season.
        Before = median of the 4 weeks before {fmtDay(p.start)} (when training load first held above a low level);
        now, the large value, = median of the last 4 weeks. A result is coloured only when its 95% interval (block
        bootstrap over 7-day blocks) excludes zero. Sick and travel days are left out of the results (steps keeps them, to show
        what training load can't). Load rows describe the work, not the result.
      </Foot>
    </Card>
  );
}

// NUTRITION SCORECARD — "all of the above" (2026-10-04): every nutrition trend
// worth watching, same row shape as the fitness card. Complete days only; means (several
// rows are yes/no days); coloured only when the bootstrap interval excludes zero.
// "Vs break-even" replaces total calories and is described, never scored.
const NUTRITION_AREA = { protein: "MACROS", fiber: "MACROS", variety: "QUALITY", veg: "QUALITY",
  sugar: "LIMITS", sodium: "LIMITS", alcohol: "ALCOHOL", dry: "ALCOHOL", dessert: "EVENINGS",
  post_dinner: "EVENINGS", balance: "ENERGY", complete: "DATA" };

function NutritionScorecard() {
  const q = useQuery({ queryKey: ["nutrition-scorecard"], queryFn: () => api.getNutritionScorecard(),
                       staleTime: 30 * 60 * 1000 });
  const n = q.data;
  if (!n?.available) {
    return <Card><Eyebrow left="NUTRITION SCORECARD" /><EmptyState query={q} /></Card>;
  }
  const ref = `${fmtDay(n.reference.from)}–${fmtDay(n.reference.to)}`;
  const improved = n.rows.filter((r) => r.verdict === "improved").length;
  const worse = n.rows.filter((r) => r.verdict === "worsened").length;
  return (
    <Card>
      <Eyebrow left="NUTRITION SCORECARD" right={`VS ${ref}`.toUpperCase()} />
      <HeroSub>
        Last 4 weeks ({fmtDay(n.recent.from)}–{fmtDay(n.recent.to)}) against {ref}: <K>{improved}</K> clearly
        better, <K>{worse}</K> clearly worse, the rest within noise. Fully-logged days only. Tap a row for detail.
      </HeroSub>
      <div className="mt-1">
        {n.rows.map((r) => {
          const v = VERDICT[r.verdict] ?? VERDICT.unclear;
          const neutral = r.better == null;
          const val = r.key === "balance" && r.now != null
            ? `${r.now > 0 ? "+" : ""}${r.now} ${r.unit}` : `${r.now ?? "—"} ${r.unit}`;
          const sub = r.key === "balance" && r.now != null
            ? `${r.now >= 0 ? "above" : "below"} break-even · was ${sign(r.before)} in ${ref}`
            : r.before != null ? `${r.before} ${r.unit} in ${ref}` : "no earlier data to compare";
          const extra = [
            r.target != null && `Target ${r.target}${r.key === "sodium" ? " mg (guideline)" : ` ${r.unit.split("/")[0]}`}.`,
            r.now_days_at_target != null && `On target ${r.now_days_at_target} days/week lately.`,
            r.ci_low != null && `95% interval for the change ${sign(r.ci_low)} to ${sign(r.ci_high)}.`,
            r.note,
          ].filter(Boolean).join(" ");
          return (
            <ScoreRow key={r.key} area={NUTRITION_AREA[r.key] ?? ""} label={r.label}
              series={(r.weekly || []).map((w) => ({ median: w.value }))}
              color={neutral ? "var(--ink-2)" : v.color} value={val} sub={sub}
              verdict={r.change != null ? `${sign(r.change)} · ${v.text}` : v.text}
              more={extra || null}
              chart={{ points: weeklyPts(r.weekly), unit: r.unit, target: r.target ?? null,
                       domain: r.unit === "% of days" ? [0, 100] : undefined,
                       bands: [{ label: "BEFORE", from: n.reference.from, to: n.reference.to },
                               { label: "NOW", from: n.recent.from, to: n.recent.to, strong: true }] }} />
          );
        })}
      </div>
      <Foot>
        Curves are weekly averages over 16 weeks. A row is coloured only when its 95% interval (block bootstrap,
        7-day blocks) excludes zero. Alcohol counts every day; everything else reads fully-logged days.
      </Foot>
    </Card>
  );
}

// Walking HR month by month against the same months a year earlier (backend
// progress.year_over_year). Was the VS LAST YEAR card; it now opens from the Scorecard's
// walking HR row (2026-11-04), whose 90-day verdict is the one the Cardio headline uses.
function YearChart({ y }) {
  const w = y?.walk_hr;
  if (!w?.months?.length) return null;
  return (
    <div className="mt-3 border-t border-rule pt-3">
      <p className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">MONTH BY MONTH VS A YEAR EARLIER</p>
      <div className="mt-2 h-[150px]">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={w.months} margin={{ top: 6, right: 8, bottom: 0, left: -18 }}>
            <CartesianGrid vertical={false} stroke="var(--rule)" />
            <XAxis dataKey="month" tick={{ fill: "var(--ink-3)", fontSize: 10 }} tickLine={false}
              axisLine={false} interval="preserveStartEnd" />
            <YAxis tick={{ fill: "var(--ink-3)", fontSize: 10 }} tickLine={false} axisLine={false}
              domain={["dataMin - 2", "dataMax + 2"]} allowDecimals={false} width={44} />
            <Tooltip contentStyle={{ background: "var(--canvas)", border: "1px solid var(--rule)", borderRadius: 8 }}
              labelStyle={{ color: "var(--ink-3)" }} itemStyle={{ color: "var(--ink)" }}
              formatter={(value, name) => [`${value} bpm`, name]} />
            <Line dataKey="last" name="Year before" stroke="var(--ink-3)" strokeWidth={2}
              dot={false} connectNulls={false} isAnimationActive={false} />
            <Line dataKey="this" name="Last 12 months" stroke="var(--hrv)" strokeWidth={2}
              dot={{ r: 2.5, fill: "var(--hrv)" }} connectNulls={false} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <p className="mt-1.5 flex items-center gap-4 text-[10.5px] text-ink-3">
        <span className="flex items-center gap-1.5"><span className="h-[2px] w-3" style={{ background: "var(--hrv)" }} />last 12 months</span>
        <span className="flex items-center gap-1.5"><span className="h-[2px] w-3" style={{ background: "var(--ink-3)" }} />year before</span>
        <span className="ml-auto">monthly median</span>
      </p>
      <p className="mt-1.5 text-[10.5px] leading-snug text-ink-3">
        Apple&apos;s daily walking average, travel and sick days left out. Walking speed only exists
        from January 2026 (steady at about 2.4–2.5 mph since), so last year&apos;s pace can&apos;t be
        checked; weight and terrain aren&apos;t controlled for either.
      </p>
    </div>
  );
}

function RateRow({ label, r }) {
  const v = VERDICT[r.verdict] ?? VERDICT.unclear;
  return (
    <div className="flex items-baseline justify-between gap-3 border-t border-rule py-1.5 text-[11px] tabular-nums">
      <span className="min-w-0 text-ink-2">{label}</span>
      {r.rate == null ? <span className="text-ink-3">{v.text}</span> : (
        <span className="flex-none font-semibold" style={{ color: v.color }}>
          {sign(r.rate)}%/mo <span className="font-normal text-ink-3">[{sign(r.ci_low)}, {sign(r.ci_high)}]</span>
        </span>
      )}
    </div>
  );
}

// Class sets change rep scheme every time and rarely reach failure, so no logged number is a
// max (backend progress.strength_progress). Two measures that hold up anyway: load at equal
// reps across every lift, and each lift's heaviest set at the same reps early and lately. The
// rate has an interval, so it may take colour; the per-lift comparison has none, so it stays ink.
function StrengthCard({ s }) {
  const o = s.overall, rr = s.recent_rate;
  return (
    <Card>
      <Eyebrow left="STRENGTH" right={`SINCE ${fmtDay(s.from)}`.toUpperCase()} />
      {o.rate == null ? (
        <HeroSub>Too few repeated lifts yet to read a rate.</HeroSub>
      ) : (
        <HeroSub>
          Load at equal reps across <K>{o.n_lifts}</K> lifts: <K>{sign(o.rate)}%</K> a month since {fmtDay(s.from)}
          {rr.rate != null && <>, <K>{sign(rr.rate)}%</K> a month over the last {Math.round(rr.days / 30)} months</>}.
        </HeroSub>
      )}
      <div className="mt-2">
        <RateRow label="All lifts" r={o} />
        <RateRow label={`Last ${Math.round(rr.days / 30)} months`} r={rr} />
        {s.groups.map((g) => <RateRow key={g.key} label={g.label} r={g} />)}
      </div>
      <p className="mt-3 text-[10.5px] text-ink-3">
        Heaviest at the same reps, {fmtDay(s.from)}–{fmtDay(s.early_to)} → since {fmtDay(s.recent_from)}
      </p>
      {s.lifts.length === 0 ? <p className="mt-1 text-[11px] text-ink-3">No lift has enough sessions in both periods.</p> : (
        <>
          <p className="prose-ui mt-1 text-[11px] text-ink-2 tabular-nums">
            <K>{s.n_up}</K> up, <K>{s.n_same}</K> same, <K>{s.n_down}</K> down
            {s.typical_pct != null && <>, typically {sign(s.typical_pct)}%</>}. {s.lifts.length} of {s.n_all_lifts} lifts
            were repeated enough to compare.
          </p>
          <div className="mt-1">
            {s.lifts.map((l) => (
              <div key={l.exercise} className="flex items-baseline gap-2 border-t border-rule py-1.5 text-[11px] tabular-nums first:border-t-0">
                <span className="min-w-0 flex-1 break-words text-ink-2">{l.exercise}</span>
                <span className="flex-none text-ink">
                  {l.early_lb} → {l.recent_lb} lb{l.assisted ? " assist" : ""} × {l.reps}+
                </span>
                <span className="w-9 flex-none text-right text-ink-3">{l.verdict}</span>
              </div>
            ))}
          </div>
        </>
      )}
      <Foot>
        Rate: top set per lift per session, log load on lift, reps and time, lifts with 3+ sessions; intervals
        resample training days. Comparison: heaviest load for 10+ reps (least band help for 5+), lifts with 2+
        sessions in both periods. Few sets go near failure, so these track what you chose to lift, not your
        maximum. Dumbbells are the combined weight; sets before Apr 2 were logged per hand and are left out.
      </Foot>
    </Card>
  );
}

export default function Progress() {
  const q = useQuery({ queryKey: ["progress"], queryFn: () => api.getProgress(), staleTime: 30 * 60 * 1000 });
  const p = q.data;
  if (!p?.available) {
    return (
      <div className="space-y-4 p-4">
        <Card>
          <Eyebrow left="PROGRESS" />
          <EmptyState query={q} message={p?.reason ? `Not available — ${p.reason}.` : null} />
        </Card>
      </div>
    );
  }
  const s = p.strength;

  return (
    <div className="space-y-4 p-4">
      <Scorecard p={p} />

      <NutritionScorecard />

      <StrengthCard s={s} />

      {p.tape && <Card>
        <Eyebrow left="BODY" right="TAPE" />
        {p.tape && (
          <p className="mt-2 text-[11px] text-ink-2 tabular-nums">
            {p.tape.n < 2 ? (
              <>Tape baseline set {fmtDay(p.tape.first_date)} — waist {p.tape.waist_first}″, arm {p.tape.arm_first}″,
                chest {p.tape.chest_first}″. The next measurement starts the trend.</>
            ) : (
              <>Tape {fmtDay(p.tape.first_date)} → {fmtDay(p.tape.last_date)}: waist {p.tape.waist_first} → <K>{p.tape.waist_last}</K>″,
                arm {p.tape.arm_first} → <K>{p.tape.arm_last}</K>″, chest {p.tape.chest_first} → <K>{p.tape.chest_last}</K>″</>
            )}
          </p>
        )}
      </Card>}

    </div>
  );
}
