import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api.js";
import { windowDates, fmtMinutes } from "../utils.js";
import { Card, Eyebrow, Hero, HeroSub, K, Sep, Foot, Legend, Histogram } from "../components/ui.jsx";
import TimeWindowSelector from "../components/TimeWindowSelector.jsx";
import DaylightTiming from "../components/DaylightTiming.jsx";
import {
  clockSuspect, clockUsable, durationUsable, durationSuspect,
  spanMinutes, localMinutes, clockToAxis, axisToLabel, quantile,
} from "../lib/sleep.js";

/**
 * Sleep, split out of Recovery into its own view.
 *
 * Recovery had four sleep sections (averages, stages, consistency, schedule) on top
 * of ten vitals sections — long enough that the sleep half was never read. These are
 * one coherent subject and now get their own view.
 *
 * The headline change is that schedule is shown as a DISTRIBUTION and a stack on a
 * shared clock axis, not as two more time series. Regularity is a shape: a straight
 * left edge is a consistent bedtime, a ragged one is not, and the spread of the
 * distribution is the number that a "consistency score" was compressing away.
 *
 * DATA GUARD (see lib/sleep.js): Apple merges afternoon naps into the night's
 * session, so ~5 nights a year carry a wake time 8–14 h late. Those are dropped from
 * CLOCK views only — their `total_min` is still correct (excluding them moves the
 * yearly mean by 0.3 min), so duration views keep them.
 */

const STAGES = [
  { key: "deep_min",  label: "Deep",  color: "var(--deep)" },
  { key: "core_min",  label: "Core",  color: "var(--core)" },
  { key: "rem_min",   label: "REM",   color: "var(--rem)" },
  { key: "awake_min", label: "Awake", color: "var(--awake)" },
];

const ORIGIN_H = 18;          // clock axis starts at 18:00
const AXIS_SPAN = 18 * 60;    // …and runs 18 hours, to 12:00

/** Distribution strip: every night as a tick, with the interquartile box + median. */
function ClockDistribution({ values, color, label, height = 42 }) {
  const W = 320, X0 = 8, X1 = W - 8;
  if (!values.length) return null;
  const x = (v) => X0 + (Math.max(0, Math.min(AXIS_SPAN, v)) / AXIS_SPAN) * (X1 - X0);
  const q1 = quantile(values, 0.25), med = quantile(values, 0.5), q3 = quantile(values, 0.75);
  const spread = q3 - q1;
  return (
    <div>
      <div className="flex items-baseline justify-between">
        <span className="text-[9px] tracking-micro text-ink-3">{label}</span>
        <span className="text-[9.5px] text-ink-2">
          median <b className="font-semibold text-ink">{axisToLabel(med, ORIGIN_H)}</b>
          <span className="text-ink-3"> · middle half spans {Math.round(spread)} min</span>
        </span>
      </div>
      <svg viewBox={`0 0 ${W} ${height}`} className="mt-1 block w-full" style={{ height }}>
        {/* interquartile box — where half your nights land */}
        <rect x={x(q1)} y={6} width={Math.max(x(q3) - x(q1), 1)} height={height - 20}
          rx="3" fill={color} opacity="0.16" />
        {values.map((v, i) => (
          <line key={i} x1={x(v)} y1={8} x2={x(v)} y2={height - 14}
            stroke={color} strokeWidth="1" opacity="0.5" />
        ))}
        <line x1={x(med)} y1={4} x2={x(med)} y2={height - 12} stroke="var(--ink)" strokeWidth="1.6" />
        {/* hour ticks */}
        {Array.from({ length: 7 }, (_, i) => i * 3 * 60).map((t) => (
          <text key={t} x={x(t)} y={height - 2} textAnchor="middle" fontSize="7.5" fill="var(--ink-3)">
            {axisToLabel(t, ORIGIN_H)}
          </text>
        ))}
      </svg>
    </div>
  );
}

