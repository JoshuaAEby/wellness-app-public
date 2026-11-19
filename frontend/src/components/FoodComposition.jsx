import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  PieChart, Pie, Cell, BarChart, Bar, XAxis, YAxis,
  CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import { api } from "../api.js";
import { Card, Eyebrow, HeroSub, K, Sep, Foot, Legend, SegmentedTabs } from "./ui.jsx";

/**
 * What the diet is made of, and whether it has quietly changed.
 *
 * Both views read food-group tags attached to the ITEM (services/food_tags.py),
 * not keywords matched against each entry's text. That distinction is the whole
 * reason these cards can be trusted: keyword coverage of leafy greens appeared to
 * fall 77% -> 30% in June 2026 purely because saved-item reuse rose from 35% to
 * 60% of entries, and a reused item carries a canonical name rather than an
 * ingredient list. Freshly-typed entries were flat over the same period and fibre
 * per day was at its series high in August. An item tag can be wrong, but it is
 * wrong identically every time that food appears, so it shifts the baseline
 * instead of manufacturing drift.
 *
 * COLOUR: the palette and its order are validated (see --food-* in index.css).
 * Render order must stay the backend's COMPONENT_ORDER — that sequence is what
 * clears adjacent-pair colour-vision separation.
 *
 * Each item is DECOMPOSED into calorie shares rather than given one label. The
 * single-label version put 71% of calories in a `mixed_dish` catch-all that could
 * never move, and left vegetables eaten inside a burrito uncounted.
 */

const COLORS = {
  dairy: "var(--food-dairy)",
  grain: "var(--food-grain)",
  vegetable: "var(--food-veg)",
  added_fat: "var(--food-fat)",
  fruit: "var(--food-fruit)",
  plant_protein: "var(--food-plantprotein)",
  added_sugar: "var(--food-sugar)",
  animal_protein: "var(--food-animalprotein)",
  other: "var(--food-other)",
};

const TIP = {
  contentStyle: {
    background: "var(--canvas)",
    border: "1px solid var(--rule)",
    borderRadius: 8,
    fontSize: 11,
  },
  labelStyle: { color: "var(--ink-3)", fontSize: 11 },
};

function Snapshot({ days }) {
  // Calorie-weighted only. An entry no longer HAS a single category to be counted
  // under — it contributes to several components at once — so an entry-share
  // reading would be meaningless here.
  const { data, isLoading } = useQuery({
    queryKey: ["foodComposition", days],
    queryFn: () => api.getFoodComposition(days),
    staleTime: 1000 * 60 * 30,
  });

  if (isLoading) return <p className="py-8 text-center text-[11px] text-ink-3">loading…</p>;

  const slices = (data?.slices ?? []).filter((s) => s.kcal_pct != null);
  if (!slices.length) {
    return (
      <HeroSub>
        Nothing decomposed yet in this window. Items are tagged nightly; a manual run
        is available at <K>POST /api/nutrition/tags/backfill</K>.
      </HeroSub>
    );
  }

  const rows = slices.map((s) => ({ ...s, value: s.kcal_pct }));
  const top = [...rows].sort((a, b) => b.value - a.value)[0];
  const partial = data.tagged_entry_pct != null && data.tagged_entry_pct < 99;

  return (
    <>
      <div className="relative mt-3 h-52">
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Pie
              data={rows}
              dataKey="value"
              nameKey="label"
              innerRadius="58%"
              outerRadius="86%"
              startAngle={90}
              endAngle={-270}
              paddingAngle={2}
              stroke="none"
              isAnimationActive={false}
            >
              {rows.map((r) => (
                <Cell key={r.key} fill={COLORS[r.key] ?? "var(--food-other)"} />
              ))}
            </Pie>
            <Tooltip
              {...TIP}
              formatter={(v, _n, p) => [
                `${v}% · ${p.payload.kcal.toLocaleString()} kcal`,
                p.payload.label,
              ]}
            />
          </PieChart>
        </ResponsiveContainer>

        {/* The donut's own hero: the single largest share, named. A ring of eight
            slices otherwise makes the reader do the ranking by eye.
            Sized to the HOLE, not to the card — the shared <Hero> is tuned for a
            full-width card and its digits spill over the ring at this diameter. */}
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
          <span className="text-[25px] font-semibold leading-none tabular-nums"
            style={{ color: COLORS[top.key] }}>
            {Math.round(top.value)}<span className="text-[13px]">%</span>
          </span>
          <span className="prose-ui mt-1 max-w-[92px] text-center text-[9.5px] leading-tight text-ink-3">
            {top.label}
          </span>
        </div>
      </div>

      <Legend
        items={rows.map((r) => ({
          label: r.label,
          color: COLORS[r.key] ?? "var(--food-other)",
          note: ` ${r.value}%`,
        }))}
      />

      <Foot>
        Every meal is split into its parts, so a burrito puts calories into grain,
        meat, beans, cheese <i>and</i> vegetables at once — this counts what you ate,
        not what it was served as. Shares are{" "}
        <b className="font-semibold text-ink-2">estimated per item</b>, and by
        calories, where fat weighs more than it looks (9 kcal/g against 4).
        Alcohol and drinks sit in “other”.
        <br />
        Last <K>{data.days}</K> days<Sep />
        <K>{data.entries_tagged.toLocaleString()}</K> decomposed entries
        {partial && (
          <>
            <Sep />
            <span style={{ color: "var(--ink-2)" }}>
              {data.tagged_entry_pct}% of entries classified — the rest are excluded
              rather than lumped into “other”.
            </span>
          </>
        )}
      </Foot>
    </>
  );
}

