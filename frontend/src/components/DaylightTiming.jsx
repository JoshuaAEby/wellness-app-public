import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api.js";
import { Card, Eyebrow, Hero, HeroSub, K, Sep, Foot } from "./ui.jsx";

/**
 * WHEN daylight happened, not just how much.
 *
 * The dose has always been visible (total minutes/day). The timing was sitting in the
 * same rows the whole time — `time_in_daylight` arrives at minute resolution — and was
 * simply never read. It matters because the circadian system responds to timing:
 * 60 minutes at 08:00 and 60 minutes at 18:00 are an identical daily total and a very
 * different signal.
 *
 * Derived on read from the raw minutes, so the full history is covered with no
 * migration and no backfill job.
 */

function hhmm(h) {
  if (h == null) return "—";
  const hr = Math.floor(h);
  const mn = Math.round((h - hr) * 60);
  return `${String(hr).padStart(2, "0")}:${String(mn).padStart(2, "0")}`;
}
function median(xs) {
  const v = xs.filter((x) => x != null).sort((a, b) => a - b);
  if (!v.length) return null;
  const m = Math.floor(v.length / 2);
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}

export default function DaylightTiming({ start, end }) {
  const { data } = useQuery({
    queryKey: ["daylight-timing", start, end],
    queryFn: () => api.getDaylightTiming(start, end),
    staleTime: 1000 * 60 * 30,
  });

  const days = data?.days ?? [];
  const cutoff = data?.morning_cutoff_hour ?? 10;

  const stats = useMemo(() => {
    const withLight = days.filter((d) => d.first_light_hour != null);
    return {
      n: withLight.length,
      medFirst: median(withLight.map((d) => d.first_light_hour)),
      medMorning: median(days.map((d) => d.morning_light_min)),
      // Days where the first daylight of the day arrives after the morning window has
      // already closed — no morning light at all, which is the circadially costly case.
      noMorning: days.filter((d) => (d.morning_light_min ?? 0) < 1).length,
      total: days.length,
    };
  }, [days]);

  if (!days.length) return null;

  // Recent strip: one column per day, bar height = morning minutes, dot = first light.
  const recent = days.slice(-21);
  const maxMorning = Math.max(...recent.map((d) => d.morning_light_min || 0), 1);

  return (
    <Card>
      <Eyebrow left="DAYLIGHT TIMING" right={`${stats.n}/${stats.total} DAYS`} />
      <Hero value={hhmm(stats.medFirst)} unit={["TYPICAL FIRST LIGHT"]} />
      <HeroSub>
        Median <K>{Math.round(stats.medMorning ?? 0)}</K> min before {hhmm(cutoff)}
        <Sep />
        <K>{stats.noMorning}</K> of {stats.total} days had none at all
      </HeroSub>

      <div className="mt-3">
        <p className="text-[9px] tracking-micro text-ink-3">
          LAST {recent.length} DAYS · BAR = MORNING MIN · DOT = FIRST LIGHT
        </p>
        <div className="mt-2 flex h-[64px] items-end gap-[3px]">
          {recent.map((d) => {
            const h = ((d.morning_light_min || 0) / maxMorning) * 100;
            // First light mapped across a 05:00–20:00 window, drawn top-down: a dot
            // high in the column means an early start.
            const fl = d.first_light_hour;
            const dotTop = fl == null ? null
              : Math.max(0, Math.min(100, ((fl - 5) / 15) * 100));
            return (
              <div key={d.date} className="relative flex-1" style={{ height: "100%" }}
                   title={`${d.date} · first ${hhmm(fl)} · ${Math.round(d.morning_light_min)} min before ${hhmm(cutoff)}`}>
                <div className="absolute bottom-0 w-full rounded-[1px]"
                     style={{ height: `${h}%`, background: "var(--breakfast, #f59e0b)", opacity: 0.55 }} />
                {dotTop != null && (
                  <div className="absolute h-[3px] w-full rounded-full"
                       style={{ top: `${dotTop}%`, background: "var(--hrv)" }} />
                )}
              </div>
            );
          })}
        </div>
        <div className="mt-1 flex justify-between text-[8.5px] text-ink-3">
          <span>early ↑</span><span>late ↓</span>
        </div>
      </div>

      <Foot>
        Derived from minute-level daylight data, so it covers the whole history without
        any new logging. Nothing here clears multiplicity correction yet — the strongest
        link is morning light against resting-HR z at ρ≈−0.18, which is suggestive at
        best and not something to act on.
      </Foot>
    </Card>
  );
}
