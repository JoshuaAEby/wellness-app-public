import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";

// Tonight's assignment for any running non-training experiment, plus dose logging.
//
// The arm is SHOWN, not hidden: this trial has no placebo arm, so you already know
// which nights you're taking something. Hiding it would only stop you following the
// protocol. (With a placebo arm this card would show "take tonight's capsule" instead.)
//
// Logging the time is not bookkeeping. For an acute intervention the dose-to-outcome
// interval has to be constant — L-theanine's half-life is ~1 h, so a dose taken in the
// morning sits ~12 half-lives from bedtime and cannot affect that night's HRV. That day
// is a placebo day wearing a treatment label, and because required n scales as 1/d²,
// diluting the effect multiplies the duration quadratically: half the doses at the wrong
// hour turns one year into four. Recording the time cannot repair it after the fact —
// conditioning on a post-randomisation variable breaks the randomised comparison — so
// this exists to make drift visible while the protocol can still be corrected.
const PROMPT_FROM_H = 19;   // 7pm — early enough to PLAN the evening, not just react

export default function DoseCard() {
  const qc = useQueryClient();
  const { data: experiments } = useQuery({
    queryKey: ["experiments"], queryFn: api.getExperiments });

  const supp = (experiments || []).filter((e) => e.active && e.kind !== "training");
  if (supp.length === 0) return null;
  // Each experiment brings its own copy and visibility hour from the server
  // (coach.EXPERIMENT_COPY): the L-theanine card stays an evening card; the after-dinner
  // rule shows from the morning so the evening can be planned around it.
  // Evening only. The dose is taken at bedtime, so a prompt at 09:00 is dead space for
  // twelve hours — and a prompt you scroll past all day is one you stop seeing. Stays
  // visible once logged (collapsed) so the day still reads as complete.
  //
  // Moved 20:00 -> 19:00 on request: knowing the arm an hour earlier allows the evening
  // to be planned around it rather than interrupted by it, which protects the constant
  // dose-to-bedtime interval the comment above depends on.
  const hour = new Date().getHours();
  const shown = supp.filter((e) => hour >= (e.copy?.show_from_hour ?? PROMPT_FROM_H));
  if (shown.length === 0) return null;
  return shown.map((e) => <DoseRow key={e.id} exp={e} qc={qc} />);
}

function DoseRow({ exp, qc }) {
  const { data: today } = useQuery({
    queryKey: ["experiment-today", exp.id],
    queryFn: () => api.getExperimentToday(exp.id),
    staleTime: 1000 * 60 * 5,
  });
  if (!today || !today.arm) return null;

  const treatment = today.is_treatment;
  const logged = today.complied != null;
  const copy = exp.copy || {};
  // Followed-or-not comes from the food log for this kind: just state tonight's rule.
  // Compact since 2026-11-04 (note 97): the footer explaining the auto-check became one
  // word in the corner; corrections still live in Habits → Record.
  if (copy.auto_compliance) {
    return (
      <div className="rounded-2xl border border-rule bg-card px-4 py-3">
        <div className="flex items-baseline justify-between gap-2">
          <p className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">{exp.label}</p>
          <p className="text-[9px] tracking-micro text-ink-3">AUTO-CHECKED</p>
        </div>
        <p className="mt-1 text-[13px] font-semibold text-ink">{treatment ? copy.a_title : copy.b_title}</p>
        <p className="mt-0.5 text-[11px] leading-snug text-ink-3">{treatment ? copy.a_hint : copy.b_hint}</p>
      </div>
    );
  }

  const log = (complied) => {
    const now = new Date();
    api.logDose(exp.id, {
      complied,
      dose_minute: complied ? now.getHours() * 60 + now.getMinutes() : null,
    }).then(() => {
      qc.invalidateQueries({ queryKey: ["experiment-today", exp.id] });
      qc.invalidateQueries({ queryKey: ["experiment-accrual"] });
    });
  };

  if (logged) {
    return (
      <div className="flex items-center gap-2 rounded-xl bg-slate-800/60 px-4 py-2.5">
        <span className="text-xs font-medium uppercase tracking-widest text-slate-500">
          {exp.arm_a}
        </span>
        <span className={today.complied ? "text-emerald-400" : "text-slate-600"}>
          {today.complied ? "✓" : "—"}
        </span>
        <span className="flex-1 text-[11px] text-slate-500">
          {treatment
            ? (today.complied ? "taken" : "assigned but not taken")
            : (today.complied ? "logged — off night" : "off night")}
          {today.dose_minute != null &&
            ` · ${String(Math.floor(today.dose_minute / 60)).padStart(2, "0")}:${String(today.dose_minute % 60).padStart(2, "0")}`}
        </span>
        <button onClick={() => log(!today.complied)} className="text-[10px] text-slate-600">
          undo
        </button>
      </div>
    );
  }

  return (
    <div className="rounded-xl bg-slate-800 p-4">
      <p className="text-xs font-medium uppercase tracking-widest text-slate-400">
        {exp.label}
      </p>
      <p className="mt-1 text-sm font-semibold text-white">
        {treatment ? (copy.a_title ?? `Tonight: take ${exp.arm_a}`) : (copy.b_title ?? "Tonight: none")}
      </p>
      <p className="mt-1 text-[11px] leading-relaxed text-slate-500">
        {treatment ? copy.a_hint : copy.b_hint}
      </p>
      <div className="mt-3 flex gap-2">
        <button
          onClick={() => log(true)}
          className="flex-1 rounded-lg bg-indigo-600 py-2 text-sm font-semibold text-white active:bg-indigo-500"
        >
          {treatment ? "Taken" : "Confirmed"}
        </button>
        <button
          onClick={() => log(false)}
          className="rounded-lg bg-slate-700 px-4 py-2 text-sm text-slate-300 active:bg-slate-600"
        >
          {treatment ? "Didn't" : "Took it anyway"}
        </button>
      </div>
    </div>
  );
}