function Trend({ weeks }) {
  const { data, isLoading } = useQuery({
    queryKey: ["foodCompositionTrend", weeks],
    queryFn: () => api.getFoodCompositionTrend(weeks),
    staleTime: 1000 * 60 * 30,
  });

  if (isLoading) return <p className="py-8 text-center text-[11px] text-ink-3">loading…</p>;

  const rows = data?.weeks ?? [];
  const order = data?.order ?? [];
  const labels = data?.labels ?? {};
  if (rows.length < 2) {
    return <HeroSub>Not enough logged weeks yet to show a trend.</HeroSub>;
  }

  // Shares, not totals: weeks differ in how many days were logged, and an
  // under-logged week would otherwise read as a change in what was eaten.
  const present = order.filter((k) => rows.some((r) => (r[k] ?? 0) > 0));

  return (
    <>
      <div className="mt-4 h-52">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={rows} margin={{ top: 4, right: 4, bottom: 0, left: 0 }} barCategoryGap="18%">
            <CartesianGrid stroke="var(--rule)" strokeDasharray="3 3" vertical={false} />
            <XAxis
              dataKey="week"
              tick={{ fill: "var(--ink-3)", fontSize: 10 }}
              tickFormatter={(w) => w.slice(5)}
              minTickGap={26}
            />
            {/* Explicit ticks: per-category shares are rounded to 1 dp, so a stack
                can total 100.1 and Recharts would otherwise label the top tick
                "100.10000000000001". */}
            <YAxis
              tick={{ fill: "var(--ink-3)", fontSize: 10 }}
              domain={[0, 100]}
              ticks={[0, 25, 50, 75, 100]}
              allowDataOverflow
              width={30}
              tickFormatter={(v) => `${v}`}
            />
            <Tooltip
              {...TIP}
              formatter={(v, n) => [`${v}%`, labels[n] ?? n]}
              labelFormatter={(w) => `week of ${w}`}
            />
            {present.map((k) => (
              <Bar
                key={k}
                dataKey={k}
                stackId="a"
                fill={COLORS[k] ?? "var(--food-other)"}
                stroke="var(--card)"
                strokeWidth={1}
                isAnimationActive={false}
              />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>

      <Legend
        items={present.map((k) => ({ label: labels[k] ?? k, color: COLORS[k] ?? "var(--food-other)" }))}
      />

      <Foot>
        Share of each week’s calories, by category. Weeks with fewer than three logged
        days are omitted — a thinly-logged week says more about logging than about eating.
      </Foot>
    </>
  );
}

export default function FoodComposition({ days = 90, weeks = 16 }) {
  const [view, setView] = useState("now");
  return (
    <Card>
      <Eyebrow left="WHAT THE DIET IS MADE OF" right={view === "now" ? `${days}D` : `${weeks}W`} />
      <SegmentedTabs
        className="mt-2"
        value={view}
        onChange={setView}
        options={[
          { value: "now", label: "COMPOSITION" },
          { value: "trend", label: "OVER TIME" },
        ]}
      />
      {view === "now" ? <Snapshot days={days} /> : <Trend weeks={weeks} />}
    </Card>
  );
}
