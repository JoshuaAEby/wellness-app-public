import { useState, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ResponsiveContainer, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ReferenceLine } from "recharts";
import { api } from "../../api.js";
import { Card, Eyebrow, HeroSub, Foot } from "../../components/ui.jsx";
import { fmtDate } from "../../utils.js";
import { yearMarkers } from "./chartUtils.jsx";

/**
 * THE RECORD — the weekly check-off grid.
 *
 * Sits at the top because it is what the tab is actually opened to do: mostly
 * backfill, sometimes today. Today's LOG view writes the same rows, and both
 * are fine — they invalidate the same query keys.
 *
 * The grid keeps THEME grouping (Sleep / Movement / Nutrition / Mind) rather
 * than the measurement-type grouping used by the cards below it. A checklist is
 * scanned by life-area, and theme is genuinely the right axis for that. It is
 * the wrong axis for a scoreboard, which is why "What's holding" groups by type
 * instead — the two views deliberately disagree.
 *
 * Cell logic (tap cycles, defaultTrue semantics, the 26-week
 * drill-down and the goal editor) is lifted VERBATIM from the old Habits tab.
 * It was subtle and working; re-deriving it would have risked silent regressions.
 */

export const HABITS = [
  // defaultTrue: assumed done unless explicitly marked failed (like no_alcohol pattern)
  //
  // `manual` was true until 2026-09-07; this became DERIVED on 2026-08-19 (latest
  // logged dose vs the cutoff). It stays tappable — `defaultTrue` + `editable` keep the
  // manual override, which build_calendar still honours ahead of the derived value —
  // but it is no longer counted as a manual habit for adherence purposes. See the note
  // in services/adherence.py for why `meditation` was NOT reclassified alongside it.
  { key: "no_late_caffeine", label: "No late caffeine", color: "#f59e0b", emoji: "☕", manual: false, editable: true, defaultTrue: true },
  { key: "outdoor_light_am", label: "Outdoor light",    color: "#38bdf8", emoji: "☀️", manual: false, editable: true },
  { key: "supplements",      label: "Supplements",      color: "#a78bfa", emoji: "💊", manual: true  },
  { key: "creatine",         label: "Creatine",         color: "#fb923c", emoji: "🧪", manual: true  },
  { key: "meditation",       label: "Meditation",       color: "#10b981", emoji: "🧘", manual: true  },
  { key: "worked_out",       label: "Worked out",       color: "#f97316", emoji: "💪", manual: false },
  { key: "went_for_walk",    label: "Long Walk",        color: "#22d3ee", emoji: "🚶", manual: false },
  { key: "slept_well",       label: "Slept well",       color: "#818cf8", emoji: "😴", manual: false },
  { key: "no_alcohol",       label: "No alcohol",       color: "#34d399", emoji: "🍵", manual: false },
  // Met today's target (services/fluids). A dot = no water logged, which is not a miss.
  { key: "fluids_goal",      label: "Fluids goal",      color: "#22d3ee", emoji: "💧", manual: false },
  { key: "consistent_bedtime", label: "Consistent bedtime", color: "#818cf8", emoji: "🛏️", manual: false },
  { key: "consistent_waketime", label: "Consistent wake time", color: "#818cf8", emoji: "🌅", manual: false },
];

export const HABIT_BY_KEY = Object.fromEntries(HABITS.map((h) => [h.key, h]));

export const GRID_SECTIONS = [
  { name: "Sleep",             icon: "🌙", keys: ["no_late_caffeine", "outdoor_light_am", "slept_well", "consistent_bedtime", "consistent_waketime"] },
  { name: "Movement",          icon: "🏃", keys: ["worked_out", "went_for_walk"] },
  { name: "Nutrition",         icon: "🍳", keys: ["supplements", "creatine", "no_alcohol", "fluids_goal"] },
  { name: "Mind", icon: "🧘", keys: ["meditation"] },
];