export default function Sleep({ window = "3M", onWindowChange, windowOffset = 0, onWindowOffsetChange }) {
  const { start, end } = windowDates(window, windowOffset);
  const { data: sessions = [], isLoading } = useQuery({
    queryKey: ["sleep", start, end],
    queryFn: () => api.sleep(start, end),
  });
  const { data: settings } = useQuery({ queryKey: ["settings"], queryFn: api.getSettings });

  const [schedPage, setSchedPage] = useState(0);   // 0 = most recent 14 nights
  const targetMin = Math.round((settings?.target_sleep_hours ?? 7.5) * 60);

  const { clock, dur, excluded, beds, wakes, durs, last } = useMemo(() => {
    const clock = clockUsable(sessions);
    const dur = durationUsable(sessions);
    const excluded = sessions.filter((s) => clockSuspect(s) != null);
    return {
      clock, dur, excluded,
      beds:  clock.map((s) => clockToAxis(localMinutes(s.bedtime, s.bed_offset_min), ORIGIN_H)).filter((v) => v != null),
      wakes: clock.map((s) => clockToAxis(localMinutes(s.wake_time, s.wake_offset_min), ORIGIN_H)).filter((v) => v != null),
      durs:  dur.map((s) => s.total_min).filter((v) => v != null),
      last:  sessions[sessions.length - 1] ?? null,
    };
  }, [sessions]);

  // Early returns must still render the window control — otherwise paging back to
  // an empty period leaves you with no way to page forward again.
  const shell = (msg) => (
    <div className="space-y-4 p-4">
      <TimeWindowSelector value={window} onChange={onWindowChange}
        offset={windowOffset} onOffsetChange={onWindowOffsetChange} />
      <p className="text-[11px] text-ink-3">{msg}</p>
    </div>
  );
  if (isLoading) return shell("Loading…");
  if (!sessions.length) return shell("No sleep data in this window.");

  const avg = durs.length ? Math.round(durs.reduce((a, b) => a + b, 0) / durs.length) : null;
  const med = quantile(durs, 0.5);
  const short = durs.filter((v) => v < 6.5 * 60).length;

  // 14-night stack on the shared clock axis, pageable backwards. 14 rows is about
  // what stays legible at this height; older nights are reached by paging rather
  // than by squeezing more rows in.
  const PAGE = 14;
  const pages = Math.max(1, Math.ceil(clock.length / PAGE));
  const page = Math.min(schedPage, pages - 1);           // clamp if the window shrank
  const hiIdx = clock.length - page * PAGE;
  const recent = clock.slice(Math.max(0, hiIdx - PAGE), hiIdx);
  const W = 320, SX0 = 62, SX1 = W - 8, ROW = 15;
  const sx = (v) => SX0 + (Math.max(0, Math.min(AXIS_SPAN, v)) / AXIS_SPAN) * (SX1 - SX0);
  const medBed = beds.length ? quantile(beds, 0.5) : null;

  const lastStages = last ? STAGES.map((st) => ({ ...st, min: last[st.key] ?? 0 })) : [];
  const lastTot = lastStages.reduce((a, s) => a + s.min, 0) || 1;

  return (
    <div className="space-y-4 p-4">
      <TimeWindowSelector value={window} onChange={onWindowChange}
        offset={windowOffset} onOffsetChange={onWindowOffsetChange} />

      {/* ── Last night ─────────────────────────────────────────────────────── */}
      {last && (
        <Card>
          <Eyebrow left="LAST NIGHT" right={last.date} />
          {/* "Short" needs a tolerance. Against a 7h30 target, 7h27 is not a bad
              night, and colouring a 3-minute miss red trains you to ignore the
              colour. 30 min is roughly the night-to-night noise in this series. */}
          {(() => {
            const shortBy = targetMin - (last.total_min ?? 0);
            const meaningful = shortBy > 30;
            return (
              <Hero value={fmtMinutes(last.total_min)}
                unit={["ASLEEP", meaningful ? `${fmtMinutes(shortBy)} SHORT`
                       : shortBy > 0 ? "ON TARGET" : "OVER TARGET"]}
                color={meaningful ? "var(--over)" : undefined} />
            );
          })()}
          <HeroSub>
            {clockSuspect(last)
              ? <>Clock times unusable for this night — <K>{clockSuspect(last)}</K>. Duration is still valid.</>
              : <>Asleep <K>{axisToLabel(clockToAxis(localMinutes(last.bedtime, last.bed_offset_min), ORIGIN_H), ORIGIN_H)}</K>,
                  up <K>{axisToLabel(clockToAxis(localMinutes(last.wake_time, last.wake_offset_min), ORIGIN_H), ORIGIN_H)}</K></>}
          </HeroSub>
          {/* Stage split is a COMPOSITION, not a timeline — Apple exports nightly
              totals only, so placing stages on a clock would invent an order. */}
          <div className="mt-3 flex h-[20px] gap-[2px] overflow-hidden rounded-md">
            {lastStages.filter((s) => s.min > 0).map((s) => {
              const pct = (s.min / lastTot) * 100;
              return (
                <div key={s.key} className="flex items-center justify-center overflow-hidden"
                  style={{ flex: s.min, backgroundColor: s.color }} title={`${s.label} ${fmtMinutes(s.min)}`}>
                  {pct >= 14 && (
                    <span className="truncate px-1 text-[9px] font-medium leading-none text-ink">
                      {s.label} {fmtMinutes(s.min)}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
          <Foot>Stage totals, not sequence — Apple exports nightly totals only.</Foot>
        </Card>
      )}

      {/* Circadian light: when it lands, not just how much. */}
      <DaylightTiming start={start} end={end} />

      {/* ── Schedule: distributions + the stack ────────────────────────────── */}
      <Card>
        <Eyebrow left="SCHEDULE" right={`${clock.length} NIGHTS`} />
        <div className="mt-3 space-y-3">
          <ClockDistribution values={beds} color="var(--core)" label="BEDTIME" />
          <ClockDistribution values={wakes} color="var(--rem)" label="WAKE" />
        </div>

        {recent.length > 2 && (
          <>
            <div className="mt-4 flex items-center justify-between">
              <p className="text-[9px] tracking-micro text-ink-3">
                {page === 0 ? `LAST ${recent.length} NIGHTS`
                            : `${recent[0]?.date} – ${recent[recent.length - 1]?.date}`}
              </p>
              <div className="flex items-center gap-1">
                <button
                  onClick={() => setSchedPage((p) => Math.min(pages - 1, p + 1))}
                  disabled={page >= pages - 1}
                  className="rounded px-1.5 py-0.5 text-[11px] text-ink-3 enabled:hover:text-ink disabled:opacity-30"
                  aria-label="Earlier nights">◀</button>
                <span className="text-[9px] tabular-nums text-ink-3">{page + 1}/{pages}</span>
                <button
                  onClick={() => setSchedPage((p) => Math.max(0, p - 1))}
                  disabled={page === 0}
                  className="rounded px-1.5 py-0.5 text-[11px] text-ink-3 enabled:hover:text-ink disabled:opacity-30"
                  aria-label="Later nights">▶</button>
              </div>
            </div>
            <svg viewBox={`0 0 ${W} ${recent.length * ROW + 16}`} className="mt-1 block w-full">
              {medBed != null && (
                <line x1={sx(medBed)} y1={0} x2={sx(medBed)} y2={recent.length * ROW}
                  stroke="var(--ink-2)" strokeWidth="1" strokeDasharray="4 3" opacity="0.7" />
              )}
              {Array.from({ length: 7 }, (_, i) => i * 3 * 60).map((t) => (
                <line key={t} x1={sx(t)} y1={0} x2={sx(t)} y2={recent.length * ROW}
                  stroke="var(--rule)" opacity="0.5" />
              ))}
              {recent.map((s, i) => {
                const b = clockToAxis(localMinutes(s.bedtime, s.bed_offset_min), ORIGIN_H);
                const span = spanMinutes(s);
                const y = i * ROW;
                const isLast = page === 0 && i === recent.length - 1;
                const shortNight = (s.total_min ?? 0) < 6.5 * 60;
                return (
                  <g key={s.date}>
                    <text x={0} y={y + ROW - 5} fontSize="7.5" fill="var(--ink-3)">{s.date.slice(5)}</text>
                    <text x={54} y={y + ROW - 5} textAnchor="end" fontSize="7.5"
                      fill={isLast ? "var(--ink)" : "var(--ink-3)"}>
                      {((s.total_min ?? 0) / 60).toFixed(1)}h
                    </text>
                    <rect x={sx(b)} y={y + 2} width={Math.max(sx(b + span) - sx(b), 1.5)} height={ROW - 6}
                      rx="2" fill="var(--core)" opacity={isLast ? 0.95 : 0.4} />
                    {shortNight && (
                      <rect x={sx(b)} y={y + 2} width={Math.max(sx(b + span) - sx(b), 1.5)} height={ROW - 6}
                        rx="2" fill="none" stroke="var(--over)" strokeWidth="1" />
                    )}
                  </g>
                );
              })}
              {Array.from({ length: 7 }, (_, i) => i * 3 * 60).map((t) => (
                <text key={t} x={sx(t)} y={recent.length * ROW + 12} textAnchor="middle"
                  fontSize="7.5" fill="var(--ink-3)">{axisToLabel(t, ORIGIN_H)}</text>
              ))}
            </svg>
            <Legend items={[
              { label: "Time asleep", color: "var(--core)" },
              { label: "Under 6h30", color: "var(--over)", outline: true },
            ]} />
          </>
        )}
        <Foot>
          Every night on one clock axis, so a regular schedule is a straight left edge.
          The shaded box in each distribution is where the middle half of nights fall —
          its width is your consistency, in minutes.
        </Foot>
      </Card>

      {/* ── Duration ───────────────────────────────────────────────────────── */}
      <Card>
        <Eyebrow left="DURATION" right={`${durs.length} NIGHTS`} />
        <Hero value={avg != null ? fmtMinutes(avg) : "—"} unit={["MEAN", `GOAL ${fmtMinutes(targetMin)}`]} />
        <HeroSub>
          Median <K>{med != null ? fmtMinutes(med) : "—"}</K>
          <Sep />
          <K>{short}</K> night{short === 1 ? "" : "s"} under 6h30
          {avg != null && med != null && avg < med - 5 && (
            <><Sep />mean sits <K>{fmtMinutes(med - avg)}</K> below median — a few very
              short nights, not a uniformly low average</>
          )}
        </HeroSub>

        {/* The shape is the point. Mean and median are nearly identical here while
            the distribution is clearly left-skewed: a solid mode with a straggle of
            bad nights. "Raise the average" and "cut the bad nights" are different
            instructions and only the shape tells them apart. */}
        <div className="mt-3">
          <Histogram
            values={durs}
            binSize={30}
            fmtX={(m) => `${Math.floor(m / 60)}h`}
            tickEvery={2}
            colorFor={(binStart) => (binStart < 6.5 * 60 ? "var(--over)" : "var(--core)")}
            refs={[
              { value: targetMin, label: `goal ${fmtMinutes(targetMin)}` },
              ...(med != null ? [{ value: med, label: `median`, dash: "2 2" }] : []),
            ]}
          />
        </div>
        <Legend items={[
          { label: "Nights", color: "var(--core)" },
          { label: "Under 6h30", color: "var(--over)" },
        ]} />

        <Foot>
          One bar per 30-minute band; the number in a bar is how many nights landed
          there. Nights whose clock times were unusable are still counted here — their
          totals are correct.
        </Foot>
      </Card>

      {/* ── Data quality ───────────────────────────────────────────────────── */}
      {excluded.length > 0 && (
        <Card>
          <Eyebrow left="EXCLUDED FROM SCHEDULE" right={`${excluded.length} OF ${sessions.length}`} />
          <p className="prose-ui mt-2 text-[11px] leading-snug text-ink-2">
            Apple reports one sleep block per day, so an afternoon nap becomes that
            night's wake time. These nights are dropped from the clock views above and
            kept in the duration figures, where their totals are still right.
          </p>
          <div className="mt-2 space-y-1">
            {excluded.slice(-6).map((s) => (
              <div key={s.date} className="flex items-baseline gap-2 text-[10px]">
                <span className="text-ink-3">{s.date}</span>
                <span className="text-ink-2">
                  {axisToLabel(clockToAxis(localMinutes(s.bedtime, s.bed_offset_min), ORIGIN_H), ORIGIN_H)} →{" "}
                  {axisToLabel(clockToAxis(localMinutes(s.wake_time, s.wake_offset_min), ORIGIN_H), ORIGIN_H)}
                </span>
                <span className="ml-auto text-ink-3">{clockSuspect(s)}</span>
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}
