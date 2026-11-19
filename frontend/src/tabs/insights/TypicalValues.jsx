import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api.js";
import { windowDates } from "../../utils.js";
import TimeWindowSelector from "../../components/TimeWindowSelector.jsx";
import { Card, Eyebrow, Foot } from "../../components/ui.jsx";

/**
 * TYPICAL (note 86, 2026-10-26): "how much protein do I typically take in?" for every
 * daily variable — mean ± SD, median [IQR], and how many days it rests on.
 *
 * The backend (services/typical_values) uses the correlation engine's frame, so a
 * "typical" day here is the same normal day the findings use: travel/sick days out,
 * nutrition only on complete-log days. When the mean misleads (≥25% zeros, or a long
 * tail — alcohol, hard minutes, screen time) the MEDIAN is shown first and the row
 * says so; SD on a zero-heavy series describes almost no actual day.
 */

const fmtNum = (v) => {
  if (v == null || Number.isNaN(v)) return "–";
  if (v === 0) return "0";
  const a = Math.abs(v);
  if (a >= 1000) return Math.round(v).toLocaleString();
  if (a >= 100) return v.toFixed(0);
  if (a >= 10) return v.toFixed(1);
  return v.toFixed(2);
};
// Clock hours; bedtimes after midnight arrive as 24+ h.
const fmtClock = (h) => {
  const m = Math.round((((h % 24) + 24) % 24) * 60);
  return `${String(Math.floor(m / 60) % 24).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
};

function Row({ it }) {
  let main, sub;
  if (it.kind === "binary") {
    main = `${it.pct.toFixed(0)}% of days`;
    sub = null;
  } else if (it.kind === "clock") {
    main = `${fmtClock(it.mean)} ± ${Math.round(it.sd * 60)} min`;
    sub = `median ${fmtClock(it.median)} · IQR ${fmtClock(it.p25)}–${fmtClock(it.p75)}`;
  } else if (it.prefer_median) {
    main = `${fmtNum(it.median)} median`;
    sub = `IQR ${fmtNum(it.p25)} to ${fmtNum(it.p75)} · mean ${fmtNum(it.mean)} ± ${fmtNum(it.sd)}`
      + (it.zero_share >= 0.25 ? ` · zero on ${Math.round(it.zero_share * 100)}% of days` : "");
  } else {
    main = `${fmtNum(it.mean)} ± ${fmtNum(it.sd)}`;
    sub = `median ${fmtNum(it.median)} · IQR ${fmtNum(it.p25)} to ${fmtNum(it.p75)}`;
  }
  return (
    <div className={`flex items-baseline gap-2 border-t border-rule py-1.5 first:border-t-0`}>
      <div className="min-w-0 flex-1">
        <div className="truncate text-[12px] text-ink">{it.label}</div>
        {sub && <div className="text-[10px] leading-snug text-ink-3">{sub}</div>}
      </div>
      <div className="flex-none text-right">
        <div className="text-[12px] font-semibold tabular-nums text-ink">{main}</div>
        <div className="text-[9.5px] text-ink-3 tabular-nums">{it.n} days</div>
      </div>
    </div>
  );
}

export default function TypicalValues({ embedded = false }) {
  const [win, setWin] = useState("3M");
  const [q, setQ] = useState("");
  const { start, end } = windowDates(win);
  const { data, isLoading } = useQuery({
    queryKey: ["typical", start, end],
    queryFn: () => api.getTypicalValues(start, end),
    staleTime: 10 * 60 * 1000,
  });

  const groups = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (data?.groups ?? [])
      .map((g) => ({ ...g, items: needle
        ? g.items.filter((it) => it.label.toLowerCase().includes(needle) || g.group.toLowerCase().includes(needle))
        : g.items }))
      .filter((g) => g.items.length);
  }, [data, q]);

  return (
    <div className={embedded ? "space-y-3" : "space-y-3 p-4"}>
      <TimeWindowSelector value={win} onChange={setWin} />
      <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search — protein, sleep, steps…"
             className="w-full rounded-lg bg-surface px-3 py-2 text-[13px] text-ink outline-none placeholder:text-ink-3" />
      {data && (
        <p className="px-1 text-[10.5px] leading-snug text-ink-3">
          {data.included_days} normal days of {data.window_days} ({data.start} → {data.end}). Travel, sick and
          other flagged days are left out, nutrition counts only fully-logged days, and today isn't
          included yet. Each row says how many days it rests on.
        </p>
      )}
      {isLoading && <p className="px-1 text-[12px] text-ink-3">Loading…</p>}
      {groups.map((g) => (
        <Card key={g.group}>
          <Eyebrow left={g.group.toUpperCase()} right={`${g.items.length}`} />
          <div className="mt-1">
            {g.items.map((it) => <Row key={it.key} it={it} />)}
          </div>
        </Card>
      ))}
      {data && !groups.length && (
        <p className="px-1 text-[12px] text-ink-3">Nothing matches “{q}”.</p>
      )}
      {data && (
        <Foot>
          ± is one standard deviation: about two-thirds of days fall within it, for roughly
          symmetric variables. IQR is the middle half of days (25th to 75th percentile). Where the median is shown first, the variable is lopsided (many
          zero days or a long tail) and the median is the more honest “typical”. Variables with
          fewer than {data.min_days} days in the window are hidden.
        </Foot>
      )}
    </div>
  );
}
