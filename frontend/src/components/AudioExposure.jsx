import { useQuery } from "@tanstack/react-query";
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ReferenceLine } from "recharts";
import { api } from "../api.js";
import { Card, Eyebrow, Hero, HeroSub, K, Sep, Foot, Legend } from "./ui.jsx";

/**
 * Noise-dose baseline — a slow variable, shown the way slow variables should be.
 *
 * Expressed as a percentage of the NIOSH recommended DAILY limit (85 dBA for 8 h, 3 dB
 * exchange rate), because hearing damage is level x time and irreversible: a mean dB
 * would treat thirty seconds of a siren and four hours of a loud bar as the same thing.
 *
 * Environmental and headphone are plotted separately on purpose — headphone exposure is
 * the one almost entirely under your control, and averaging them together would hide it.
 *
 * The point of this card is the DRIFT verdict, not the current week. It exists to answer
 * "has my acoustic environment changed?" a year from now.
 */

const COLORS = { environmental: "var(--under)", headphone: "var(--over)" };

function DriftLine({ label, drift }) {
  if (!drift) return null;
  if (drift.status === "accruing") {
    return (
      <p className="text-[10.5px] leading-snug text-ink-3">
        {label}: <K>{drift.weeks}</K> of {drift.need} weeks needed before a trend can be read.
      </p>
    );
  }
  const rising = drift.status === "rising";
  return (
    <p className="text-[10.5px] leading-snug" style={{ color: rising ? "var(--over)" : "var(--ink-2)" }}>
      {label}: {drift.status === "flat" ? "steady" : drift.status}
      {drift.change_pct != null && drift.status !== "flat" && (
        <> <b className="font-semibold">{drift.change_pct > 0 ? "+" : ""}{drift.change_pct}%</b></>
      )}
      <Sep />recent {drift.recent}% vs {drift.prior}% of the daily limit
    </p>
  );
}

export default function AudioExposure() {
  const { data } = useQuery({
    queryKey: ["audio-exposure", 52],
    queryFn: () => api.getAudioExposure(52),
    staleTime: 1000 * 60 * 60,
  });

  const weeks = data?.weeks ?? [];
  if (!weeks.length) {
    return (
      <Card>
        <Eyebrow left="NOISE EXPOSURE" right="NO DATA YET" />
        <HeroSub>
          Apple records environmental and headphone audio levels continuously. Nothing has
          arrived since ingestion was switched on — it will begin filling in after the next
          Health sync.
        </HeroSub>
      </Card>
    );
  }

  const rows = weeks.map((w) => ({
    label: w.week.slice(5),
    environmental: w.environmental?.mean_dose_pct ?? null,
    headphone: w.headphone?.mean_dose_pct ?? null,
  }));
  const latest = weeks[weeks.length - 1];
  const overDays = weeks.reduce(
    (a, w) => a + (w.environmental?.days_over_limit ?? 0) + (w.headphone?.days_over_limit ?? 0), 0);
  const rising = ["environmental", "headphone"].filter((k) => data?.drift?.[k]?.status === "rising");

  return (
    <Card>
      <Eyebrow left="NOISE EXPOSURE" right={`${weeks.length} WEEKS`} />
      <Hero value={`${Math.round(latest?.environmental?.mean_dose_pct ?? 0)}%`}
            unit={["OF THE DAILY LIMIT", "THIS WEEK, ENVIRONMENTAL"]} />
      <HeroSub>
        {rising.length
          ? <>Exposure is <b style={{ color: "var(--over)" }}>rising</b> for {rising.join(" and ")} —
             the trend, not this week, is the thing to look at.</>
          : <>Tracked as a baseline. The number to watch is whether the line drifts upward
             over months, not where it sits today.</>}
        {overDays > 0 && <><Sep /><K>{overDays}</K> day{overDays === 1 ? "" : "s"} at or over the limit</>}
      </HeroSub>

      <div className="mt-3" style={{ width: "100%", height: 140 }}>
        <ResponsiveContainer>
          <LineChart data={rows} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
            <CartesianGrid stroke="var(--rule)" vertical={false} />
            <XAxis dataKey="label" tick={{ fontSize: 8.5, fill: "var(--ink-3)" }} interval="preserveStartEnd" />
            <YAxis tick={{ fontSize: 9, fill: "var(--ink-2)" }} width={30}
                   tickFormatter={(v) => `${v}%`} />
            <Tooltip contentStyle={{ background: "var(--card)", border: "1px solid var(--rule)", borderRadius: 8, fontSize: 11 }}
                     labelStyle={{ color: "var(--ink-2)" }}
                     formatter={(v, n) => [`${v}% of daily limit`, n]} />
            {/* 100% = the NIOSH recommended daily dose, not a danger cliff. */}
            <ReferenceLine y={100} stroke="var(--over)" strokeDasharray="3 3" />
            <Line type="monotone" dataKey="environmental" stroke={COLORS.environmental}
                  strokeWidth={1.8} dot={false} connectNulls isAnimationActive={false} />
            <Line type="monotone" dataKey="headphone" stroke={COLORS.headphone}
                  strokeWidth={1.8} dot={false} connectNulls isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <Legend items={[
        { label: "Environmental", color: COLORS.environmental },
        { label: "Headphone", color: COLORS.headphone, note: "the one you control" },
      ]} />

      <div className="mt-3 space-y-1 border-t border-rule pt-2">
        <DriftLine label="Environmental" drift={data?.drift?.environmental} />
        <DriftLine label="Headphone" drift={data?.drift?.headphone} />
      </div>

      <Foot>
        100% is the NIOSH recommended daily dose — 85 dBA for 8 h, halving per +3 dB — not
        a cliff edge. A watch microphone is not a dosimeter: readings are approximate,
        sampling is intermittent, and a phone in a bag under-reads. Read the direction,
        not the decimal.
      </Foot>
    </Card>
  );
}
