import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ResponsiveContainer, ComposedChart, Scatter, Line,
  XAxis, YAxis, CartesianGrid, Tooltip, ReferenceLine,
} from "recharts";
import { api } from "../api.js";
import { Card, Eyebrow, Hero, HeroSub, K, Sep, Foot, Legend, SegmentedTabs } from "./ui.jsx";

/**
 * Heart-rate recovery — how far HR falls in the minute after a session ends.
 *
 * A validated fitness and autonomic marker, and the one fitness signal here that does
 * NOT depend on VO2max — which is unreliable for this user, since half of recent walks
 * record no HR and Apple needs sustained elevated HR to update it. HRR only needs the
 * watch worn through cooldown.
 *
 * TWO VIEWS, because the scatter alone was answering the wrong question. "Where do I
 * stand" and "am I improving" are different, and neither is legible from a cloud of
 * points:
 *
 *   VS EFFORT — HRR against the peak it recovered from, with a per-category Theil-Sen
 *     fit and its 95% band. Peak matters a lot here (measured slopes +0.49 to +0.64
 *     bpm per bpm, all CIs clear of zero), which is exactly why a single headline
 *     number would mislead. The fit line IS the "expected" the eye was looking for:
 *     your own peak-adjusted expectation, not a population norm.
 *
 *   OVER TIME — the residual from that fit, i.e. HRR with the peak effect removed,
 *     against date. This is the fitness question. Raw HRR over time is confounded by
 *     how hard the sessions happened to be.
 *
 * WHEN THE BAND SPANS ZERO THE CARD SAYS SO. As of writing, every category's time
 * trend is flat with a wide interval, so the honest headline is "no detectable
 * change" — drawing a confident trend line through that spread is the thing this
 * rewrite exists to avoid.
 *
 * NO CLINICAL CUTOFF. The widely cited 12 bpm floor comes from a standardised maximal
 * treadmill test with a controlled active cooldown. These are gym classes and walks
 * with whatever cooldown happened, so a third of sessions fall below 12 for protocol
 * reasons that say nothing about health. Showing that line invited exactly that
 * misreading.
 */

const CAT_COLOR = { hiit: "var(--over)", strength: "var(--hrv)", walk: "var(--under)" };
const CAT_LABEL = { hiit: "Gym class", strength: "Strength", walk: "Walk" };
const CATS = ["hiit", "strength", "walk"];

const DAY_MS = 86400000;
const fmtTick = (t) =>
  new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });

const residPoints = (tr) =>
  tr.points.map((p) => ({ t: new Date(`${p.date}T12:00:00`).getTime(), resid: p.resid }));

/**
 * The trend as two endpoints, drawn through the residual centroid.
 *
 * Theil-Sen's own intercept is fitted on date ORDINALS, which are ~740,000 — carrying
 * it over to millisecond timestamps would put the line thousands of bpm off screen.
 * The slope is what the server reports and what matters; anchoring it at the mean
 * residual over the midpoint reproduces the same line in chart coordinates.
 */
function residFitLine(tr) {
  const pts = residPoints(tr);
  const ts = pts.map((p) => p.t);
  const t0 = Math.min(...ts);
  const t1 = Math.max(...ts);
  const tm = (t0 + t1) / 2;
  const mid = pts.reduce((a, p) => a + p.resid, 0) / pts.length;
  const perDay = tr.per_90d / 90;
  const at = (t) => mid + perDay * ((t - tm) / DAY_MS);
  return [{ t: t0, fit: at(t0) }, { t: t1, fit: at(t1) }];
}

function TrendVerdict({ trend }) {
  if (!trend) return null;
  const { per_90d, per_90d_lo, per_90d_hi, flat, direction, n } = trend;
  return (
    <div className="flex items-baseline gap-2">
      <span className="text-[10px] text-ink-2">{direction}</span>
      <span className="text-[9px] text-ink-3">
        {per_90d >= 0 ? "+" : ""}{per_90d} bpm/90d
        {" "}[{per_90d_lo >= 0 ? "+" : ""}{per_90d_lo}, {per_90d_hi >= 0 ? "+" : ""}{per_90d_hi}]
        {" "}· n={n}{flat ? " · interval spans zero" : ""}
      </span>
    </div>
  );
}