export function pad2(n) {
  return String(n).padStart(2, "0");
}

export function localTodayStr() {
  const d = new Date();
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

export function monthBounds(year, month) {
  const start = `${year}-${pad2(month)}-01`;
  const lastDay = new Date(year, month, 0).getDate(); // month is 1-indexed; day 0 = last day of prev month
  const end = `${year}-${pad2(month)}-${pad2(lastDay)}`;
  return { start, end };
}

export function mondayOf(d) {
  const x = new Date(d);
  x.setHours(12, 0, 0, 0);
  x.setDate(x.getDate() - ((x.getDay() + 6) % 7));
  return x;
}

export function dstr(d) {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

export function addDaysDate(d, n) {
  const x = new Date(d);
  x.setDate(x.getDate() + n);
  return x;
}

export function weekDates(monday) {
  return Array.from({ length: 7 }, (_, i) => addDaysDate(monday, i));
}

export function nextHabitValue(habit, cur) {
  if (habit.external) return cur === true ? null : true;   // logged <-> not logged
  if (habit.defaultTrue) return cur === false ? null : false;     // ✓(assumed) → ✗ → ✓
  if (habit.editable)    return cur === true ? false : true;      // override: ✓ ↔ ✗
  return cur === true ? false : cur === false ? null : true;      // empty → ✓ → ✗ → empty
}

// A PAST day with no entry, on a manual daily habit, is a miss — not "unknown".
//
// The backend already scored it that way: services/adherence.py uses a calendar-day
// denominator for manual habits, so an unlogged day counts against you there. The grid
// drew the same day as an empty dashed box, so the picture and the percentage told
// different stories about the same square. This closes that.
//
// The predicate picks out exactly the manual DAILY habits (supplements, creatine,
// meditation) without introducing a fourth copy of that list:
//   - `external` excludes any manually logged frequency EVENT stored outside habit_logs
//     (none ship today), where "no record" means it did not happen rather than a goal was
//     missed. Same reasoning the derived event habits (worked_out, went_for_walk) rely on.
//   - `defaultTrue` excludes no_late_caffeine, where null already means ✓ (assumed).
//   - derived habits are excluded by `manual` itself; build_calendar already resolves
//     their past days to true/false, and a null there is genuinely unknowable (a dead
//     watch is not a missed night of sleep).
//
// TODAY IS NEVER AN IMPLIED MISS. The day is not over; marking it failed at 09:00 would
// be a lie the user then has to argue with.
function impliesMiss(habit, isToday, isFuture) {
  return Boolean(habit.manual) && !habit.external && !habit.defaultTrue
         && !isToday && !isFuture;
}

export function HabitCell({ habit, value, isToday, isFuture, onTap }) {
  const manual = habit.manual || habit.editable;
  const done = value === true || (habit.defaultTrue && value == null);
  const failed = value === false;
  const impliedMiss = value == null && !done && impliesMiss(habit, isToday, isFuture);

  let glyph = "", bg = "#0f172a", color = "#334155", border = "none";
  if (done) {
    glyph = "✓"; color = "#34d399"; bg = manual ? "rgba(52,211,153,.16)" : "#0f172a";
  } else if (failed) {
    if (habit.key === "no_alcohol") { glyph = "🍺"; color = "#f59e0b"; bg = "rgba(245,158,11,.14)"; }
    else { glyph = "✗"; color = "#f87171"; bg = manual ? "rgba(248,113,113,.14)" : "#0f172a"; }
  } else if (impliedMiss) {
    // Dimmer than an explicit ✗ and with no fill: it still reads as a miss (and counts
    // as one), but stays visibly distinct from a day you actively marked missed.
    glyph = "✗"; color = "rgba(248,113,113,.45)"; bg = "#0f172a";
    border = "1px dashed rgba(248,113,113,.25)";
  } else {
    glyph = manual ? "" : "·";
    border = manual ? "1px dashed #334155" : "none";
  }

  const style = {
    height: 30, borderRadius: 7, background: bg, color, border,
    display: "flex", alignItems: "center", justifyContent: "center",
    fontSize: 14, width: "100%",
    outline: isToday ? "1.5px solid rgba(99,102,241,.7)" : "none", outlineOffset: -1.5,
    opacity: isFuture ? 0.25 : 1,
  };
  if (manual && !isFuture) {
    return <button onClick={onTap} style={{ ...style, cursor: "pointer" }} aria-label={`${habit.label} ${value === true ? "done" : value === false ? "missed" : impliedMiss ? "not logged, counts as missed" : "not set"}`}>{glyph}</button>;
  }
  return <div style={style}>{glyph}</div>;
}

const TREND_WEEKS = 26;

export function HabitTrendChart({ habit, goal }) {
  const end = localTodayStr();
  const startD = new Date();
  startD.setDate(startD.getDate() - TREND_WEEKS * 7);
  const start = `${startD.getFullYear()}-${pad2(startD.getMonth() + 1)}-${pad2(startD.getDate())}`;

  const { data: days = [], isLoading } = useQuery({
    queryKey: ["habit-trend-calendar", start, end],
    queryFn: () => api.getHabitsCalendar(start, end),
    staleTime: 1000 * 60 * 30,
  });

  const weeks = useMemo(() => {
    const buckets = new Map();
    for (const d of days) {
      const dt = new Date(`${d.date}T00:00:00`);
      dt.setDate(dt.getDate() - ((dt.getDay() + 6) % 7)); // back to Monday
      const iso = `${dt.getFullYear()}-${pad2(dt.getMonth() + 1)}-${pad2(dt.getDate())}`;
      // Keyed on the year-aware ISO Monday, not the "Aug 10" label: yearMarkers and
      // ReferenceLine both address buckets by `key`, and a bare label repeats every
      // year (see chartUtils.yearMarkers). The label is applied by tickFormatter.
      if (!buckets.has(iso)) buckets.set(iso, { key: iso, done: 0 });
      if (d[habit.key] === true) buckets.get(iso).done += 1;
    }
    return [...buckets.values()].sort((a, b) => (a.key < b.key ? -1 : 1));
  }, [days, habit.key]);

  if (isLoading) return <p className="mt-3 text-xs text-slate-500">Loading history…</p>;
  if (weeks.length < 2) return null;

  const goalWeekly = goal
    ? (goal.period === "week" ? goal.target : goal.target * 7 / 30.44)
    : null;

  return (
    <div className="mt-3">
      <p className="mb-1 text-[10px] uppercase tracking-widest text-slate-500">
        Days per week · last {TREND_WEEKS} weeks
      </p>
      <ResponsiveContainer width="100%" height={110}>
        <BarChart data={weeks} margin={{ top: 4, right: 4, bottom: 0, left: -28 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false} />
          <XAxis dataKey="key" tickFormatter={fmtDate} tick={{ fill: "#64748b", fontSize: 9 }} interval="preserveStartEnd" />
          {/* 26 weeks spans a New Year, and the tick labels ("Aug 10") carry no year,
              so without these a January bucket is indistinguishable from the previous
              one. Same treatment the Exposures charts already use. */}
          {yearMarkers(weeks).map((m) => (
            <ReferenceLine key={m.key} x={m.key} stroke="var(--ink-3)" strokeDasharray="2 3"
              strokeOpacity={0.7}
              label={{ value: m.year, position: "insideTopLeft", fontSize: 8,
                       fill: "var(--ink-3)" }} />
          ))}
          <YAxis domain={[0, 7]} ticks={[0, 7]} tick={{ fill: "#64748b", fontSize: 9 }} />
          <Tooltip
            contentStyle={{ background: "#1e293b", border: "1px solid #334155", borderRadius: 8, fontSize: 11 }}
            labelStyle={{ color: "#94a3b8" }}
            labelFormatter={fmtDate}
            formatter={(v) => [`${v} day${v === 1 ? "" : "s"}`, "done"]}
          />
          {goalWeekly != null && (
            <ReferenceLine y={goalWeekly} stroke={GOLD}
              strokeDasharray={goal.period === "week" ? "0" : "4 4"} strokeWidth={1.5} />
          )}
          <Bar dataKey="done" fill={habit.color} radius={[2, 2, 0, 0]} isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
      {goalWeekly != null && (
        <p className="text-[10px] text-slate-600">
          — goal {goal.period === "week"
            ? `${goal.target}/wk`
            : `${goal.target}/mo (shown at ≈${goalWeekly.toFixed(1)}/wk)`}
        </p>
      )}
    </div>
  );
}

export function HabitGrid({ weekDays, todayStr, byDate, goals = {}, onToggle, doseRows }) {
  const [infoKey, setInfoKey] = useState(null);
  const [doseHint, setDoseHint] = useState({});   // experiment id -> hint open
  const COLS = "34px repeat(7, 1fr)";

  // Goal status for a habit: gold (met) + a human label, consistent with the row tint.
  const goalStatus = (key) => {
    const goal = goals?.[key];
    if (!goal) return { gold: false, label: null };
    if (goal.period === "week") {
      const cnt = weekDays.reduce((n, d) => n + (byDate[dstr(d)]?.[key] === true ? 1 : 0), 0);
      return { gold: cnt >= goal.target, label: `${cnt}/${goal.target} this week` };
    }
    return { gold: !!goal.met, label: `${goal.current}/${goal.target} this ${goal.period_label || "month"}` };
  };

  const info = infoKey ? HABIT_BY_KEY[infoKey] : null;

  return (
    <div className="rounded-xl bg-slate-800 p-2.5">
      <div className="grid items-end gap-1 mb-1.5" style={{ gridTemplateColumns: COLS }}>
        <span />
        {weekDays.map((d) => {
          const ds = dstr(d), isToday = ds === todayStr;
          return (
            <div key={ds} className="text-center text-xs leading-tight"
              style={{ color: isToday ? "#a5b4fc" : "#64748b", background: isToday ? "rgba(99,102,241,.18)" : "transparent", borderRadius: 6, padding: "2px 0" }}>
              {"MTWTFSS"[(d.getDay() + 6) % 7]}<br />{d.getDate()}
            </div>
          );
        })}
      </div>

      {GRID_SECTIONS.map((sec) => (
        <div key={sec.name}>
          <p className="mt-2 mb-1 text-[10px] font-medium uppercase tracking-widest text-slate-500">{sec.icon} {sec.name}</p>
          {/* Randomized dose row, rendered outside the generic habit loop on purpose.
              Its cells are NOT habit cells: on an off-night an empty cell means "correctly
              did not take it", not "missed". Running it through the normal renderer would
              paint a row that looks like a habit failed half the time by design. Writes
              go to the experiment via /dose — experiment_assignments stays the single
              source of truth for compliance. */}
          {sec.name === "Nutrition" && (doseRows || []).map((row) => (
            <div key={row.id}>
              <div className="grid items-center gap-1 mb-1" style={{ gridTemplateColumns: COLS }}>
                {/* Tappable (note 65): a bare letter and blank cells didn't say this row
                    could be backfilled. One row per running non-training experiment. */}
                <button onClick={() => setDoseHint((h) => ({ ...h, [row.id]: !h[row.id] }))}
                        className="flex items-center justify-center" style={{ height: 30 }}
                        title={`${row.title} — tap for help`}>
                  <span className="text-[13px] font-bold text-violet-300 underline decoration-dotted">{row.label}</span>
                </button>
                {weekDays.map((d) => {
                  const ds = dstr(d);
                  const a = row.days[ds];
                  const future = ds > todayStr;
                  if (!a) return <div key={ds} style={{ height: 30 }} />;
                  const off = !a.is_treatment;
                  // Manual rows: unrecorded = "didn't" — blank means not done, tap to
                  // flip. Auto rows (after-dinner): followed-or-not comes from the food log,
                  // a tap stores an override. Either way the randomised (ITT) analysis reads
                  // only the assignment.
                  const val = row.auto ? (a.complied ?? a.auto_followed) : a.complied;
                  const took = val === true;
                  const title = row.auto
                    ? `${ds} — ${off ? "kitchen-closed night" : "have-something night"}, ${
                        val == null ? "no timed dinner logged" : took ? "followed" : "not followed"} — tap to change`
                    : off ? `${ds} — off night${took ? " (taken anyway: protocol deviation)" : ""}`
                          : `${ds} — dose night, ${took ? "taken" : "not taken"} — tap to change`;
                  return (
                    <button
                      key={ds}
                      disabled={future}
                      onClick={() => row.onToggle(ds, !took)}
                      className="flex items-center justify-center rounded-md"
                      style={{
                        height: 30,
                        opacity: future ? 0.25 : 1,
                        background: took ? "rgba(167,139,250,.30)"
                          : off ? "transparent" : "rgba(148,163,184,.10)",
                        border: off ? "1px dashed rgba(100,116,139,.45)" : "none",
                      }}
                      title={title}
                    >
                      <span className="text-[13px] leading-none"
                            style={{ color: took ? "#c4b5fd" : "#475569" }}>
                        {took ? "✓" : off ? "·" : ""}
                      </span>
                    </button>
                  );
                })}
              </div>
              {doseHint[row.id] && (
                <p className="mb-1.5 ml-[38px] text-[10px] leading-snug text-slate-400">
                  <span className="text-violet-300">{row.label}</span> = {row.title}, a running experiment.{" "}
                  {row.auto
                    ? <>Solid cells are have-something nights, dashed are kitchen-closed nights. ✓ = you followed
                        that night's rule, read from your food log. Tap a night to correct it.</>
                    : <>Solid cells are dose nights, dashed are off nights. ✓ = taken; blank = not taken.
                        Tap any night to switch it.</>}
                </p>
              )}
            </div>
          ))}
          {sec.keys.map((key) => {
            const h = HABIT_BY_KEY[key];
            const manual = h.manual || h.editable;
            const { gold } = goalStatus(key);
            return (
              <div key={key} className="grid items-center gap-1 mb-1"
                style={{ gridTemplateColumns: COLS,
                         ...(gold ? { background: "rgba(251,191,36,.12)", borderRadius: 7 } : {}) }}>
                <button onClick={() => setInfoKey(key)} aria-label={`${h.label} details`}
                  className="flex items-center justify-center" style={{ height: 30, width: "100%" }}>
                  <span style={{ fontSize: 17, opacity: manual ? 1 : 0.8, filter: gold ? "drop-shadow(0 0 3px rgba(251,191,36,.7))" : "none" }}>{h.emoji}</span>
                </button>
                {weekDays.map((d) => {
                  const ds = dstr(d);
                  return (
                    <HabitCell key={ds} habit={h} value={byDate[ds]?.[key]}
                      isToday={ds === todayStr} isFuture={ds > todayStr}
                      onTap={() => onToggle(ds, key, nextHabitValue(h, byDate[ds]?.[key]))} />
                  );
                })}
              </div>
            );
          })}
        </div>
      ))}

      {/* Emoji detail popup — full name + any goal */}
      {info && (() => {
        const goal = goals[infoKey];
        const manual = info.manual || info.editable;
        const gs = goalStatus(infoKey);
        return (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-6" onClick={() => setInfoKey(null)}>
            <div className="w-full max-w-xs rounded-2xl bg-slate-800 p-5" onClick={(e) => e.stopPropagation()}>
              <div className="flex items-center gap-2.5 mb-2">
                <span style={{ fontSize: 26 }}>{info.emoji}</span>
                <p className="text-base font-semibold" style={{ color: info.color }}>{info.label}</p>
              </div>
              <p className="text-xs text-slate-400 mb-3">
                {manual ? "Tap a day to log — ✓ done · ✗ missed · empty = not set" : "Tracked automatically from your data"}
              </p>
              {goal ? (
                <div className="rounded-lg bg-slate-700/50 px-3 py-2.5">
                  <p className="text-sm text-slate-300">Goal: <span className="font-semibold">{goal.target}× / {goal.period}</span></p>
                  <p className="mt-0.5 text-sm" style={{ color: gs.gold ? GOLD : "#94a3b8" }}>
                    {gs.label}{gs.gold ? " — met ✓" : ""}
                  </p>
                </div>
              ) : (
                <p className="text-sm text-slate-500">
                  No frequency goal set{manual ? "." : " — add one in the monthly summary below."}
                </p>
              )}
              {CONSISTENCY_KINDS[infoKey] && <ConsistencyTarget kind={CONSISTENCY_KINDS[infoKey]} color={info.color} />}
              <HabitTrendChart habit={info} goal={goal} />
              <button onClick={() => setInfoKey(null)}
                className="mt-4 w-full rounded-lg bg-slate-700 py-2 text-sm text-slate-200 hover:bg-slate-600 transition-colors">
                Close
              </button>
            </div>
          </div>
        );
      })()}
    </div>
  );
}

const GOLD = "#fbbf24";

const CONSISTENCY_KINDS = { consistent_bedtime: "bedtime", consistent_waketime: "waketime" };

function fmtClock(min) {
  const h = Math.floor(min / 60) % 24, m = min % 60;
  return `${(h % 12) || 12}:${String(m).padStart(2, "0")} ${h < 12 ? "am" : "pm"}`;
}

// Tonight's target: median of the reference nights ± threshold, plus each night's offset
// from that median (dot plot against the shaded window). /api/habits/consistency-targets.
export function ConsistencyTarget({ kind, color }) {
  const { data, isLoading } = useQuery({ queryKey: ["consistencyTargets"], queryFn: api.getConsistencyTargets });
  if (isLoading) return <p className="mt-3 text-xs text-slate-500">Loading target…</p>;
  const t = data?.[kind];
  if (!t) return <p className="mt-3 text-xs text-slate-500">Not enough recent home nights for a target yet.</p>;
  const thr = data.threshold_min;
  const span = Math.max(thr * 2, ...t.nights.map((n) => Math.abs(n.offset_min) + 10));
  const W = 260, H = 46, pad = 8;
  const x = (o) => pad + ((Math.max(-span, Math.min(span, o)) + span) / (2 * span)) * (W - 2 * pad);
  const nights = [...t.nights].sort((a, b) => a.date.localeCompare(b.date));
  const ln = t.last_night;
  const verb = kind === "bedtime" ? "In bed" : "Up";
  return (
    <div className="mt-3 rounded-lg bg-slate-700/50 px-3 py-2.5">
      <p className="text-sm text-slate-300">
        {verb} between <span className="font-semibold" style={{ color }}>{fmtClock(t.lo_clock_min)}–{fmtClock(t.hi_clock_min)}</span>
      </p>
      <p className="mt-0.5 text-xs text-slate-400">
        Median {fmtClock(t.ref_clock_min)} ± {thr} min · {t.n} {t.basis === "last_14_nights" ? "of the last 14 nights at home" : "most recent home nights"}
      </p>
      <svg viewBox={`0 0 ${W} ${H}`} className="mt-2 w-full" role="img" aria-label="Recent nights versus target window">
        <rect x={x(-thr)} y={6} width={x(thr) - x(-thr)} height={22} rx={3} fill={color} opacity={0.15} />
        <line x1={x(0)} x2={x(0)} y1={4} y2={30} stroke={color} strokeWidth={1.5} />
        {nights.map((n, i) => {
          const last = ln && n.date === ln.date;
          return <circle key={n.date} cx={x(n.offset_min)} cy={17} r={last ? 5 : 3.5}
            fill={last ? (ln.consistent ? color : "#f87171") : "#cbd5e1"}
            opacity={last ? 1 : 0.35 + 0.5 * (i / Math.max(1, nights.length - 1))}>
            <title>{`${fmtDate(n.date)}: ${n.offset_min > 0 ? "+" : ""}${n.offset_min} min`}</title>
          </circle>;
        })}
        <text x={x(-thr)} y={42} textAnchor="middle" fontSize={9} fill="#94a3b8">−{thr}</text>
        <text x={x(0)} y={42} textAnchor="middle" fontSize={9} fill="#94a3b8">{fmtClock(t.ref_clock_min)}</text>
        <text x={x(thr)} y={42} textAnchor="middle" fontSize={9} fill="#94a3b8">+{thr}</text>
      </svg>
      {ln && (
        <p className="mt-1 text-xs" style={{ color: ln.consistent ? "#94a3b8" : "#f87171" }}>
          Last night: {fmtClock(ln.clock_min)}, {ln.offset_min > 0 ? "+" : ""}{ln.offset_min} min vs that night's median {ln.consistent ? "✓" : "✗"}
        </p>
      )}
    </div>
  );
}

export function GoalProgress({ goal, color }) {
  const { current, target, met } = goal;
  const fill = met ? GOLD : color;
  if (target <= 8) {
    // Segmented "lines that fill" — one cell per rep toward the target.
    return (
      <div className="mt-1 flex gap-1">
        {Array.from({ length: target }).map((_, i) => (
          <div key={i} className="h-1.5 flex-1 rounded-full"
            style={{ backgroundColor: i < current ? fill : "#334155" }} />
        ))}
      </div>
    );
  }
  const pct = Math.min(100, Math.round((current / target) * 100));
  return (
    <div className="mt-1 h-1.5 rounded-full bg-slate-700">
      <div className="h-1.5 rounded-full transition-all" style={{ width: `${pct}%`, backgroundColor: fill }} />
    </div>
  );
}

export function GoalEditor({ goal, onSave, onRemove, onCancel, pending }) {
  const [target, setTarget] = useState(goal?.target ?? 3);
  const [period, setPeriod] = useState(goal?.period ?? "week");
  return (
    <div className="mt-2 flex flex-wrap items-center gap-2 rounded-lg bg-slate-700/40 p-2">
      <div className="flex items-center gap-1.5">
        <button onClick={() => setTarget((t) => Math.max(1, t - 1))}
          className="h-6 w-6 rounded-md bg-slate-700 text-slate-300 text-sm leading-none">−</button>
        <span className="w-5 text-center text-sm font-bold text-white">{target}</span>
        <button onClick={() => setTarget((t) => Math.min(31, t + 1))}
          className="h-6 w-6 rounded-md bg-slate-700 text-slate-300 text-sm leading-none">+</button>
        <span className="text-xs text-slate-400">×&nbsp;per</span>
      </div>
      <div className="flex overflow-hidden rounded-md border border-slate-600">
        {["week", "month"].map((p) => (
          <button key={p} onClick={() => setPeriod(p)}
            className={`px-2.5 py-1 text-xs capitalize ${period === p ? "bg-indigo-600 text-white" : "text-slate-400"}`}>
            {p}
          </button>
        ))}
      </div>
      <button disabled={pending} onClick={() => onSave(target, period)}
        className="ml-auto rounded-md bg-emerald-600 px-2.5 py-1 text-xs font-medium text-white disabled:opacity-50">
        Save
      </button>
      {goal && (
        <button disabled={pending} onClick={onRemove}
          className="rounded-md px-2 py-1 text-xs text-rose-400">Remove</button>
      )}
      <button onClick={onCancel} className="px-2 py-1 text-xs text-slate-400">Cancel</button>
    </div>
  );
}
