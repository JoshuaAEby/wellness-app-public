import { useState, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { ResponsiveContainer, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ReferenceLine } from "recharts";
import { api } from "../../api.js";
import { Card, Eyebrow, HeroSub, K, Sep, Foot, SegmentedTabs, EmptyState } from "../../components/ui.jsx";
import { bestEvidence } from "../../lib/behaviors.js";
import { ST_CATS, ST_COLOR, ST_LABEL, fmtMin } from "../../screentime.js";
import { aggregateByPeriod, aggregateScreentimeByPeriod, trimEmpty,
         ScrollChart, ScreentimeCategoryModal, fillDateRange, yearMarkers, labelFor } from "./chartUtils.jsx";

/**
 * DOSE and TIMING behaviours — the ones with a quantity rather than a checkbox.
 *
 * These were previously four unrelated blocks at the very bottom of the tab, below
 * the check-off grid, which inverted their actual value: alcohol and screen time
 * carry the STRONGEST cached evidence of anything the tab tracks,
 * while the checkboxes above them were largely uncorrelatable. So they lead here,
 * each with its evidence stated inline.
 *
 * The window pager (◀ ▶) steps back a whole window at a time rather than only
 * offering fixed presets — reaching further back was a repeated request.
 */

// "ALL" is not a fixed span — alcohol alone goes back years, and the old tab both offered All AND defaulted to it. The rebuild
// dropped it, which silently hid years of history behind a 3M default. EPOCH is
// generous rather than clever: the queries return what exists.
const WINDOWS = { "1M": 30, "3M": 90, "1Y": 365, "ALL": 4000 };

// Daily bars over four years is ~1,460 unreadable slivers, so the window picks a
// sensible default granularity. The D/W/M control still overrides it.
const DEFAULT_PERIOD = { "1M": "day", "3M": "week", "1Y": "week", "ALL": "month" };

function iso(d) {
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
}

function Rho({ v }) {
  return <span style={{ color: v >= 0 ? "var(--rhr)" : "var(--over)" }}>{v>=0?"+":""}{v.toFixed(2)}</span>;
}

function EvidenceLine({ ev }) {
  if (!ev) return null;
  if (ev.reason) {
    return <p className="mt-1 text-[9.5px] text-ink-3">
      {ev.reason === "no_variance" ? "No variance to analyse yet."
        : `Not enough paired days yet${ev.maxN ? ` (best ${ev.maxN})` : ""}.`}
    </p>;
  }
  return (
    <p className="mt-1 text-[9.5px] leading-snug text-ink-3">
      {ev.subject} → {ev.outcome} <Rho v={ev.rho} /> · n={ev.n}
      {ev.tier === "suggestive" && " · suggestive"}
    </p>
  );
}

// `of` renders a denominator: a count with no "out of what" is not a rate, and
// "WEEKS WITH: 12" reads completely differently at 14 weeks than at 200. Context.jsx
// already did this ("of N logged days"); this block did not. Denominators here are the
// number of periods actually IN THE WINDOW, so the pair is always internally consistent
// with the chart beside it.
function Stat({ label, value, unit, of }) {
  return (
    <div className="rounded-lg bg-surface px-2 py-1.5">
      <p className="text-[8.5px] tracking-micro text-ink-3">{label}</p>
      <p className="text-[15px] font-semibold leading-none text-ink">
        {value}{unit && <span className="text-[9px] font-normal text-ink-3">{unit}</span>}
        {of != null && (
          <span className="text-[10px] font-normal text-ink-3">/{of}</span>
        )}
      </p>
    </div>
  );
}

/**
 * One dose series: headline stats, a bar chart with a mean reference, evidence.
 *
 * Optional (FLUIDS, note 80): `stats` replaces the three default tiles, `refY`/`refLabel`
 * replace the mean line (fluids draws its TARGET), `note` is a caption under the header.
 * A row whose value is null is a period the series was NOT LOGGED — no bar, never a zero.
 */
function DoseBlock({ title, unit, rows, color, mean, total, daysWith, nPeriods, evidence,
                    periodLabel = "DAY", fmt = (v)=>v.toFixed(1), query, windowLabel,
                    stats, refY, refLabel, note }) {
  const header = (
    <>
      <div className="flex items-baseline justify-between">
        <p className="text-[9px] tracking-micro text-ink-3">{title}</p>
        <p className="text-[8.5px] text-ink-3">{unit}</p>
      </div>
      {note && <p className="mt-0.5 text-[9px] text-ink-3">{note}</p>}
    </>
  );
  // Empty used to mean `return null` — the whole block vanished (note 53).
  if (!rows?.some((r) => r.value != null)) {
    return (
      <div className="mt-3 border-t border-rule pt-3">
        {header}
        <EmptyState query={query} window={windowLabel} />
      </div>
    );
  }
  const max = Math.max(...rows.map((r) => r.value ?? 0), refY ?? 0, 0);
  const ref = refY ?? (mean > 0 ? mean : null);
  return (
    <div className="mt-3 border-t border-rule pt-3">
      {header}
      <div className="mt-1.5 grid grid-cols-3 gap-1.5">
        {stats ? stats.map((st) => <Stat key={st.label} {...st} />) : (<>
          <Stat label="TOTAL" value={fmt(total)} />
          <Stat label={`PER ${periodLabel}`} value={fmt(mean)} />
          <Stat label={`${periodLabel}S WITH`} value={daysWith} of={nPeriods ?? rows.length} />
        </>)}
      </div>
      <div className="mt-2" style={{ width: "100%", height: 108 }}>
        <ResponsiveContainer>
          <BarChart data={rows} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
            <CartesianGrid stroke="var(--rule)" vertical={false} />
            <XAxis dataKey="key" tickFormatter={labelFor(rows)} tick={{ fontSize: 8.5, fill: "var(--ink-3)" }} interval="preserveStartEnd" />
            {yearMarkers(rows).map((m) => (
              <ReferenceLine key={m.key} x={m.key} stroke="var(--ink-3)" strokeDasharray="2 3"
                strokeOpacity={0.7}
                label={{ value: m.year, position: "insideTopLeft", fontSize: 8,
                         fill: "var(--ink-3)" }} />
            ))}
            <YAxis tick={{ fontSize: 9, fill: "var(--ink-2)" }} width={36} domain={[0, Math.max(max, 1)]} allowDecimals={false} />
            <Tooltip contentStyle={{ background: "var(--card)", border: "1px solid var(--rule)", borderRadius: 8, fontSize: 11 }}
                     labelStyle={{ color: "var(--ink-2)" }}
                     formatter={(v) => (v == null ? "not logged" : fmt(v))} />
            {ref != null && (
              <ReferenceLine y={ref} stroke="var(--ink-3)" strokeDasharray="3 3"
                label={refLabel ? { value: refLabel, position: "insideTopLeft", fontSize: 8,
                                    fill: "var(--ink-3)" } : undefined} />
            )}
            <Bar dataKey="value" fill={color} radius={[2,2,0,0]} isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <EvidenceLine ev={evidence} />
    </div>
  );
}

export default function Exposures() {
  const [win, setWin] = useState("ALL");
  const [offset, setOffset] = useState(0);       // windows back from now
  // Weekly by default: alcohol is a sparse event, and at daily
  // granularity the chart is mostly empty bars. Caffeine reads fine either way.
  const [period, setPeriod] = useState(DEFAULT_PERIOD["ALL"]);
  const [modalCat, setModalCat] = useState(null);
  const [showMac, setShowMac] = useState(false);   // phone is the headline; Mac folds in

  const span = WINDOWS[win];
  const { start, end } = useMemo(() => {
    const e = new Date(); e.setDate(e.getDate() - offset * span);
    const s = new Date(e); s.setDate(s.getDate() - (span - 1));
    return { start: iso(s), end: iso(e) };
  }, [win, offset, span]);

  const alcQ = useQuery({
    queryKey: ["alcohol-trend", start, end], queryFn: () => api.rawTrend("alcohol", start, end, "sum"), staleTime: 6e5 });
  const alcoholRaw = alcQ.data ?? [];
  const caffQ = useQuery({
    queryKey: ["caffeine-trend", start, end], queryFn: () => api.getCaffeineRange(start, end), staleTime: 6e5 });
  const caffRaw = caffQ.data ?? [];
  // Trip days he said weren't logged (note 102): drawn as not logged, never as zero.
  const gapsQ = useQuery({
    queryKey: ["trip-gaps", start, end], queryFn: () => api.getTripGaps(start, end), staleTime: 6e5 });
  const fluidsQ = useQuery({
    queryKey: ["fluids-range", start, end], queryFn: () => api.getFluidsRange(start, end), staleTime: 6e5 });
  const tvQ = useQuery({
    queryKey: ["tv-range", start, end], queryFn: () => api.getTvRange(start, end), staleTime: 6e5 });
  const [tvWho, setTvWho] = useState("me");
  const switchQ = useQuery({
    queryKey: ["switch-trend", start, end], queryFn: () => api.rawTrend("switch_min", start, end, "sum"), staleTime: 6e5 });
  const screenQ = useQuery({
    queryKey: ["screentime-range", start, end], queryFn: () => api.getScreentime(start, end), staleTime: 6e5 });
  const screenRaw = screenQ.data ?? [];
  const { data: corr } = useQuery({
    queryKey: ["correlations", 180], queryFn: () => api.getCorrelations(180), staleTime: 18e5 });

  const results = corr?.results ?? corr ?? [];
  const ev = (metric, as) => bestEvidence(results, { evidence: metric, evidenceAs: as });

  // TRIM the leading/trailing empty periods. Without this the ALL window renders its
  // full nominal span — years of zero bars before the metric existed — which is what
  // made caffeine (logged since 2026-08-03) look like scattered noise across a decade.
  const series = (raw, key) => {
    const filled = fillDateRange(raw ?? [], start, end);
    const agg = aggregateByPeriod(filled.map((r) => ({ ...r, value: r[key] ?? r.value ?? 0 })), period);
    return trimEmpty(agg, (r) => r.value ?? 0);
  };

  /**
   * Averages divide by periods WITH DATA, not by every period in the window.
   *
   * The old version divided by the whole span, so caffeine over the ALL window read
   * "per day 2 mg" — 6,175 mg spread across ~4,000 days, nearly all of which predate
   * caffeine logging entirely. An average over days that never had the metric is not
   * an average of anything.
   */
  const summarise = (rows) => {
    // Not-logged periods (null) are neither zeros nor part of the denominator.
    const vals = rows.filter((r) => r.value != null).map((r) => r.value);
    const active = vals.filter((v) => v > 0);
    const total = vals.reduce((a, b) => a + b, 0);
    return { total, mean: active.length ? total / active.length : 0,
             daysWith: active.length, nPeriods: vals.length };
  };

  /**
   * ALCOHOL · CAFFEINE · FLUIDS share ONE time axis (note 80: "aligned charts").
   * They used to trim separately, so in ALL alcohol started in 2022 and caffeine in
   * 2026-08 on different axes. Now one daily spine → one set of period keys, trimmed on
   * the UNION of the three. A period before a series was ever logged — and, for fluids,
   * any period with no water-logged day — is null ("not logged"), never a zero bar.
   * Fluids is oz per LOGGED day, not a sum: a weekly sum mostly measures how many days
   * were logged.
   */
  const intake = useMemo(() => {
    const spine = fillDateRange([], start, end).map((r) => r.date);
    const by = (raw, key) => Object.fromEntries((raw ?? []).map((r) => [r.date, r[key] ?? r.value ?? 0]));
    const alcBy = by(alcoholRaw, "sdu"), caffBy = by(caffRaw, "mg");
    const flBy = Object.fromEntries((fluidsQ.data?.days ?? []).map((d) => [d.date, d]));
    const firstCaff = fluidsQ.data?.first?.caffeine;
    const agg = (f) => aggregateByPeriod(spine.map((date) => ({ date, value: f(date) })), period);
    // A gap day counts in neither the sum nor the "logged" count, so a period made only of
    // gap days is null; a week with some reads the logged days' sum.
    const gap = (k) => new Set(gapsQ.data?.[k] ?? []);
    const alcGap = gap("alcohol"), caffGap = gap("caffeine"), waterGap = gap("water");
    const alcA = agg((d) => (alcGap.has(d) && !alcBy[d] ? 0 : alcBy[d] || 0));
    const alcOn = agg((d) => (alcGap.has(d) && !alcBy[d] ? 0 : 1));
    const caffA = agg((d) => (caffGap.has(d) ? 0 : caffBy[d] || 0));
    const caffOn = agg((d) => (!caffGap.has(d) && (!firstCaff || d >= firstCaff) ? 1 : 0));
    const flDay = (d) => (waterGap.has(d) ? null : flBy[d]);
    const flSum = agg((d) => flDay(d)?.total || 0);
    const flN = agg((d) => (flDay(d) ? 1 : 0));
    const flT = agg((d) => flDay(d)?.target || 0);
    const rows = alcA.map((r, i) => ({
      key: r.key, label: r.label,
      alc: alcOn[i].value > 0 ? r.value : null,
      caff: caffOn[i].value > 0 ? caffA[i].value : null,
      fl: flN[i].value > 0 ? flSum[i].value / flN[i].value : null,
      flTarget: flN[i].value > 0 ? flT[i].value / flN[i].value : null,
    }));
    const any = (r) => (r.alc ?? 0) > 0 || (r.caff ?? 0) > 0 || r.fl != null;
    let lo = rows.findIndex(any);
    let hi = rows.length - 1 - [...rows].reverse().findIndex(any);
    const kept = lo < 0 ? [] : rows.slice(lo, hi + 1);
    const pick = (k) => kept.map((r) => ({ key: r.key, label: r.label, value: r[k] }));
    // Fluid stats from the logged DAYS inside the kept span.
    const span0 = kept[0]?.key, days = (fluidsQ.data?.days ?? [])
      .filter((d) => (!span0 || d.date >= span0) && !waterGap.has(d.date));
    const atTarget = days.filter((d) => d.total >= d.target).length;
    const avg = (xs) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0);
    return {
      alcRows: pick("alc"), caffRows: pick("caff"), flRows: pick("fl"),
      fluids: { n: days.length, atTarget, mean: avg(days.map((d) => d.total)),
                goal: fluidsQ.data?.goal },
      firstCaff, firstWater: fluidsQ.data?.first?.water,
    };
  }, [alcoholRaw, caffRaw, fluidsQ.data, gapsQ.data, period, start, end]);
  const { alcRows, caffRows, flRows } = intake;
  const alc = summarise(alcRows), caf = summarise(caffRows);
  const fmtSince = (d) => (d ? new Date(d + "T12:00:00").toLocaleDateString("en-US",
    { month: "short", day: "numeric", year: "numeric" }) : null);

  // The endpoint returns {date, phone:{cat:min}, mac:{cat:min}} — aggregation needs
  // one flat column per category, and the Mac fold-in has to happen before summing.
  const screenRows = useMemo(() => {
    const flat = (screenRaw ?? []).map((d) => {
      const row = { date: d.date };
      for (const c of ST_CATS) row[c] = (d.phone?.[c] || 0) + (showMac ? (d.mac?.[c] || 0) : 0);
      return row;
    });
    return trimEmpty(
      aggregateScreentimeByPeriod(fillDateRange(flat, start, end), period),
      (r) => r.total ?? 0);
  }, [screenRaw, period, start, end, showMac]);
  // Named in every empty state, so "nothing" is always "nothing in <when>".
  const windowLabel = win === "ALL" ? "your full history" : `${start} → ${end}`;

  const screenMean = screenRows.length
    ? screenRows.reduce((a,r)=>a+(r.total??0),0)/screenRows.length : 0;

  return (
    <Card>
      <Eyebrow left="EXPOSURES" right={win === "ALL" ? "FULL HISTORY" : `${start.slice(5)} → ${end.slice(5)}`} />
      <HeroSub>
        The behaviours with a quantity rather than a checkbox — and, as it turns out,
        the ones carrying the strongest evidence in the whole tab.
      </HeroSub>

      <div className="mt-2 flex items-center justify-between gap-2">
        <SegmentedTabs options={Object.keys(WINDOWS).map((k)=>({value:k,label:k}))}
                       value={win}
                       onChange={(v)=>{ setWin(v); setOffset(0); setPeriod(DEFAULT_PERIOD[v]); }} />
        <div className="flex items-center gap-1">
          <button onClick={()=>setOffset((o)=>o+1)} disabled={win==="ALL"}
                  className="rounded px-1.5 py-0.5 text-[11px] text-ink-3 hover:text-ink disabled:opacity-30"
                  title="Previous window">◀</button>
          <span className="text-[9px] text-ink-3">{win==="ALL" ? "all" : offset===0 ? "now" : `−${offset}`}</span>
          <button onClick={()=>setOffset((o)=>Math.max(0,o-1))} disabled={offset===0 || win==="ALL"}
                  className="rounded px-1.5 py-0.5 text-[11px] text-ink-3 hover:text-ink disabled:opacity-30"
                  title="Next window">▶</button>
        </div>
        <SegmentedTabs options={[["day","D"],["week","W"],["month","M"]].map(([v,l])=>({value:v,label:l}))}
                       value={period} onChange={setPeriod} />
      </div>

      <DoseBlock title="ALCOHOL" unit={`SDU · per ${period}`} rows={alcRows} color="var(--over)"
                 {...alc} periodLabel={period.toUpperCase()}
                 evidence={ev("alcohol_sdu","drinking")} query={alcQ} windowLabel={windowLabel} />
      <DoseBlock title="CAFFEINE" unit={`mg · per ${period}`} rows={caffRows} color="var(--breakfast, #f59e0b)"
                 {...caf} periodLabel={period.toUpperCase()} fmt={(v)=>Math.round(v)}
                 note={caffRows.some((r) => r.value == null) ? `logged since ${fmtSince(intake.firstCaff)}` : null}
                 evidence={ev("caffeine_total_mg","caffeine")} query={caffQ} windowLabel={windowLabel} />
      <DoseBlock title="FLUIDS" unit={`oz per logged day · per ${period}`} rows={flRows}
                 color="var(--hrv)" fmt={(v)=>Math.round(v)}
                 refY={intake.fluids.goal || null} refLabel="goal"
                 note={`+ 4 oz per SDU of wine or spirits · days before Nov 1 used the old target · days without logged water are not counted (since ${fmtSince(intake.firstWater) ?? "—"})`}
                 stats={[
                   { label: "PER LOGGED DAY", value: Math.round(intake.fluids.mean), unit: " oz" },
                   { label: "GOAL", value: intake.fluids.goal ?? "—", unit: " oz" },
                   { label: "DAYS AT TARGET", value: intake.fluids.atTarget, of: intake.fluids.n },
                 ]}
                 evidence={ev("water_oz","water")} query={fluidsQ} windowLabel={windowLabel} />

      {(() => {
        const rows = series(tvQ.data?.days, tvWho), st = summarise(rows);
        const apps = Object.entries(tvQ.data?.apps?.[tvWho] ?? {});
        const left = tvQ.data?.unanswered_min ?? 0;
        return (
          <DoseBlock title="TV" unit={`min · per ${period}`} rows={rows} color="var(--rem)"
                     {...st} periodLabel={period.toUpperCase()} fmt={(v)=>Math.round(v)}
                     query={tvQ} windowLabel={windowLabel}
                     note={<>
                       <span className="mr-2 inline-flex gap-1 align-middle">
                         {[["me", "ME"], ["family", "FAMILY"], ["other", "OTHER"]].map(([k, label]) => (
                           <button key={k} onClick={() => setTvWho(k)}
                             className={`rounded border px-1.5 py-0.5 text-[8.5px] tracking-micro ${tvWho === k ? "border-ink-3 text-ink" : "border-rule text-ink-3"}`}>
                             {label}
                           </button>
                         ))}
                       </span>
                       {apps.length > 0 && <>{apps.map(([a, m]) => `${a} ${m}m`).join(" · ")} · </>}
                       answered sessions since Oct 31{left > 0 && ` · ${left}m unanswered`}
                     </>} />
        );
      })()}

      {(() => {
        const rows = series(switchQ.data, "value"), st = summarise(rows);
        return (
          <DoseBlock title="SWITCH" unit={`min · per ${period}`} rows={rows} color="var(--rem)"
                     {...st} periodLabel={period.toUpperCase()} fmt={(v)=>Math.round(v)}
                     note="console total, every player · since Nov 1"
                     query={switchQ} windowLabel={windowLabel} />
        );
      })()}

      <div className="mt-3 border-t border-rule pt-3">
        <div className="flex items-baseline justify-between">
          <p className="text-[9px] tracking-micro text-ink-3">SCREEN TIME</p>
          <button onClick={() => setShowMac((v) => !v)}
                  className="text-[8.5px] text-ink-3 hover:text-ink-2">
            {showMac ? "phone + mac" : "phone only"}
          </button>
        </div>
      {screenRows.length === 0 ? (
        // Screen time is pushed by a collector on the Mac, not logged by hand, so an
        // empty CURRENT window almost always means the pipeline stopped — say where to
        // look. A past window may simply predate collection (began 2026-06), so the
        // hint is only offered when the window reaches today.
        <EmptyState query={screenQ} window={windowLabel} what="No screen time received"
          hint={offset === 0
            ? "It arrives from the Mac collector, so this usually means collection stopped — check that the Mac is awake and can reach the server."
            : null} />
      ) : (<>
          <div className="mt-1.5 grid grid-cols-3 gap-1.5">
            {/* Period-aware, like DoseBlock: the rows are D/W/M buckets, so a hardcoded
                "PER DAY" read "5637m per day" on the month view (2026-09-23). */}
            <Stat label={`PER ${period.toUpperCase()}`} value={Math.round(screenMean)} unit="m" />
            <Stat label="PEAK" value={Math.round(Math.max(...screenRows.map((r)=>r.total??0),0))} unit="m" />
            <Stat label={`${period.toUpperCase()}S WITH`} value={screenRows.filter((r)=>(r.total??0)>0).length}
                  of={screenRows.length} />
          </div>
          <div className="mt-2" style={{ width: "100%", height: 118 }}>
            <ResponsiveContainer>
              <BarChart data={screenRows} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}
                        onClick={(st)=>{ const c=st?.activePayload?.[0]?.dataKey; if (c && c!=="total") setModalCat(c); }}>
                <CartesianGrid stroke="var(--rule)" vertical={false} />
                <XAxis dataKey="key" tickFormatter={labelFor(screenRows)} tick={{ fontSize: 8.5, fill: "var(--ink-3)" }} interval="preserveStartEnd" />
            {yearMarkers(screenRows).map((m) => (
              <ReferenceLine key={m.key} x={m.key} stroke="var(--ink-3)" strokeDasharray="2 3"
                strokeOpacity={0.7}
                label={{ value: m.year, position: "insideTopLeft", fontSize: 8,
                         fill: "var(--ink-3)" }} />
            ))}
                <YAxis tick={{ fontSize: 9, fill: "var(--ink-2)" }} width={36} />
                <Tooltip contentStyle={{ background: "var(--card)", border: "1px solid var(--rule)", borderRadius: 8, fontSize: 11 }}
                         labelStyle={{ color: "var(--ink-2)" }} />
                {ST_CATS.map((c, i) => (
                  <Bar key={c} dataKey={c} stackId="s" fill={ST_COLOR[c]}
                       radius={i === ST_CATS.length - 1 ? [2,2,0,0] : undefined}
                       isAnimationActive={false} />
                ))}
              </BarChart>
            </ResponsiveContainer>
          </div>
          <EvidenceLine ev={ev("screen_total","screen time")} />
          <p className="mt-1 text-[9px] leading-snug text-ink-3">
            Entertainment screen time tracks <i>higher</i> HRV in the cache, which is
            almost certainly confounded — rest days mean more TV and better recovery
            at once. Read it as an association, not a lever.
          </p>
      </>)}
      </div>

      {modalCat && (
        <ScreentimeCategoryModal category={modalCat} start={start} end={end} device={null}
                                 onClose={()=>setModalCat(null)} />
      )}

      <Foot>
        Windows step back a full span at a time, so a 3M view can be walked back
        through the whole history rather than only showing the most recent months.
      </Foot>
    </Card>
  );
}