export default function HRRecoveryCard() {
  const [view, setView] = useState("effort");
  const { data } = useQuery({
    queryKey: ["hr-recovery", 365],
    queryFn: () => api.getHRRecovery(365),
    staleTime: 1000 * 60 * 30,
  });

  const sessions = data?.sessions ?? [];
  if (!sessions.length) {
    return (
      <Card>
        <Eyebrow left="HEART-RATE RECOVERY" right="NO DATA YET" />
        <HeroSub>
          Computed from the HR samples your watch records after a session ends. It will
          fill in once workouts have been recomputed.
        </HeroSub>
      </Card>
    );
  }

  const byCat = data?.by_category ?? {};
  const fits = data?.fits ?? {};
  const trends = data?.trends ?? {};
  const gym = byCat.hiit;
  const gymFit = fits.hiit;
  const gymTrend = trends.hiit;
  const present = CATS.filter((c) => byCat[c]);

  // Headline is the trend verdict, not the median: "am I improving" is the question
  // the median cannot answer and the one that was being asked.
  const heroValue = gymTrend
    ? (gymTrend.flat ? "flat" : `${gymTrend.per_90d > 0 ? "+" : ""}${gymTrend.per_90d}`)
    : gym ? `${gym.median}` : "—";
  const heroUnit = gymTrend
    ? (gymTrend.flat
        ? ["NO DETECTABLE TREND", "GYM CLASSES, PEAK-ADJUSTED"]
        : ["BPM PER 90 DAYS", "GYM CLASSES, PEAK-ADJUSTED"])
    : ["BPM DROP IN 60s", "GYM CLASSES, MEDIAN"];

  return (
    <Card>
      <Eyebrow left="HEART-RATE RECOVERY" right={`${data.n_total} SESSIONS`} />
      <Hero value={heroValue} unit={heroUnit} />
      <HeroSub>
        How fast heart rate falls after a session. Recovery scales strongly with how
        hard you went, so the reference is your own fit line at that peak — not a
        single number.
        {gym && <><Sep />gym classes run a median <K>{gym.median}</K>
          {gym.iqr_low != null && <> (IQR {gym.iqr_low}–{gym.iqr_high})</>}</>}
      </HeroSub>

      <SegmentedTabs
        className="mt-3"
        value={view}
        onChange={setView}
        options={[
          { value: "effort", label: "VS EFFORT" },
          { value: "time", label: "OVER TIME" },
        ]}
      />

      {view === "effort" ? (
        <div className="mt-3" style={{ width: "100%", height: 190 }}>
          <ResponsiveContainer>
            <ComposedChart margin={{ top: 6, right: 6, bottom: 6, left: 0 }}>
              <CartesianGrid stroke="var(--rule)" />
              <XAxis type="number" dataKey="hr_peak" name="peak HR"
                     domain={["dataMin - 5", "dataMax + 5"]}
                     tick={{ fontSize: 9, fill: "var(--ink-2)" }}
                     label={{ value: "peak HR", position: "insideBottom", offset: -4,
                              fontSize: 9, fill: "var(--ink-3)" }} />
              <YAxis type="number" tick={{ fontSize: 9, fill: "var(--ink-2)" }} width={30}
                     label={{ value: "bpm drop", angle: -90, position: "insideLeft",
                              fontSize: 9, fill: "var(--ink-3)" }} />
              <Tooltip
                contentStyle={{ background: "var(--card)", border: "1px solid var(--rule)",
                                borderRadius: 8, fontSize: 11 }}
                labelStyle={{ color: "var(--ink-2)" }}
                formatter={(v, n) => [Math.round(v), n === "hrr60" ? "bpm drop" : n]}
                labelFormatter={(l) => `peak ${Math.round(l)}`} />
              {/* Bounds first so points and the fit line draw over them. Two thin
                  dashed lines rather than a filled band: a range <Area> fed from a
                  per-series `data` prop renders an empty path in Recharts. */}
              {CATS.flatMap((c) => {
                const f = fits[c];
                if (!f) return [];
                return ["lo", "hi"].map((b) => (
                  <Line key={`b-${c}-${b}`} data={f.band} dataKey={b} type="linear"
                        stroke={`color-mix(in srgb, ${CAT_COLOR[c]} 45%, transparent)`}
                        strokeWidth={1} strokeDasharray="2 3" dot={false}
                        isAnimationActive={false} legendType="none" />
                ));
              })}
              {CATS.map((c) => {
                const f = fits[c];
                if (!f) return null;
                const line = [{ hr_peak: f.x0, fit: f.y0 }, { hr_peak: f.x1, fit: f.y1 }];
                return (
                  <Line key={`l-${c}`} data={line} dataKey="fit" type="linear"
                        stroke={CAT_COLOR[c]} strokeWidth={1.5} dot={false}
                        isAnimationActive={false} legendType="none"
                        strokeDasharray={f.flat ? "4 3" : undefined} />
                );
              })}
              {CATS.map((c) => {
                const pts = sessions.filter((s) => s.category === c);
                if (!pts.length) return null;
                return <Scatter key={c} name={CAT_LABEL[c]} data={pts} dataKey="hrr60"
                                fill={CAT_COLOR[c]} fillOpacity={0.65}
                                isAnimationActive={false} />;
              })}
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <div className="mt-3" style={{ width: "100%", height: 190 }}>
          <ResponsiveContainer>
            <ComposedChart margin={{ top: 6, right: 6, bottom: 6, left: 0 }}>
              <CartesianGrid stroke="var(--rule)" />
              <XAxis type="number" dataKey="t" domain={["dataMin", "dataMax"]}
                     tickFormatter={fmtTick} tick={{ fontSize: 9, fill: "var(--ink-2)" }} />
              <YAxis type="number" tick={{ fontSize: 9, fill: "var(--ink-2)" }} width={30}
                     label={{ value: "vs expected", angle: -90, position: "insideLeft",
                              fontSize: 9, fill: "var(--ink-3)" }} />
              <Tooltip
                contentStyle={{ background: "var(--card)", border: "1px solid var(--rule)",
                                borderRadius: 8, fontSize: 11 }}
                labelStyle={{ color: "var(--ink-2)" }}
                formatter={(v) => [`${v > 0 ? "+" : ""}${Math.round(v)} bpm`, "vs expected"]}
                labelFormatter={(l) => fmtTick(l)} />
              {/* Zero = exactly your own peak-adjusted expectation. */}
              <ReferenceLine y={0} stroke="var(--ink-3)" strokeDasharray="3 3" />
              {/* Scatter and trend line are emitted in two separate passes rather
                  than one map returning a pair: Recharts inspects its children to
                  build series, and does not reliably descend into nested arrays. */}
              {CATS.map((c) => {
                const tr = trends[c];
                if (!tr) return null;
                return (
                  <Scatter key={`s-${c}`} name={CAT_LABEL[c]} data={residPoints(tr)}
                           dataKey="resid" fill={CAT_COLOR[c]} fillOpacity={0.6}
                           isAnimationActive={false} />
                );
              })}
              {CATS.map((c) => {
                const tr = trends[c];
                if (!tr) return null;
                return (
                  <Line key={`l-${c}`} data={residFitLine(tr)} dataKey="fit" type="linear"
                        stroke={CAT_COLOR[c]} strokeWidth={1.5} dot={false}
                        isAnimationActive={false} legendType="none"
                        strokeDasharray={tr.flat ? "4 3" : undefined} />
                );
              })}
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}

      <Legend items={present.map((c) => ({
        label: CAT_LABEL[c], color: CAT_COLOR[c],
        note: view === "effort"
          ? (fits[c]
              ? `slope ${fits[c].slope > 0 ? "+" : ""}${fits[c].slope.toFixed(2)} bpm/bpm · n=${fits[c].n}`
              : `median ${byCat[c].median} · n=${byCat[c].n}`)
          : (trends[c]
              ? `${trends[c].per_90d > 0 ? "+" : ""}${trends[c].per_90d} bpm/90d · n=${trends[c].n}`
              : `n=${byCat[c].n} — too few to fit`),
      }))} />

      {view === "time" && CATS.some((c) => trends[c]) && (
        <div className="mt-2 space-y-1 border-t border-rule pt-2">
          {CATS.filter((c) => trends[c]).map((c) => (
            <div key={c} className="flex items-baseline gap-2">
              <span className="w-[68px] shrink-0 text-[9px] tracking-micro text-ink-3">
                {CAT_LABEL[c].toUpperCase()}
              </span>
              <TrendVerdict trend={trends[c]} />
            </div>
          ))}
        </div>
      )}

      <Foot>
        {view === "effort" ? (
          <>Higher is better, and the fit line is your own expectation at that peak — a
            point above it recovered better than you usually do from that intensity.
            The band is the 95% interval on the slope; a dashed line means it spans
            zero, so the relationship is not established for that type.</>
        ) : (
          <>Zero is your own peak-adjusted expectation, so this asks whether recovery
            is improving independently of how hard the sessions were. A dashed trend
            line means the interval spans zero — no change detectable yet, which with
            this much session-to-session spread needs many months to resolve.</>
        )}
        {" "}Real-world HRR is far noisier than the clinical version, which follows a
        standardised maximal test with a controlled cooldown rather than a variable
        class — so no clinical cutoff is drawn here. Needs the watch worn through
        cooldown; about one session in six has no reading.
      </Foot>
    </Card>
  );
}
