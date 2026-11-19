import { useState, useMemo } from "react";
import { useQuery, useQueries, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api.js";
import { Card, Eyebrow, HeroSub, Foot } from "../../components/ui.jsx";
import { HabitGrid, weekDates, mondayOf, dstr, addDaysDate, localTodayStr } from "./grid.jsx";

/**
 * The weekly record. Wrapper only — the grid itself is the original component.
 *
 * Both this and Today's LOG view write habit_logs and invalidate the same keys,
 * which is intentional: this is the surface for backfill (and for correcting a
 * past day), Today is the surface for the day in front of you.
 */
export default function Record() {
  const qc = useQueryClient();
  const today = useMemo(() => new Date(), []);
  const [weekStart, setWeekStart] = useState(() => mondayOf(today));

  const weekDays = useMemo(() => weekDates(weekStart), [weekStart]);
  const weekStartStr = dstr(weekDays[0]);
  const weekEndStr = dstr(weekDays[6]);

  const { data: weekData = [] } = useQuery({
    queryKey: ["habits-calendar", weekStartStr, weekEndStr],
    queryFn: () => api.getHabitsCalendar(weekStartStr, weekEndStr),
    staleTime: 60_000,
  });
  // `= {}` is load-bearing: HabitGrid indexes `goals[key]` directly, so an
  // undefined first render (before the query resolves) throws and — with no error
  // boundary in the app shell — blanks the whole screen, not just this card.
  const { data: goals = {} } = useQuery({ queryKey: ["habit-goals"], queryFn: api.getHabitGoals });

  const byDate = useMemo(() => {
    const m = {};
    weekData.forEach((d) => { m[d.date] = d; });
    return m;
  }, [weekData]);

  // Randomised-dose row. Reads/writes the EXPERIMENT, never a parallel habit column —
  // two records of the same fact would drift, and the per-protocol analysis reads
  // only the assignment.
  const { data: allExperiments } = useQuery({ queryKey: ["experiments"], queryFn: api.getExperiments });
  // One row per running non-training experiment (L-theanine, after-dinner, …). useQueries
  // because the number of experiments varies — hooks can't be called in a loop.
  const doseExps = (allExperiments || []).filter((e) => e.active && e.kind !== "training");
  const assignQs = useQueries({
    queries: doseExps.map((e) => ({
      queryKey: ["assignments", e.id, weekStartStr, weekEndStr],
      queryFn: () => api.getAssignments(e.id, weekStartStr, weekEndStr),
      staleTime: 60_000,
    })),
  });
  const doseMutation = useMutation({
    mutationFn: ({ expId, date, complied }) => api.logDose(expId, { date, complied, dose_minute: null }),
    onSuccess: (_d, v) => {
      qc.invalidateQueries({ queryKey: ["assignments"] });
      qc.invalidateQueries({ queryKey: ["experiment-today", v.expId] });
    },
  });
  const doseRows = doseExps.map((e, i) => (assignQs[i]?.data ? {
    id: e.id, label: e.copy?.row_label ?? e.arm_a.slice(0, 1).toUpperCase(), name: e.arm_a,
    title: e.label, auto: !!e.copy?.auto_compliance, days: assignQs[i].data.days,
    onToggle: (date, complied) => doseMutation.mutate({ expId: e.id, date, complied }),
  } : null)).filter(Boolean);

  const cellMutation = useMutation({
    mutationFn: ({ date, key, value }) => api.logHabits({ date, [key]: value }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["habits-calendar"] });
      qc.invalidateQueries({ queryKey: ["streaks"] });
      qc.invalidateQueries({ queryKey: ["habit-goals"] });
      qc.invalidateQueries({ queryKey: ["habit-adherence"] });
    },
  });

  const todayStr = localTodayStr();
  const isCurrentWeek = weekStartStr === dstr(mondayOf(today));
  const weekLabel =
    `${weekDays[0].toLocaleDateString("default", { month: "short", day: "numeric" })} – ` +
    `${weekDays[6].toLocaleDateString("default", { day: "numeric" })}`;

  return (
    <Card>
      <Eyebrow left="THE RECORD" right={isCurrentWeek ? "THIS WEEK" : "PAST WEEK"} />
      <HeroSub>
        Tap to correct any day in the week. Today can be logged here or on Today —
        both write the same row.
      </HeroSub>

      <div className="mt-2 flex items-center justify-between">
        <button onClick={() => setWeekStart((w) => addDaysDate(w, -7))}
                className="rounded px-2 py-1 text-[12px] text-ink-3 hover:text-ink"
                title="Previous week">◀</button>
        <span className="text-[11px] font-semibold text-ink">{weekLabel}</span>
        <button onClick={() => setWeekStart((w) => addDaysDate(w, 7))}
                disabled={isCurrentWeek}
                className="rounded px-2 py-1 text-[12px] text-ink-3 hover:text-ink disabled:opacity-30"
                title="Next week">▶</button>
      </div>

      <div className="mt-2">
        <HabitGrid
          weekDays={weekDays}
          todayStr={todayStr}
          byDate={byDate}
          goals={goals}
          onToggle={(date, key, value) => cellMutation.mutate({ date, key, value })}
          doseRows={doseRows}
        />
      </div>

      <div className="mt-2 flex items-center justify-between">
        <p className="text-[9px] leading-snug text-ink-3">
          ✓ done · ✗ missed · dashed = not logged · dot = automatic
        </p>
      </div>

      <Foot>
        Automatic rows are derived from Apple Health and cannot be tapped — a walk
        counts because it was recorded, not because it was claimed.
      </Foot>
    </Card>
  );
}
