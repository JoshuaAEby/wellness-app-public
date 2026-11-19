import TvCheck from "../components/TvCheck.jsx";
import { useState, useMemo, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";
import { fmtDuration, fmtMinutes, kgToLbs, addDays, foodToday } from "../utils.js";
import DateStepper from "../components/DateStepper.jsx";
import AiStatusBanner from "../components/AiStatusBanner.jsx";
import ReadinessRing from "../components/ReadinessRing.jsx";
import MetricInfoModal, { METRIC_INFO } from "../components/MetricInfoModal.jsx";
import LogFoodModal from "../components/LogFoodModal.jsx";
import MorningCheckinModal from "../components/MorningCheckinModal.jsx";
import DayFlagControl from "../components/DayFlagControl.jsx";
import DayFlagCalendar from "../components/DayFlagCalendar.jsx";
import TripPlanner from "../components/TripPlanner.jsx";
import ClassBoardSheet from "../components/ClassBoardSheet.jsx";
import ComboFromMeal from "../components/ComboFromMeal.jsx";
import TzPromptBanner from "../components/TzPromptBanner.jsx";
import ParseJobsTray from "../components/ParseJobsTray.jsx";
import QuickLogRow from "../components/QuickLogRow.jsx";
import { acwrBand } from "../lib/acwr.js";
import TodaySummaries from "../components/TodaySummaries.jsx";
import IntakeTrio from "../components/IntakeTrio.jsx";
import NapCard from "../components/NapCard.jsx";
import CharacterSheet from "../components/CharacterSheet.jsx";
import BatteryCard from "../components/BatteryCard.jsx";
import DoseCard from "../components/DoseCard.jsx";
import { Card, Eyebrow, Hero, HeroSub, K, Foot, Well, SegmentedTabs, TrackBar, StackBar, EmptyState, BrandTag, RhoMark } from "../components/ui.jsx";

// Dessert added 2026-09-13: 55 of 266 "snacks" were sweets, every timed one at 8–9 pm —
// evening dessert and daytime snacking were two different things under one name. It is
// NOT skippable and NOT part of a complete day (see SKIPPABLE and MAIN_MEALS).
const MEALS = ["breakfast", "lunch", "dinner", "dessert", "snack", "drinks"];
const MEAL_LABELS = { breakfast: "Breakfast", lunch: "Lunch", dinner: "Dinner", dessert: "Dessert", snack: "Snack", drinks: "Drinks" };
// Only the three main meals are "skippable" — a skipped snack/drink isn't a
// meaningful known-zero the way a skipped lunch is.
const SKIPPABLE = new Set(["breakfast", "lunch", "dinner"]);
const MEAL_EMOJI  = { breakfast: "🌅", lunch: "☀️", dinner: "🌙", dessert: "🍨", snack: "🍎", drinks: "🥤" };
const PROTEIN_MULT = { building: 0.75, maintaining: 0.65, cutting: 0.85, peaking: 0.80 };

/** Local calendar date as YYYY-MM-DD — avoids UTC rollover in evening hours */
function localToday() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// ── RPE quick-log modal ───────────────────────────────────────────────────────

function RpeQuickModal({ workout, onSave, onClose, isPending, isError }) {
  const [selected, setSelected] = useState(null);
  const label = workout.activity_type ?? "Workout";
  const date  = workout.start_time?.slice(0, 10);

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/60 px-4 pb-6"
      onClick={onClose}>
      <div className="w-full max-w-md rounded-2xl bg-slate-800 p-5 space-y-4"
        onClick={e => e.stopPropagation()}>
        <div>
          <p className="text-xs font-medium uppercase tracking-widest text-slate-400">Rate Effort</p>
          <p className="text-base font-semibold text-white mt-0.5">{label}</p>
          {date && <p className="text-xs text-slate-500">{date}</p>}
        </div>
        <div>
          <div className="flex justify-between text-xs text-slate-500 mb-1.5 px-0.5">
            <span>Easy</span><span>Max</span>
          </div>
          <div className="grid grid-cols-10 gap-1">
            {[1,2,3,4,5,6,7,8,9,10].map(n => (
              <button
                key={n}
                onClick={() => setSelected(n)}
                className={`rounded-lg py-2 text-sm font-bold transition-colors ${
                  selected === n
                    ? "bg-indigo-500 text-white"
                    : n <= 4  ? "bg-emerald-900/50 text-emerald-300 hover:bg-emerald-800/60"
                    : n <= 7  ? "bg-amber-900/50  text-amber-300  hover:bg-amber-800/60"
                    : "bg-rose-900/50   text-rose-300   hover:bg-rose-800/60"
                }`}
              >{n}</button>
            ))}
          </div>
          <div className="mt-2 flex justify-between text-xs text-slate-600 px-0.5">
            <span>1–4 easy/moderate</span>
            <span>8–10 hard/max</span>
          </div>
        </div>
        {isError && (
          <p className="text-xs text-rose-400">Failed to save — please try again</p>
        )}
        <div className="flex gap-2">
          <button onClick={onClose} disabled={isPending}
            className="flex-1 rounded-xl bg-slate-700 py-2.5 text-sm font-medium text-slate-400 disabled:opacity-50">
            Cancel
          </button>
          <button
            disabled={selected == null || isPending}
            onClick={() => selected != null && onSave(selected)}
            className="flex-1 rounded-xl bg-indigo-600 py-2.5 text-sm font-medium text-white disabled:opacity-40">
            {isPending ? "Saving…" : `Save RPE ${selected ?? ""}`}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Daily checklist ───────────────────────────────────────────────────────────

// A logged My Foods combination (note 35): ONE row with the combination's totals,
// expanding to its parts. The parts stay separate rows in the database — tapping a part
// edits that part; ✕ on the combination removes every part.
function ComboLogRow({ parts, renderPart, onDeleteAll }) {
  const [open, setOpen] = useState(false);
  const base = parts.find((p) => p.combo_part === "base") || parts[0];
  const rest = parts.filter((p) => p !== base);
  const title = parts[0].combo_name
    || [base.name || base.description, ...rest.map((p) => p.name || p.description)].join(" + ");
  const sum = (k) => parts.reduce((a, p) => a + (p[k] ?? 0), 0);
  const hasKcal = parts.some((p) => p.calories != null);
  const hasProt = parts.some((p) => p.protein_g != null);
  return (
    <div>
      <div className="flex items-center gap-2 rounded-lg bg-slate-700/60 px-3 py-2">
        <button className="flex-1 min-w-0 text-left" onClick={() => setOpen((v) => !v)}>
          <p className="text-xs text-white truncate">
            {title.length > 38 ? title.slice(0, 35) + "…" : title}
          </p>
          <p className="text-xs text-slate-500">
            {hasKcal ? `${Math.round(sum("calories"))} kcal` : ""}
            {hasProt ? ` · ${sum("protein_g").toFixed(0)}g protein` : ""}
            <span className="ml-1.5 text-slate-600">{open ? "▾" : "▸"} {parts.length} parts</span>
          </p>
        </button>
        <button
          onClick={() => {
            if (window.confirm(`Delete ${title} (all ${parts.length} parts)?`)) onDeleteAll(parts);
          }}
          className="flex-shrink-0 text-slate-600 hover:text-rose-400 transition-colors text-sm leading-none"
          title="Delete combination"
        >✕</button>
      </div>
      {open && (
        <div className="ml-4 mt-1 space-y-1 border-l border-slate-700 pl-2">
          {[base, ...rest].map(renderPart)}
        </div>
      )}
    </div>
  );
}

function ChecklistItem({ label, sub, onClick }) {
  return (
    <button
      onClick={onClick}
      className="w-full flex items-center gap-3 rounded-lg bg-slate-700/50 px-3 py-2.5 text-left hover:bg-slate-700 transition-colors"
    >
      <span className="w-4 h-4 flex-shrink-0 rounded-full border-2 border-slate-500" />
      <span className="flex-1 text-sm text-white">{label}</span>
      {sub && <span className="text-xs text-slate-500 flex-shrink-0">{sub}</span>}
      <span className="text-xs text-slate-600 flex-shrink-0">→</span>
    </button>
  );
}

// ── Data freshness ────────────────────────────────────────────────────────────

function fmtFreshness(isoStr) {
  if (!isoStr) return null;
  const diffMs = Date.now() - new Date(isoStr).getTime();
  const diffMin = Math.floor(diffMs / 60_000);
  if (diffMin < 2)   return { text: "Synced just now",        cls: "text-emerald-400" };
  if (diffMin < 60)  return { text: `Synced ${diffMin}m ago`, cls: "text-emerald-400" };
  const diffH = Math.floor(diffMin / 60);
  if (diffH < 3)     return { text: `Synced ${diffH}h ago`,   cls: "text-yellow-400" };
  if (diffH < 12)    return { text: `Synced ${diffH}h ago`,   cls: "text-orange-400" };
  return               { text: `No sync in ${diffH}h`,        cls: "text-rose-400" };
}

// ── Status helpers ────────────────────────────────────────────────────────────

function zStatus(z, invertGood = false) {
  if (z == null) return { label: "No data", color: "#64748b" };
  // Label always describes actual z-score direction (what your value is doing)
  const label = z >= 1.0  ? "Well above baseline"
              : z >= 0.0  ? "Above baseline"
              : z >= -1.0 ? "Below baseline"
              :              "Well below baseline";
  // Color encodes goodness — invert sign for metrics where lower is better (e.g. RHR)
  const v = invertGood ? -z : z;
  const color = v >= 1.0  ? "#10b981"
              : v >= 0.0  ? "#6ee7b7"
              : v >= -1.0 ? "#f59e0b"
              :              "#ef4444";
  return { label, color };
}

function sleepDebtStatus(hours) {
  if (hours == null) return { label: "No data", color: "#64748b" };
  if (hours < 0.25) return { label: "On target", color: "#10b981" };
  if (hours < 0.5)  return { label: "Slight deficit", color: "#6ee7b7" };
  if (hours < 1.0)  return { label: "Moderate deficit", color: "#f59e0b" };
  return               { label: "High deficit", color: "#ef4444" };
}

// One rule app-wide (lib/acwr.js, 2026-11-04): Rho eases effort only above 2.0.
function acwrStatus(acwr) {
  const b = acwrBand(acwr);
  if (b == null) return { label: "No data", color: "#64748b" };
  if (b === "very_high") return { label: "Very high load", color: "#ef4444" };
  if (b === "high") return { label: "High load", color: "#f59e0b" };
  return { label: b === "light" ? "Light load" : "Normal load", color: "#10b981" };
}

// ── Activity rings (Apple-style: Move outer, Exercise middle, Stand inner) ───

const STAND_GOAL = 12; // hours — Apple's standard goal

function ActivityRings({ calories, exerciseMins, standHours, steps, calGoal, exerciseGoal, stepsGoal }) {
  const size = 120;
  const cx = size / 2, cy = size / 2;
  const rings = [
    { value: calories,     goal: calGoal,      color: "#f97316", r: 50, sw: 8 }, // Move
    { value: exerciseMins, goal: exerciseGoal,  color: "#22c55e", r: 39, sw: 8 }, // Exercise
    { value: standHours,   goal: STAND_GOAL,    color: "#38bdf8", r: 28, sw: 8 }, // Stand
    { value: steps,        goal: stepsGoal,     color: "#a78bfa", r: 17, sw: 8 }, // Steps
  ];
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="flex-shrink-0">
      {rings.map((ring, i) => {
        const pct = Math.min(1, (ring.value ?? 0) / ring.goal);
        const circ = 2 * Math.PI * ring.r;
        return (
          <g key={i}>
            <circle cx={cx} cy={cy} r={ring.r} fill="none"
              stroke={ring.color} strokeOpacity={0.15} strokeWidth={ring.sw} />
            <circle cx={cx} cy={cy} r={ring.r} fill="none"
              stroke={ring.color} strokeWidth={ring.sw} strokeLinecap="round"
              strokeDasharray={`${circ} ${circ}`}
              strokeDashoffset={circ * (1 - pct)}
              style={{ transform: "rotate(-90deg)", transformOrigin: `${cx}px ${cy}px` }}
            />
          </g>
        );
      })}
    </svg>
  );
}

// ── Mini progress bar ─────────────────────────────────────────────────────────

// Actionable suggestions, ranked server-side (services/nudges). One line each; the top
// four show, the rest expand.
// Anchor-lift targets under Rho's strength hint (note 56). Shown every lifting day: the
// MAINTAIN-day "hold loads" deferral was removed 2026-10-29 (morning signals didn't
// predict his lifting — see training_rec._strength_hint). Easing off is his call; the
// in-session rule below catches a genuinely flat day.
function AnchorTargets() {
  const { data } = useQuery({ queryKey: ["anchors"], queryFn: api.getAnchors, staleTime: 10 * 60 * 1000 });
  const anchors = (data?.anchors ?? []).filter((a) => a.target);
  if (!anchors.length) return null;
  return (
    <div className="mt-2 space-y-0.5 pl-[3.1rem]">
      <p className="text-[8.5px] tracking-micro text-ink-3">ANCHOR TARGETS</p>
      {anchors.map((a) => {
        const t = a.target;
        return (
          <p key={a.exercise} className="text-[11px] tabular-nums text-ink-2">
            <span className="text-ink">{a.short}</span>{" "}
            {t.action === "less_assist" && t.lb === 0 ? "unassisted" : `${t.lb} lb${a.assisted ? " assist" : ""}`} × {t.reps}
          </p>
        );
      })}
      <p className="pt-0.5 text-[10px] text-ink-3">First set misses by 2+ reps → drop one step.</p>
    </div>
  );
}

function NudgeCard() {
  const [expanded, setExpanded] = useState(false);
  const { data: nudges = [] } = useQuery({
    queryKey: ["nudges"],
    queryFn: api.getNudges,
    staleTime: 1000 * 60 * 5,
  });
  if (!nudges.length) return null;

  // Tone drives a left rule rather than a coloured title: the nudge text stays in
  // ink so it reads as a sentence, and urgency is carried by a separate channel.
  const TONE = {
    warn:  "var(--over)",
    focus: "var(--hrv)",
    info:  "var(--ink-3)",
  };
  // One line per item (2026-10-21: the card was too big and too wordy). The
  // server sends `title` + a short `meta` (the numbers); `lines`/`body` are rendered
  // only if a payload still carries them (an older cached response).
  const MAX = 4;
  const shown = expanded ? nudges : nudges.slice(0, MAX);

  return (
    <Card>
      <Eyebrow left="FOCUS" right={nudges.length > 1 ? `${nudges.length} ITEMS` : null} />
      <div className="mt-2 space-y-1.5">
        {shown.map((n) => (
          <div key={n.id} className="flex items-baseline gap-2.5">
            <span className="w-[3px] flex-none self-stretch rounded-full"
              style={{ backgroundColor: TONE[n.tone] ?? TONE.info }} />
            <div className="min-w-0 flex-1">
              <div className="flex items-baseline gap-2">
                <p className="prose-ui min-w-0 flex-1 truncate text-[12.5px] font-semibold leading-snug text-ink">{n.title}</p>
                {n.meta && (
                  <span className="flex-none text-[10.5px] tabular-nums text-ink-3">{n.meta}</span>
                )}
              </div>
              {n.lines?.length ? (
                <p className="prose-ui truncate text-[11px] leading-snug text-ink-3">{n.lines.join(" · ")}</p>
              ) : n.body ? (
                <p className="prose-ui truncate text-[11px] leading-snug text-ink-3">{n.body}</p>
              ) : null}
            </div>
          </div>
        ))}
      </div>
      {nudges.length > MAX && (
        <button onClick={() => setExpanded((v) => !v)}
          className="mt-2 text-[10px] font-semibold tracking-micro text-ink-3 hover:text-ink-2">
          {expanded ? "SHOW LESS" : `${nudges.length - MAX} MORE →`}
        </button>
      )}
    </Card>
  );
}

function MiniBar({ label, value, goal, color, displayValue, goalDisplay, suffix = "" }) {
  const pct = value != null ? Math.min(100, Math.round((value / goal) * 100)) : 0;
  const hasData = value != null;
  return (
    <div>
      <div className="flex justify-between text-xs mb-1">
        <span style={{ color }} className="font-medium">{label}</span>
        <span className={hasData ? "text-white font-semibold" : "text-slate-600"}>
          {hasData ? (displayValue ?? value) : "—"}
          <span className="text-slate-500 font-normal"> / {goalDisplay ?? goal}{suffix}</span>
        </span>
      </div>
      <div className="h-1.5 rounded-full bg-slate-700">
        <div className="h-1.5 rounded-full transition-all" style={{ width: `${pct}%`, backgroundColor: hasData ? color : "#1e293b" }} />
      </div>
    </div>
  );
}

// ── CalorieBalanceBar ─────────────────────────────────────────────────────────
// Bar always shows intake vs. estimated Out (RMR + active_cals) when available.
//
// mode="fixed":   Out bar fills; a tick marks the fixed calorie goal.
// mode="balance": Out bar fills; surplus/deficit vs. Out is the signal. No tick.
// mode="pace":    Out bar fills; tick marks Out + offset (the intake target).
//
// Falls back to intake-only display when Out isn't available (no profile / no sync).
/**
 * Energy for the day, as one bar you read left to right.
 *
 * The track is the day's projected Out; the fill is what's gone In. So the *empty*
 * part of the bar is literally the room left before you hit maintenance — the thing
 * worth knowing at 3pm — and the goal tick shows where you meant to land. The old
 * version put In/Out/Net on a text line above a bar that duplicated the same
 * relationship; this states it once, geometrically.
 *
 * Net is a chip on the right, coloured by state tokens only (over/under/balanced),
 * never by the series colour of the fill.
 */
function CalorieBalanceBar({ intake, out, goal, offset = 0, mode = "fixed", mean = null }) {
  const inK = Math.round(intake || 0);

  if (out != null) {
    const net = inK - out;
    const netColor = net < -100 ? "var(--under)" : net > 100 ? "var(--over)" : "var(--breakfast)";
    const netLabel = net < -100 ? "deficit" : net > 100 ? "surplus" : "balanced";
    const pct = Math.min(100, (inK / out) * 100);

    // fixed mode: tick at the goal. pace mode: tick at out + offset (target intake).
    let tickVal = null, tickText = null;
    if (mode === "fixed" && goal) {
      tickVal = goal; tickText = `goal ${goal.toLocaleString()}`;
    } else if (mode === "pace" && offset !== 0) {
      tickVal = out + offset; tickText = `target ${(out + offset).toLocaleString()}`;
    }
    const tickPct = tickVal != null ? Math.min(100, Math.max(0, (tickVal / out) * 100)) : null;
    const room = out - inK;
    // The 30-day mean sits on the same scale as everything else in the track.
    // DASHED where the goal tick is SOLID: one is a target you chose, the other is
    // what you actually do, and the two must never be mistaken for each other.
    const atOut = (v) => Math.min(100, Math.max(0, (v / out) * 100));
    const meanPct = mean?.value != null ? atOut(mean.value) : null;
    const bandLo = mean?.lo != null ? atOut(mean.lo) : null;
    const bandHi = mean?.hi != null ? atOut(mean.hi) : null;

    return (
      <div>
        <div className="relative h-[30px] overflow-hidden rounded-md bg-surface">
          <div className="absolute inset-y-0 left-0 transition-[width] duration-300"
            style={{ width: `${pct}%`, backgroundColor: "var(--breakfast)", opacity: 0.55 }} />
          {tickPct != null && (
            <div className="absolute inset-y-0 w-px bg-ink-2" style={{ left: `${tickPct}%` }} title={tickText} />
          )}
          {bandLo != null && bandHi != null && bandHi > bandLo && (
            <div className="absolute inset-y-0 bg-ink/[0.13]"
              style={{ left: `${bandLo}%`, width: `${bandHi - bandLo}%` }}
              title={`typical ${mean.lo.toLocaleString()}–${mean.hi.toLocaleString()} kcal`} />
          )}
          {meanPct != null && (
            <div className="absolute inset-y-0 w-px"
              style={{
                left: `${meanPct}%`,
                // A 1px element with `border-dashed` renders as a solid hairline in
                // WebKit, so the dashes come from a repeating gradient instead.
                backgroundImage:
                  "repeating-linear-gradient(to bottom, var(--ink) 0 3px, transparent 3px 6px)",
                opacity: 0.9,
              }}
              title={`30-day mean ${mean.value.toLocaleString()} kcal · ${mean.n} complete days`} />
          )}
          <div className="relative flex h-full items-center justify-between px-2">
            <span className="text-[10px] font-semibold tracking-micro text-ink">
              IN <span className="text-[12px]">{inK.toLocaleString()}</span>
            </span>
            <span className="text-[10px] tracking-micro text-ink-2">
              OUT <span className="text-[12px] font-semibold text-ink">{out.toLocaleString()}</span>
            </span>
          </div>
        </div>
        <div className="mt-1 flex items-baseline justify-between gap-2 text-[9.5px] leading-none">
          <span className="min-w-0 truncate text-ink-3">
            {tickText && `▲ ${tickText}`}
            {tickText && mean?.value != null && " · "}
            {mean?.value != null &&
              `┊ mean ${mean.value.toLocaleString()} · usual ${mean.lo.toLocaleString()}–${mean.hi.toLocaleString()}`}
          </span>
          <span className="flex-none font-semibold" style={{ color: netColor }}>
            {net > 0 ? "+" : ""}{net.toLocaleString()} {netLabel}
            {room > 0 && <span className="font-normal text-ink-3"> · {room.toLocaleString()} room</span>}
          </span>
        </div>
      </div>
    );
  }

  // No Out available (no profile or no Apple Health data) — fall back to the fixed goal.
  if (mode === "fixed" && goal) {
    const bal = inK - goal;
    const over = bal >= 0;
    return (
      <TrackBar label="ENERGY" value={inK} goal={goal} unit=" kcal"
        color="var(--breakfast)" displayValue={inK.toLocaleString()}
        sub={`${over ? "+" : ""}${bal.toLocaleString()} kcal ${over ? "surplus" : "deficit"} vs goal`} />
    );
  }

  return (
    <p className="text-[11px] text-ink-2">
      In <span className="font-semibold text-ink">{inK.toLocaleString()}</span> kcal
    </p>
  );
}

// ── MacroSplit ────────────────────────────────────────────────────────────────
// Protein / carbs / fat as a share of total calories. Shows balance at a glance
// without inventing gram goals.
/**
 * Macro split as one labelled bar instead of a bar plus three legend rows.
 *
 * Each segment names itself and prints its own share, so the legend disappears —
 * four lines become one. The 30-day mean rides underneath as a second, much thinner
 * bar: comparison by vertical alignment, which reads instantly, rather than by
 * scanning "· 30d 34%" suffixes across three rows.
 */
// The bedtime residual: how much caffeine/alcohol is still on board at sleep onset.
//
// This is the quantity the correlation engine actually uses, and until 2026-08-30 it
// was visible nowhere in the app — it existed only inside correlations, where the
// caffeine version had also been silently reading 0.0 forever. The day
// total above it answers "how much did I have"; this answers "how much is still in me
// when I try to sleep", which is the one that moves sleep.
//
// A PROJECTED value is marked with ~: tonight's bedtime hasn't happened yet, so it is
// estimated from the recent median and will keep moving as the evening goes on and as
// more gets logged. A settled value never changes. Rendering both identically would
// invite reading tonight's guess as a measurement.
function AtBedtime({ value, projected, format }) {
  if (value == null) return null;
  return (
    <p
      className="mt-1 text-[9px] leading-none text-ink-3"
      title={projected
        ? "Projected against your recent median bedtime — moves as the evening goes on"
        : "Measured against your recorded bedtime"}
    >
      {projected ? "~" : ""}{format(value)} <span className="opacity-70">at bed</span>
    </p>
  );
}

function MacroSplit({ protein, carbs, fat, avg }) {
  const pC = (protein || 0) * 4, cC = (carbs || 0) * 4, fC = (fat || 0) * 9;
  const tot = pC + cC + fC;
  if (tot <= 0) return null;
  const pp = Math.round((pC / tot) * 100);
  const cp = Math.round((cC / tot) * 100);
  const fp = 100 - pp - cp;

  const segs = [
    { key: "p", label: `P ${pp}%`, pct: pp, color: "var(--rhr)",    title: `Protein ${Math.round(protein)} g · ${pp}% of kcal` },
    { key: "c", label: `C ${cp}%`, pct: cp, color: "var(--dinner)", title: `Carbs ${Math.round(carbs)} g · ${cp}% of kcal` },
    { key: "f", label: `F ${fp}%`, pct: fp, color: "var(--breakfast)", title: `Fat ${Math.round(fat)} g · ${fp}% of kcal` },
  ];
  const hasAvg = avg?.pp != null && avg?.cp != null && avg?.fp != null;

  return (
    <div>
      <StackBar segments={segs} height={22} />
      {hasAvg && (
        <>
          {/* Same geometry, quarter height: today vs habit is a shape comparison. */}
          <StackBar className="mt-[3px] opacity-45" height={5} minLabel={999}
            segments={[
              { key: "p", label: "", pct: avg.pp, color: "var(--rhr)" },
              { key: "c", label: "", pct: avg.cp, color: "var(--dinner)" },
              { key: "f", label: "", pct: avg.fp, color: "var(--breakfast)" },
            ]} />
          <p className="mt-0.5 text-[9.5px] leading-none text-ink-3">
            30-day mean below · {Math.round(protein)}P {Math.round(carbs)}C {Math.round(fat)}F g
          </p>
        </>
      )}
      {!hasAvg && (
        <p className="mt-0.5 text-[9.5px] leading-none text-ink-3">
          {Math.round(protein)}P {Math.round(carbs)}C {Math.round(fat)}F g · % of kcal
        </p>
      )}
    </div>
  );
}

// ── StatRow ───────────────────────────────────────────────────────────────────

// Compact stat row shown beneath the Readiness / Activity rings.
// Readiness rows pass onClick (opens the info modal); activity rows pass a dotColor.
function StatRow({ dotColor, label, value, valueColor, onClick }) {
  const inner = (
    <>
      {dotColor && <span className="w-2 h-2 rounded-full flex-shrink-0" style={{ backgroundColor: dotColor }} />}
      <span className="text-slate-400 truncate">{label}</span>
      <span className="ml-auto font-semibold flex-shrink-0" style={{ color: valueColor ?? "#e2e8f0" }}>{value}</span>
    </>
  );
  return onClick
    ? <button onClick={onClick} className="flex items-center gap-1.5 text-xs w-full text-left">{inner}</button>
    : <div className="flex items-center gap-1.5 text-xs w-full">{inner}</div>;
}

// Expanded illness/vitals view, opened from the one-line Recovery status.
// Vitals reuse the calibrated recovery_alarm markers (HRV, RHR, skin temp, resp
// rate); blood oxygen / breathing / walking HR come from recovery_signals and are
// contextual only — they do NOT feed the composite status.
const RECOVERY_LEVEL_META = {
  clear:    { label: "Vitals clear", color: "#10b981" },
  watch:    { label: "Vitals watch", color: "#f59e0b" },
  elevated: { label: "Vitals alert", color: "#ef4444" },
};

function RecoverySignalsModal({ alarm, signals, onClose }) {
  const level = alarm?.level ?? "clear";
  const lm = RECOVERY_LEVEL_META[level] ?? RECOVERY_LEVEL_META.clear;
  const rs = signals ?? {};

  const markerRows = (alarm?.markers ?? []).map((m) => ({
    key: m.key,
    label: m.label.replace(/ (suppressed|elevated)$/i, ""),
    display: m.available ? m.detail : "—",
    color: m.flagged ? "#f59e0b" : m.available ? "#e2e8f0" : "#475569",
  }));

  const spo2 = rs.blood_oxygen;
  const breathing = rs.breathing_disturbances;
  const walkHR = rs.walking_heart_rate_avg;
  const extraRows = [
    { key: "spo2", label: "Blood oxygen",
      display: spo2 == null ? "—" : `${Math.round(spo2 <= 1 ? spo2 * 100 : spo2)}%`,
      color: spo2 == null ? "#475569" : "#e2e8f0" },
    { key: "breathing", label: "Breathing disturb.",
      display: breathing == null ? "—" : breathing.toFixed(1),
      color: breathing == null ? "#475569" : "#e2e8f0" },
    { key: "walk_hr", label: "Walking HR",
      display: walkHR ? `${Math.round(walkHR.value)} bpm` : "—",
      color: walkHR ? "#e2e8f0" : "#475569" },
  ];

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/60 px-4 pb-6" onClick={onClose}>
      <div className="w-full max-w-md rounded-2xl bg-slate-800 p-5 space-y-4" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <p className="text-base font-semibold text-white">Recovery Signals</p>
            <span className="rounded-md px-2 py-0.5 text-xs font-bold tracking-wider"
              style={{ backgroundColor: lm.color + "22", color: lm.color }}>{lm.label}</span>
          </div>
          <button onClick={onClose} className="text-slate-500 text-lg leading-none">✕</button>
        </div>

        <div className="space-y-2">
          {[...markerRows, ...extraRows].map((r) => (
            <div key={r.key} className="flex items-baseline justify-between">
              <span className="text-sm text-slate-400">{r.label}</span>
              <span className="text-sm font-semibold" style={{ color: r.color }}>{r.display}</span>
            </div>
          ))}
        </div>

        {level === "elevated" && (
          <p className="text-xs text-slate-400 leading-snug border-t border-slate-700 pt-3">
            Multiple markers are off together — a common early signal of illness or overreaching.
            Consider easier training and extra rest today.
          </p>
        )}
        <p className="text-[11px] text-slate-500 leading-snug">
          Blood oxygen, breathing, and walking HR are shown for context and don't feed the status above.
        </p>
      </div>
    </div>
  );
}

// ── Sleep summary ─────────────────────────────────────────────────────────────

/**
 * Last night, compressed to the one thing that gets read: the total.
 *
 * Was a tall card — a heading, a 2xl total, a stage bar, and a four-column legend
 * printing every stage twice (label + duration). The user only reads the headline, so
 * the stage split now rides *inside* the bar as inline labels and the whole card is
 * one row plus a bar.
 *
 * BUG FIXED 2026-08-15: this navigated to `onNavigate("sleep")`, and there is no
 * "sleep" tab — TAB_COMPONENTS is today/training/trends/body/recovery/habits, and
 * Sleep.jsx has been orphaned since the 2026-07-03 cleanup. So the button silently
 * did nothing. Sleep content lives on Recovery; that's where it goes now.
 *
 * Stage ORDER is deliberately not implied — the bar is a composition, not a
 * timeline. Apple exports nightly totals only (no hypnogram).
 */
function SleepSummary({ sleep, onNavigate }) {
  if (!sleep) return null;
  const stages = [
    { label: "Deep",  min: sleep.deep_min,  color: "var(--deep)" },
    { label: "Core",  min: sleep.core_min,  color: "var(--core)" },
    { label: "REM",   min: sleep.rem_min,   color: "var(--rem)" },
    { label: "Awake", min: sleep.awake_min, color: "var(--awake)" },
  ];
  const total = stages.reduce((a, s) => a + (s.min || 0), 0) || 1;
  return (
    <button className="w-full rounded-2xl border border-rule bg-card p-3 text-left"
      onClick={() => onNavigate("recovery")}>
      <div className="flex items-baseline gap-2.5">
        <span className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">LAST NIGHT</span>
        <span className="text-[17px] font-semibold leading-none">{fmtMinutes(sleep.total_min)}</span>
        <span className="ml-auto text-[10px] tracking-micro text-ink-3">RECOVERY →</span>
      </div>
      {/* Inline labels replace the old four-column legend: a segment wide enough to
          hold its name says it in place, narrow ones stay unlabelled and are
          available on the Recovery tab. */}
      <div className="mt-2 flex h-[18px] gap-[2px] overflow-hidden rounded-[3px]">
        {stages.map((s) => {
          const pct = (s.min || 0) / total * 100;
          if (!s.min) return null;
          return (
            <div key={s.label} className="flex items-center justify-center overflow-hidden"
              style={{ flex: s.min, backgroundColor: s.color }}>
              {pct >= 14 && (
                <span className="truncate px-1 text-[9px] font-medium leading-none text-ink">
                  {s.label} {fmtMinutes(s.min)}
                </span>
              )}
            </div>
          );
        })}
      </div>
    </button>
  );
}

// ── Main ──────────────────────────────────────────────────────────────────────

// ── Outdoor conditions (Open-Meteo) ──────────────────────────────────────────
const _WX_ICON = {
  0: "☀️", 1: "🌤️", 2: "⛅", 3: "☁️", 45: "🌫️", 48: "🌫️",
  51: "🌦️", 53: "🌦️", 55: "🌧️", 56: "🌧️", 57: "🌧️",
  61: "🌧️", 63: "🌧️", 65: "🌧️", 66: "🌧️", 67: "🌧️",
  71: "🌨️", 73: "🌨️", 75: "❄️", 77: "🌨️",
  80: "🌦️", 81: "🌧️", 82: "⛈️", 85: "🌨️", 86: "❄️",
  95: "⛈️", 96: "⛈️", 99: "⛈️",
};
// AQI and UV keep their own ramps rather than the over/under state tokens: both
// are published multi-band public-health scales, not this app's judgement, and
// collapsing EPA's six categories to two would discard the category names the
// badge prints. Retuned to sit with the new palette; the thresholds are unchanged.
function aqiColor(a) {
  if (a == null) return "var(--ink-2)";
  if (a <= 50) return "#4FB286";
  if (a <= 100) return "#E9A13B";
  if (a <= 150) return "#E2874C";
  if (a <= 200) return "#E2574C";
  if (a <= 300) return "#A87CC8";
  return "#8E4A5E";
}
function uvColor(u) {
  if (u == null) return "var(--ink-2)";
  if (u < 3) return "#4FB286";
  if (u < 6) return "#E9A13B";
  if (u < 8) return "#E2874C";
  if (u < 11) return "#E2574C";
  return "#A87CC8";
}

function fmtClock(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (isNaN(d)) return null;
  let h = d.getHours();
  const m = String(d.getMinutes()).padStart(2, "0");
  const ap = h >= 12 ? "p" : "a";
  h = h % 12 || 12;
  return `${h}:${m}${ap}`;
}

function WeatherCard({ morningLight }) {
  const q = useQuery({
    queryKey: ["weather"],
    queryFn: api.getWeather,
    staleTime: 10 * 60 * 1000,      // server caches too; don't hammer on every focus
    refetchOnWindowFocus: false,
  });
  const { data } = q;
  // A failure says so (note 53); only "not configured" (null) still hides the strip.
  // No loading line — a one-row strip flashing "Loading…" is noise, not signal.
  if (q.isError || data?.available === false) {
    return (
      <Card className="!py-3">
        <EmptyState query={q} className="!mt-0"
          message={`Weather unavailable — ${data?.reason ?? "no reason given"}. It retries on the next refresh.`} />
      </Card>
    );
  }
  if (!data || data.temp == null) return null;   // not configured → no strip
  const sunrise = fmtClock(data.sunrise);
  const sunset = fmtClock(data.sunset);
  const icon = _WX_ICON[data.weather_code] ?? (data.is_day ? "☀️" : "🌙");
  const arrow = data.pressure_trend === "rising" ? "↑"
              : data.pressure_trend === "falling" ? "↓" : "→";
  const ac = aqiColor(data.aqi);

  // Ambient context, deliberately compressed. This card used to open the tab as a
  // full hero block — three stacked rows and a 3-up tile grid — which gave the
  // least actionable thing on the page the most valuable slot. Same information,
  // one strip: nothing is dropped, it just stops competing with the coach.
  const Stat = ({ label, value, color }) => (
    <span className="flex items-baseline gap-1">
      <span className="text-[9px] tracking-micro text-ink-3">{label}</span>
      <span className="text-[12px] font-semibold" style={color ? { color } : undefined}>
        {value}
      </span>
    </span>
  );

  return (
    <Card className="!py-3">
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
        <span className="text-xl leading-none">{icon}</span>
        <span className="text-[19px] font-semibold leading-none">{Math.round(data.temp)}°</span>
        <span className="prose-ui min-w-0 truncate text-[11.5px] text-ink-2">
          {data.condition}
          {data.feels_like != null && ` · feels ${Math.round(data.feels_like)}°`}
          {data.temp_max != null && ` · H${Math.round(data.temp_max)}° L${Math.round(data.temp_min)}°`}
        </span>
        {data.aqi != null && (
          <span className="ml-auto flex max-w-full items-baseline gap-1 rounded-md px-2 py-1"
            style={{ background: `color-mix(in srgb, ${ac} 12%, transparent)` }}>
            <span className="text-[12px] font-semibold leading-none" style={{ color: ac }}>
              {Math.round(data.aqi)}
            </span>
            <span className="text-[9px] leading-none" style={{ color: ac }}>{data.aqi_label}</span>
            {/* Provenance matters: a modelled AQI and a ground-monitor AQI can
                differ ~2x during wildfire smoke. */}
            <span className="text-[8.5px] leading-none text-ink-3">
              {data.aqi_source === "airnow" ? `${data.aqi_pollutant || "AQI"}·EPA` : "modelled"}
            </span>
          </span>
        )}
      </div>

      {/* UV and barometric pressure stay prominent — both are health-relevant and
          both are wired as correlation inputs. */}
      <Well className="mt-2.5 !px-3 !py-2">
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1.5">
          <Stat label="UV" color={uvColor(data.uv)}
            value={`${data.uv != null ? data.uv.toFixed(1) : "—"}${data.uv_label ? ` ${data.uv_label}` : ""}`} />
          <Stat label={`hPa ${arrow}`} value={data.pressure != null ? Math.round(data.pressure) : "—"} />
          <Stat label="RH" value={data.humidity != null ? `${Math.round(data.humidity)}%` : "—"} />
          {sunrise && (
            <Stat label="SUN" value={`${sunrise}${sunset ? `–${sunset}` : ""}`} />
          )}
          {/* Morning-daylight habit, auto-credited from Apple time_in_daylight
              inside the sunrise-relative window. */}
          {morningLight != null && (
            <span className="ml-auto text-[10.5px]"
              style={{ color: morningLight ? "var(--rhr)" : "var(--ink-3)" }}>
              {morningLight ? "Morning light ✓" : "No morning light yet"}
            </span>
          )}
        </div>
      </Well>
    </Card>
  );
}


export default function Today({ onNavigate }) {
  // Today splits into two views behind one nav tab. "State" is read-mostly — what
  // your body is doing and what to do about it. "Log" is write-mostly — everything
  // that exists to capture input. Both were previously one 15-card scroll where a
  // glance at recovery meant scrolling past the meal log. Kept in local state (not
  // the URL) because there's no router; the tab resets to State on remount, which
  // is the right default for a glance.
  // Opens on the view used last (2026-10-21): LOG was 201 of 325 Today visits, so
  // always starting on STATE cost an extra tap on most opens. Storage can be
  // unavailable (private mode) — fall back to STATE.
  const [view, setViewRaw] = useState(() => {
    try { return localStorage.getItem("today_view") === "log" ? "log" : "state"; } catch { return "state"; }
  });
  const setView = (v) => {
    setViewRaw(v);
    try { localStorage.setItem("today_view", v); } catch { /* ignore */ }
  };
  useEffect(() => { api.logView(`today/${view}`); }, [view]);
  // The character sheet and the battery share a 2-up row while collapsed. Either
  // one expanding has to break the row to full width — a radar chart or a PVT tap
  // target rendered into ~170px is unusable.
  const [sheetOpen, setSheetOpen] = useState(false);
  const [batteryOpen, setBatteryOpen] = useState(false);
  const pairExpanded = sheetOpen || batteryOpen;
  const [infoMetric, setInfoMetric]         = useState(null);
  const [showLogModal, setShowLogModal]     = useState(false);
  const [reviewJob, setReviewJob] = useState(null);   // a background parse being reviewed
  const [defaultMeal, setDefaultMeal]       = useState("breakfast");
  const [selectedLog, setSelectedLog]       = useState(null);
  const [rpeTarget, setRpeTarget]           = useState(null);
  const [showCheckinModal, setShowCheckinModal] = useState(false);
  const [showHealthDetail, setShowHealthDetail] = useState(false);
  const [showRecovery, setShowRecovery]     = useState(false);
  const [showFlagCalendar, setShowFlagCalendar] = useState(false);
  const [showTrips, setShowTrips] = useState(false);
  const [comboMeal, setComboMeal] = useState(null);   // a logged meal → one My Food
  // Note 102: after a trip, ask once what wasn't logged (until it's answered, for a week).
  const { data: tripsData } = useQuery({ queryKey: ["trips"], queryFn: api.getTrips, staleTime: 10 * 60 * 1000 });
  const [showBoard, setShowBoard] = useState(false);
  const qc = useQueryClient();

  const { data: todayData, isLoading } = useQuery({ queryKey: ["today"], queryFn: api.today });
  // Re-checked every 15 min so the row leaves once class is over without a reload.
  const { data: gymToday } = useQuery({ queryKey: ["gymToday"], queryFn: api.getGymToday,
                                        refetchInterval: 15 * 60 * 1000 });

  const deleteLogMutation = useMutation({
    mutationFn: (id) => api.deleteNutritionLog(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["nutrition-logs"] });   // both logDate + today
      qc.invalidateQueries({ queryKey: ["nutrition-daily"] });
    },
  });

  const skipMealMutation = useMutation({
    mutationFn: (meal) => api.skipMeal(logDate, meal),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["nutrition-logs"] });   // updates sections + today's reminders
      qc.invalidateQueries({ queryKey: ["nutrition-daily"] });
    },
  });

  const rpeMutation = useMutation({
    mutationFn: ({ id, rpe }) => api.setWorkoutRpe(id, rpe),
    onSuccess: () => {
      setRpeTarget(null);
      qc.invalidateQueries({ queryKey: ["workouts-recent"] });
      qc.invalidateQueries({ queryKey: ["workouts"] });   // also refresh Training tab history
      qc.invalidateQueries({ queryKey: ["today"] });
    },
    onError: (err) => {
      console.error("RPE save failed:", err);
      // keep modal open so user sees the error — displayed via rpeMutation.isError
    },
  });

  const { data: settings } = useQuery({ queryKey: ["settings"], queryFn: api.getSettings });
  const { data: streaks = {} } = useQuery({
    queryKey: ["streaks"],
    queryFn:  api.getStreaks,
    staleTime: 60_000,
  });

  const todayStr = localToday();
  // The LOG bar opens on the FOOD day (utils.foodToday): until 04:00 that is the day
  // that just ended, so a 01:30 snack lands on it (2026-10-21).
  const [logDate, setLogDate] = useState(() => foodToday());

  // Habit log — always today's date
  // Weekly tape reminder (2026-09-12). Same key AND fetcher as Body's TAPE card so the
  // two share one cache entry instead of fighting over it.
  const { data: tapeRows = [] } = useQuery({
    queryKey: ["measurements"],
    queryFn: () => api.getMeasurements(365),
  });
  const tapeLastDate = tapeRows[0]?.date;
  const tapeDaysAgo = tapeLastDate
    ? Math.round((new Date(todayStr + "T12:00:00") - new Date(tapeLastDate + "T12:00:00")) / 86400000)
    : null;
  const tapeDue = tapeDaysAgo == null || tapeDaysAgo >= 7;

  const { data: habits } = useQuery({
    queryKey: ["habits", todayStr],
    queryFn: () => api.getHabits(todayStr),
  });
  // The LOG view's habits follow the one LOG date (2026-10-21); `habits` above stays
  // TODAY's for the STATE view (weather card's morning-light badge).
  const { data: habitsOther } = useQuery({
    queryKey: ["habits", logDate],
    queryFn: () => api.getHabits(logDate),
    enabled: logDate !== todayStr,
  });
  const logHabits = logDate === todayStr ? habits : habitsOther;
  const habitMutation = useMutation({
    mutationFn: (data) => api.logHabits(data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["habits"] }),
  });
  const [habitExpanded, setHabitExpanded] = useState(false);
  const toggleHabit = (key) => {
    const current = logHabits?.[key];
    // null → true → false → null (explicit null clears the field)
    const next = current === true ? false : current === false ? null : true;
    habitMutation.mutate({ date: logDate, [key]: next });
  };
  // Optional continuous companions: log the time for caffeine/meditation. The
  // backend derives the boolean from the time, so this also marks the habit done.
  const HABIT_TIME_FIELD = { no_late_caffeine: "caffeine_last_min", meditation: "meditation_min" };
  const minToHHMM = (m) => m == null ? "" :
    `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
  const setHabitTime = (field, hhmm) => {
    const minutes = hhmm ? (Number(hhmm.slice(0, 2)) * 60 + Number(hhmm.slice(3, 5))) : null;
    habitMutation.mutate({ date: logDate, [field]: minutes });
  };
  const { data: sleepData } = useQuery({
    queryKey: ["sleep-recent"],
    queryFn: () => {
      const end = new Date().toISOString().slice(0, 10);
      const start = new Date(Date.now() - 2 * 86400000).toISOString().slice(0, 10);
      return api.sleep(start, end);
    },
  });
  const { data: recentWorkouts = [] } = useQuery({
    queryKey: ["workouts-recent"],
    queryFn: () => api.workouts(5),
  });

  // Logs for the currently selected date (shown in meal sections)
  const { data: nutritionLogs = [] } = useQuery({
    queryKey: ["nutrition-logs", logDate],
    queryFn: () => api.getNutritionLogs(logDate),
  });

  // Always fetch today's logs separately so the checklist can check today's meals
  // (shares cache with nutritionLogs when logDate === todayStr — no extra request)
  const { data: todayLogs = [] } = useQuery({
    queryKey: ["nutrition-logs", todayStr],
    queryFn: () => api.getNutritionLogs(todayStr),
  });

  // Past days with incomplete meal logs (drives the backfill reminder)
  const { data: incompleteData } = useQuery({
    queryKey: ["nutrition-incomplete"],
    queryFn: () => api.getIncompleteDays(14),
  });
  const incompleteDays = incompleteData?.incomplete || [];


  const skipDayMutation = useMutation({
    mutationFn: (date) => api.skipDay(date),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["nutrition-incomplete"] });
      qc.invalidateQueries({ queryKey: ["day-flags"] });
    },
  });
  const unskipDayMutation = useMutation({
    mutationFn: (date) => api.unskipDay(date),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["nutrition-incomplete"] });
      qc.invalidateQueries({ queryKey: ["day-flags"] });
    },
  });

  // Flags for the day being viewed (to detect a skip-acknowledged past day)
  const { data: viewedDayFlags } = useQuery({
    queryKey: ["day-flags", logDate],
    queryFn: () => api.getDayFlags(logDate, logDate),
    enabled: logDate !== todayStr,
  });
  const viewedDaySkipped = (viewedDayFlags?.flags || [])
    .some((f) => f.reason === "incomplete_data" && f.source === "manual");

  // Activity data for past dates (today's comes from todayData.activity)
  const { data: pastActivity } = useQuery({
    queryKey: ["activity-for-date", logDate],
    queryFn: () => api.getActivityForDate(logDate),
    enabled: logDate !== todayStr,
  });

  // 30-day macro history → average split (% of kcal) shown under the macro bar
  const { data: nutrition30 = [] } = useQuery({
    queryKey: ["nutrition-daily-30", todayStr],
    queryFn: () => api.getNutritionDaily(addDays(todayStr, -30), todayStr),
  });
  const avgMacroSplit = useMemo(() => {
    let p = 0, c = 0, f = 0;
    for (const d of nutrition30) { p += d.protein_g || 0; c += d.carbs_g || 0; f += d.fat_g || 0; }
    const pc = p * 4, cc = c * 4, fc = f * 9, tot = pc + cc + fc;
    if (tot <= 0) return null;
    const pp = Math.round((pc / tot) * 100);
    const cp = Math.round((cc / tot) * 100);
    return { pp, cp, fp: 100 - pp - cp };
  }, [nutrition30]);

  // ── 30-day mean intake, for the reference line on the energy bar ─────────────
  //
  // /nutrition/daily GROUPs over logged rows, so a day where only breakfast got
  // recorded comes back as a real ~400 kcal day and would drag the mean down. The
  // app already has the canonical answer to "is this day's nutrition trustworthy":
  // the auto `incomplete_data` DayFlag, which is exactly what the backend's own
  // nutrition-consistency endpoint filters on. Same rule here, so the line means
  // "your typical COMPLETE day" rather than "your typical row in the table".
  //
  // Today is excluded too — it's still in progress, and including a half-eaten day
  // in its own reference line would drag the line toward the bar.
  const { data: flags30 } = useQuery({
    queryKey: ["day-flags-30", todayStr],
    queryFn: () => api.getDayFlags(addDays(todayStr, -30), todayStr),
    staleTime: 1000 * 60 * 30,
  });
  // Mean AND spread, per nutrient. A bare mean line invites "am I above or below
  // average today?", and against this data that comparison is mostly noise —
  // measured over 90 complete days the coefficient of variation is 17% for
  // calories, 22% for protein, 39% for fiber, so only 23% of fiber days land
  // within ±15% of their own mean. The p25–p75 band is what makes the mean
  // readable: inside the band is an ordinary day, outside it is a real one.
  const nutritionRefs = useMemo(() => {
    const excluded = new Set(
      (flags30?.flags ?? [])
        .filter((f) => f.reason === "incomplete_data")
        .map((f) => f.date)
    );
    const days = nutrition30.filter((d) => d.date !== todayStr && !excluded.has(d.date));
    const pct = (sorted, p) => sorted[Math.min(sorted.length - 1, Math.floor(p * sorted.length))];
    const stat = (key) => {
      const v = days.map((d) => d[key]).filter((x) => x != null).sort((a, b) => a - b);
      if (v.length < 5) return null;    // too thin to be a reference
      return {
        value: Math.round(v.reduce((s, x) => s + x, 0) / v.length),
        lo: Math.round(pct(v, 0.25)),
        hi: Math.round(pct(v, 0.75)),
        n: v.length,
      };
    };
    return { calories: stat("calories"), protein: stat("protein_g"), fiber: stat("fiber_g") };
  }, [nutrition30, flags30, todayStr]);
  const meanIntake = nutritionRefs.calories;

  const metrics  = todayData?.metrics;
  const weight   = todayData?.latest_weight;
  const activity = todayData?.activity;
  const syncState = fmtFreshness(todayData?.last_sync_at);
  const dataHealth = todayData?.data_health;
  const readinessComplete = dataHealth?.readiness_complete;
  // Demote to amber if readiness inputs are incomplete but sync is otherwise fresh
  const dotCls = syncState
    ? (readinessComplete === false && syncState.cls === "text-emerald-400"
        ? "text-yellow-400"
        : syncState.cls)
    : null;
  const freshness = syncState ? { ...syncState, cls: dotCls } : null;
  const lastSleep     = sleepData?.slice(-1)[0] ?? null;

  // Today's nutrition totals
  const dayTotals = nutritionLogs.reduce(
    (acc, l) => ({
      calories: acc.calories + (l.calories  || 0),
      protein:  acc.protein  + (l.protein_g || 0),
      carbs:    acc.carbs    + (l.carbs_g   || 0),
      fat:      acc.fat      + (l.fat_g     || 0),
      fiber:    acc.fiber    + (l.fiber_g   || 0),
      sodium:   acc.sodium   + (l.sodium_mg || 0),
      // Calories the parser deliberately did NOT count — unnamed spreads, dressings,
      // sides. Summed separately and never added into `calories`; the whole point of
      // migration 0059 is that the exclusion stays visible instead of being folded in.
      omitted:  acc.omitted  + (l.omitted_kcal || 0),
      // Coverage, because the sum alone is ambiguous: a day of zeros and a day of
      // nulls both total 0. Skipped meals are excluded — a known zero is not an
      // un-assessed item, and counting it as one would permanently peg coverage
      // below 100%.
      nItems:   acc.nItems   + (l.skipped ? 0 : 1),
      nAssessed: acc.nAssessed + (!l.skipped && l.confidence ? 1 : 0),
    }),
    { calories: 0, protein: 0, carbs: 0, fat: 0, fiber: 0, sodium: 0,
      omitted: 0, nItems: 0, nAssessed: 0 },
  );
  // keep nutTotals alias so existing references (MiniBar progress) still work
  const nutTotals = dayTotals;
  const hasNutrition = nutritionLogs.length > 0;

  // Always-today protein total for the nudge card — separate from dayTotals which
  // follows logDate (may be a past date when user is browsing history).
  const todayProtein = useMemo(
    () => todayLogs.reduce((acc, l) => acc + (l.protein_g || 0), 0),
    [todayLogs],
  );

  // Protein goal (explicit or auto from weight + training_goal)
  const latestWeightLbs = weight?.unit === "kg" ? kgToLbs(weight.value) : weight?.value;
  const proteinGoal = settings?.protein_goal_g != null
    ? settings.protein_goal_g
    : latestWeightLbs != null && settings
      ? Math.round(latestWeightLbs * (PROTEIN_MULT[settings.training_goal] ?? 0.8))
      : null;
  // Fiber goal: explicit, else 14 g per 1,000 kcal — the DGA/IOM figure. Scaling
  // with intake beats a flat number, which is too low at maintenance and too high
  // deep in a cut. Anchored to the calorie GOAL where one is set, so the target
  // doesn't drift upward as the day's eating accumulates.
  const FIBER_G_PER_1000_KCAL = 14;
  // Floor at the low end of the adult recommendation. Pure calorie-scaling would
  // shrink the fiber target exactly when cutting — the moment fiber matters most,
  // for satiety — so a 1,500 kcal goal would ask for 21 g and call that success.
  const FIBER_FLOOR_G = 25;
  // Caffeine and alcohol shown as tracked intake alongside protein/fiber. Read-only
  // here — the buttons stay in the intake row; this is the "what have I taken in
  // today" view, and leaving them out of it made the nutrition card quietly partial.
  const { data: cafToday } = useQuery({
    queryKey: ["caffeine-day", todayStr], queryFn: () => api.getCaffeineDay(todayStr) });
  const { data: alcToday } = useQuery({
    queryKey: ["alcohol-day", todayStr], queryFn: () => api.getAlcoholDay(todayStr) });

  // Prefer the RESOLVED goal from the server (services/nutrition_goals) so the rule
  // lives in exactly one place. The local computation stays only as a fallback for a
  // settings payload that predates the field.
  const fiberGoal = settings?.fiber_goal_effective
    ?? (settings?.fiber_goal_g != null
      ? settings.fiber_goal_g
      : Math.max(FIBER_FLOOR_G,
                 Math.round(((settings?.calorie_goal ?? 2000) / 1000) * FIBER_G_PER_1000_KCAL)));

  const calorieMode = settings?.calorie_goal_mode ?? "fixed";
  // For fixed mode, calorieGoal is the static target. For balance/pace it's null
  // (goal is computed per-day as Out + offset).
  const calorieGoal = calorieMode === "fixed"
    ? (settings?.calorie_goal ?? null)
    : null;
  const calorieDynamic = settings?.calorie_goal_dynamic ?? false;
  const calorieDailyOffset = settings?.calorie_goal_daily_offset ?? 0;

  // Energy balance — resolved server-side (see services/energy.effective_calorie_goal).
  // This used to be derived here: Mifflin for today, Apple's resting_calories for past
  // dates. That put a ~380 kcal step at the one-day boundary (Apple does not measure
  // basal rate — it models it, ~380/day high here) and disagreed with the nudge card,
  // which read the raw stored goal. One derivation now, on the backend.
  const energy = todayData?.energy ?? null;
  const activeCals = logDate === todayStr
    ? (activity?.active_calories ?? null)
    : (pastActivity?.active_calories ?? null);
  // Only today's resolved energy is meaningful here; past dates keep the simple
  // RMR + active display until the per-date fetch carries an energy block too.
  const outSoFar = logDate === todayStr ? (energy?.out_so_far ?? null) : null;
  const outProjected = logDate === todayStr ? (energy?.out_projected ?? null) : null;
  const rmrForOut = energy?.rmr ?? null;
  const dynamicGoal = logDate === todayStr ? (energy?.goal ?? null) : null;

  // Meal grouping — displayed date (for meal section UI)
  const byMeal = { breakfast: [], lunch: [], dinner: [], dessert: [], snack: [], drinks: [] };
  nutritionLogs.forEach((log) => {
    const key = log.meal_type?.toLowerCase();
    if (key in byMeal) byMeal[key].push(log);
  });

  // Meal grouping — always today (for checklist task checks)
  const todayByMeal = { breakfast: [], lunch: [], dinner: [] };
  todayLogs.forEach((log) => {
    const key = log.meal_type?.toLowerCase();
    if (key in todayByMeal) todayByMeal[key].push(log);
  });

  const openLog = (meal) => {
    setSelectedLog(null);
    setDefaultMeal(meal);
    setShowLogModal(true);
  };
  const openEdit = (log) => {
    setSelectedLog(log);
    setShowLogModal(true);
  };
  // One logged food row in a meal section — also used for the parts inside an expanded
  // combination (ComboLogRow).
  const renderLogRow = (log) => (
    <div key={log.id} className="flex items-center gap-2 rounded-lg bg-slate-700/60 px-3 py-2">
      <button className="flex-1 min-w-0 text-left" onClick={() => openEdit(log)}>
        <p className="text-xs text-white truncate">
          {(() => {
            const title = log.name || log.description;
            return title.length > 38 ? title.slice(0, 35) + "…" : title;
          })()}
          {/* Inside the truncating line, AFTER the name: when the row is short of width
              the tag is what gets cut, never the name (note 60). */}
          <BrandTag brand={log.brand} />
        </p>
        <p className="text-xs text-slate-500">
          {log.calories != null ? `${Math.round(log.calories)} kcal` : ""}
          {log.protein_g != null ? ` · ${log.protein_g.toFixed(0)}g protein` : ""}
        </p>
      </button>
      <button
        onClick={() => deleteLogMutation.mutate(log.id)}
        className="flex-shrink-0 text-slate-600 hover:text-rose-400 transition-colors text-sm leading-none"
        title="Delete"
      >✕</button>
    </div>
  );

  const onFoodSaved = () => {
    qc.invalidateQueries({ queryKey: ["nutrition-logs", logDate] });
    if (logDate !== todayStr) {
      qc.invalidateQueries({ queryKey: ["nutrition-logs", todayStr] });
    }
    qc.invalidateQueries({ queryKey: ["nutrition-daily"] });
    // An alcoholic My Food logged here also writes standard drinks (note 34).
    qc.invalidateQueries({ queryKey: ["alcohol-day"] });
    qc.invalidateQueries({ queryKey: ["alcohol-trend"] });
  };

  // ── Daily checklist ──────────────────────────────────────────────────────────
  const checklistTasks = (() => {
    if (isLoading) return null; // not ready yet
    const hour = new Date().getHours();
    const tasks = [];

    // Morning check-in — suppressed if already logged OR skipped today
    // Away today (a trip): no takeover (App.jsx) and no meal prompts. The check-in row
    // stays — it's the only way in — but says it's optional (note 72).
    const away = !!todayData?.away;
    const checkinSkipped = localStorage.getItem(`checkin_skipped_${todayStr}`);
    if (!todayData?.morning_checkin && !checkinSkipped) {
      tasks.push({
        id: "checkin",
        label: "Morning check-in",
        sub: away ? "optional while away" : "energy · mood · sleep quality",
        action: () => setShowCheckinModal(true),
      });
    }

    // Meal reminders — always based on TODAY regardless of logDate
    if (!away && hour >= 12 && todayByMeal.breakfast.length === 0)
      tasks.push({ id: "breakfast", label: "Log breakfast", action: () => { setLogDate(todayStr); openLog("breakfast"); } });
    if (!away && hour >= 16 && todayByMeal.lunch.length === 0)
      tasks.push({ id: "lunch", label: "Log lunch", action: () => { setLogDate(todayStr); openLog("lunch"); } });
    if (!away && hour >= 21 && todayByMeal.dinner.length === 0)
      tasks.push({ id: "dinner", label: "Log dinner", action: () => { setLogDate(todayStr); openLog("dinner"); } });

    // Backfill reminders — PAST days with an incomplete meal log (excluded from
    // nutrition analysis until completed or skipped). Show the 3 most recent.
    incompleteDays.slice(0, 3).forEach((d) => {
      const dt = new Date(d + "T12:00:00");
      const label = dt.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
      tasks.push({
        id: `incomplete-${d}`,
        label: `Finish logging ${label}`,
        sub: "incomplete — excluded from nutrition stats",
        action: () => { setLogDate(d); },
      });
    });
    // (No caffeine backfill reminder by design — a missed caffeine day just stays
    // excluded from caffeine stats; backfill only via the card's date picker.)

    // RPE — any workout in the last 36 hours without a rating
    recentWorkouts
      .filter(w => {
        const ageH = (Date.now() - new Date(w.start_time).getTime()) / 3_600_000;
        return ageH < 36 && w.rpe == null;
      })
      .forEach(w => tasks.push({
        id: `rpe-${w.id}`,
        label: `Rate effort — ${w.activity_type ?? "Workout"}`,
        sub: w.start_time?.slice(0, 10),
        action: () => setRpeTarget(w),
      }));

    return tasks;
  })();

  // Goals from settings (with defaults)
  const stepsGoal    = settings?.steps_goal    ?? 10_000;
  const calGoal      = settings?.move_goal     ?? 500;
  const exerciseGoal = settings?.exercise_goal ?? 30;

  const stepsDisplay = activity?.steps != null
    ? activity.steps >= 1000 ? `${(activity.steps / 1000).toFixed(1)}k` : String(activity.steps)
    : null;


  return (
    <div className="p-4">
      <SegmentedTabs
        value={view} onChange={setView}
        options={[{ value: "state", label: "STATE" }, { value: "log", label: "LOG" }]}
      />

      <div className="mt-4 space-y-4">

      {/* Renders nothing unless AI is broken. Above both views because a credit
          outage stops food parsing AND the daily summary. */}
      <AiStatusBanner />

      {/* "Traveling — where are you?" Renders nothing unless the time zone is uncertain. */}
      <TzPromptBanner />

      {/* Coach — the single daily guidance card. Blinded by design: the label may
          come from the metric-based rec OR from the experiment's random draw, and
          the card never says which (so compliance can't differ by mechanism).
          Numbers live in the Readiness card below, deliberately not here. */}
      {view === "state" && (todayData?.coach || todayData?.recovery_alarm) && (() => {
        const coach = todayData.coach;
        const hint = todayData.strength_hint;

        // Recovery moved INTO this card as a corner chip (2026-08-15). It was
        // its own full-width row for a single word, directly under a card that was
        // mostly empty space. Still the tap target for the full illness/vitals modal.
        const alarm = todayData.recovery_alarm;
        const lm = alarm
          ? (RECOVERY_LEVEL_META[alarm.level ?? "clear"] ?? RECOVERY_LEVEL_META.clear)
          : null;
        const recoveryChip = lm && (
          <button onClick={() => setShowRecovery(true)}
            className="-my-1 -mr-1 flex items-center gap-1.5 rounded-md px-1.5 py-1"
            title="Recovery detail">
            <span className="h-[7px] w-[7px] flex-none rounded-full" style={{ backgroundColor: lm.color }} />
            <span className="text-[10px] font-semibold tracking-micro" style={{ color: lm.color }}>
              {lm.label}
            </span>
            <span className="text-[10px] text-ink-3">→</span>
          </button>
        );

        // Coach earns its place with one word and one sentence; it does not need a
        // 46px hero to say them. Sizing is set by the longest label (MODERATE) so the
        // card height stays constant as the label changes day to day.
        if (!coach) {
          return <Card className="!py-2.5"><Eyebrow left="RECOVERY" right={recoveryChip} /></Card>;
        }
        // Weekly-plan extras (note 29): the home pull block and a protein line. Shown on
        // the open AND the done card — finishing class does not finish the pull block.
        const extras = (coach.pull || coach.legs || coach.protein || coach.extra) && (
          <div className="mt-2.5 space-y-1.5 border-t border-rule pt-2.5">
            {coach.pull && (
              <div className="flex items-start gap-2">
                <span className="mt-px flex-none text-[9.5px] font-semibold tracking-micro"
                  style={{ color: coach.pull.done ? "var(--rhr)" : "#818cf8" }}>
                  {coach.pull.done ? "✓ PULL" : "+ PULL"}
                </span>
                <p className="prose-ui text-[11px] leading-snug text-ink-2">{coach.pull.detail}</p>
              </div>
            )}
            {/* Note 44: one rotating line from the user's own findings — the part of the
                card that is supposed to tell him something he didn't already know. */}
            {coach.extra && (
              <div className="flex items-start gap-2">
                <span className="flex-none text-[9px] tracking-micro" style={{ color: "#f59e0b" }}>
                  ALSO
                </span>
                <p className="prose-ui text-[11px] leading-snug text-ink-2">{coach.extra.detail}</p>
              </div>
            )}
            {coach.legs && (
              <div className="flex items-start gap-2">
                <span className="mt-px flex-none text-[9.5px] font-semibold tracking-micro"
                  style={{ color: coach.legs.done ? "var(--rhr)" : "#818cf8" }}>
                  {coach.legs.done ? "✓ LEGS" : "+ LEGS"}
                </span>
                <p className="prose-ui text-[11px] leading-snug text-ink-2">{coach.legs.detail}</p>
              </div>
            )}
            {coach.protein && (
              <div className="flex items-start gap-2">
                <span className="mt-px flex-none text-[9.5px] font-semibold tracking-micro"
                  style={{ color: "var(--over)" }}>
                  PROTEIN
                </span>
                <p className="prose-ui text-[11px] leading-snug text-ink-2">{coach.protein.detail}</p>
              </div>
            )}
          </div>
        );
        if (coach.acted) {
          return (
            <Card className="!py-3">
              <Eyebrow left={<span className="inline-flex items-center gap-1.5"><RhoMark size={13} />RHO · TODAY</span>} right={recoveryChip} />
              <div className="mt-2 flex items-baseline gap-2.5">
                <span className="flex-none text-[15px] font-semibold" style={{ color: "var(--rhr)" }}>
                  ✓ DONE
                </span>
                <p className="prose-ui min-w-0 text-[11.5px] leading-snug text-ink-2">
                  {coach.acted_detail}
                </p>
              </div>
              {extras}
            </Card>
          );
        }
        // The day went a different way than the call (note 21) — currently only "REST
        // was recommended and a session happened". NOT the green ✓ DONE above: nothing
        // was completed as asked. Neutral ink rather than a warning colour, because the
        // experiment is intention-to-treat and a departure is his call, not an error.
        // What matters is that the card stops saying "rest today" at 8pm to someone who
        // trained at noon.
        if (coach.departed) {
          return (
            <Card className="!py-3">
              <Eyebrow left={<span className="inline-flex items-center gap-1.5"><RhoMark size={13} />RHO · TODAY</span>} right={recoveryChip} />
              <div className="mt-2 flex items-baseline gap-2.5">
                <span className="flex-none text-[15px] font-semibold text-ink-2">
                  LOGGED
                </span>
                <p className="prose-ui min-w-0 text-[11.5px] leading-snug text-ink-2">
                  {coach.acted_detail}
                </p>
              </div>
              {extras}
            </Card>
          );
        }
        return (
          <Card className="!py-3">
            <Eyebrow left={<span className="inline-flex items-center gap-1.5"><RhoMark size={13} />RHO · TODAY</span>} right={recoveryChip} />
            {/* The label is the headline — the one instruction the app gives per day.
                Numbers stay out of this card on purpose: the label may come from the
                metric rec OR from an experiment's random draw, and revealing which
                would unblind the trial. */}
            <div className="mt-2 flex items-baseline gap-2.5">
              <span className="flex-none text-[19px] font-semibold leading-none tracking-[-.02em]"
                style={{ color: coach.color }}>
                {coach.label}
              </span>
              <p className="prose-ui min-w-0 text-[11.5px] leading-snug text-ink-2">{coach.detail}</p>
            </div>
            {extras}
            {/* Strength progression hint (only when Hevy history exists) */}
            {hint && (
              <div className="mt-2.5 flex items-start gap-2 border-t border-rule pt-2.5">
                <span className="mt-px flex-none text-[9.5px] font-semibold tracking-micro"
                  style={{ color: hint.color }}>
                  {hint.label}
                </span>
                <p className="prose-ui text-[11px] leading-snug text-ink-2">{hint.detail}</p>
              </div>
            )}
            {hint && <AnchorTargets />}
          </Card>
        );
      })()}

      {/* Class board (note 91): photo of the class whiteboard → a weight per exercise.
          Today's main view only (2026-11-10; it was in LOG too). One slim row, no
          card. Gone once he has been to the gym today (note 94: a Hevy or Apple gym
          session, or a board read 75+ min ago); stays if that check fails, since it
          matters most before class. */}
      {view === "state" && !gymToday?.done && (
        <button onClick={() => setShowBoard(true)}
          className="flex w-full items-center justify-between rounded-2xl border border-rule bg-card px-4 py-2.5 text-left">
          <span className="inline-flex items-center gap-2 whitespace-nowrap text-[12.5px] font-medium text-ink">
            <RhoMark size={16} />Rho · class board
          </span>
          <span className="whitespace-nowrap text-[11px] text-ink-3">photo → weights →</span>
        </button>
      )}

      {/* Outdoor conditions — ambient context, compressed to a strip */}
      {view === "state" && <WeatherCard morningLight={habits?.outdoor_light_am} />}

      {/* What to actually do right now. Rule-based (services/nudges) — the gap is
          arithmetic and the food suggestions come from the user's own rotation, so
          no model call is involved. Renders nothing when there's nothing to say. */}
      {view === "state" && <NudgeCard />}


      {/* Character sheet (docs/GAMIFICATION.md) + the daily battery, side by side.
          Both are collapsed-by-default utilities you open deliberately rather than
          read at a glance, so they share one row instead of taking two full-width
          cards near the top. Each still expands in place when tapped. */}
      {view === "state" && (
        <div className={`grid items-start gap-3 [&>*:only-child]:col-span-2 ${
          pairExpanded ? "grid-cols-1" : "grid-cols-2"
        }`}>
          <CharacterSheet onOpenChange={setSheetOpen} />
          <BatteryCard onNavigate={onNavigate} onOpenChange={setBatteryOpen} />
        </div>
      )}
      {/* Tonight's assignment for any running supplement experiment + dose logging. */}
      {view === "log" && <DoseCard />}

      {/* Data freshness — tap for readiness-input detail */}
      {view === "state" && freshness && (
        <div>
          <button
            className="flex items-center gap-1.5"
            onClick={() => setShowHealthDetail(v => !v)}
          >
            <span className={`text-xs ${freshness.cls}`}>●</span>
            <span className={`text-xs ${freshness.cls}`}>{freshness.text}</span>
            {readinessComplete === false && (
              <span className="text-xs text-yellow-400 ml-0.5">· incomplete inputs</span>
            )}
          </button>
          {showHealthDetail && dataHealth && (
            <div className="mt-1.5 ml-3.5 text-xs text-slate-500 space-y-0.5">
              {dataHealth.missing_inputs.length > 0 ? (
                dataHealth.missing_inputs.map((m, i) => (
                  <p key={i} className="text-yellow-500/80">Missing: {m}</p>
                ))
              ) : (
                <p className="text-emerald-500/80">All readiness inputs present</p>
              )}
              {todayData?.last_sync_at && (
                <p>Last sync: {new Date(todayData.last_sync_at).toLocaleString()}</p>
              )}
            </div>
          )}
        </div>
      )}

      <TvCheck />

      {/* The standalone protein nudge that lived here is now one rule inside
          <NudgeCard/> at the top — it named the gap but not what to do about it,
          and two protein reminders on one screen is one too many. */}

      {/* Daily checklist — only rendered when there are open tasks */}
      {view === "log" && checklistTasks !== null && checklistTasks.length > 0 && (
        <div className="rounded-xl bg-slate-800 p-4">
          <p className="mb-2.5 text-xs font-medium uppercase tracking-widest text-slate-400">
            To Do <span className="ml-1 text-slate-600 normal-case tracking-normal font-normal">{checklistTasks.length} remaining</span>
          </p>
          <div className="space-y-1.5">
            {checklistTasks.map(task => (
              <ChecklistItem key={task.id} label={task.label} sub={task.sub} onClick={task.action} />
            ))}
          </div>
        </div>
      )}

      {/* ONE date for everything in LOG (2026-10-21): meals, caffeine/alcohol/water,
          quick-log, habits, naps and the day flag all log to this day. It replaced the
          nutrition card's own stepper and the per-sheet date inputs. */}
      {view === "log" && (
        <div className="flex items-center justify-between rounded-2xl border border-rule bg-card px-3 py-2">
          <span className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">
            LOGGING FOR{" "}
            <span className={logDate === todayStr ? "text-ink-2" : "text-ink"}>
              {logDate === todayStr ? "TODAY" : new Date(`${logDate}T12:00:00`).toLocaleDateString(
                "en-US", { weekday: "short", month: "short", day: "numeric" }).toUpperCase()}
            </span>
          </span>
          <div className="flex items-center gap-2">
            {logDate !== todayStr && (
              <button onClick={() => setLogDate(todayStr)} className="text-[11px] text-ink-2">Today</button>
            )}
            <DateStepper value={logDate} max={todayStr} onChange={setLogDate} />
          </div>
        </div>
      )}

      {/* Caffeine / alcohol / water — one row, each with presets, a custom entry and
          backfill. Replaces the standalone caffeine card, which now opens inside it.
          FIRST in LOG (2026-10-21): it's the most frequent logging action (cold brew alone
          was 30 of 268 entries over 25 days) and used to sit below the whole meal log. */}
      {view === "log" && <IntakeTrio date={logDate} />}

      {/* One-tap re-log of your usual foods around now — renders nothing without history. */}
      {view === "log" && <QuickLogRow date={logDate} />}

      {/* Background parses waiting for review — renders nothing when the queue is empty. */}
      {view === "log" && (
        <ParseJobsTray onReview={(j) => { setSelectedLog(null); setReviewJob(j); setShowLogModal(true); }} />
      )}

      {/* Daily habits */}
      {view === "log" && (() => {
        const fmtHour = (h) => {
          const ampm = h >= 12 ? "pm" : "am";
          const h12  = h % 12 || 12;
          return `${h12}${ampm}`;
        };
        const caffeineHour = settings?.caffeine_cutoff_hour ?? 14;
        const lightHour    = settings?.light_cutoff_hour    ?? 10;
        const HABIT_ROWS = [
          // Derived from the caffeine instance log (services/habit_metrics): the last
          // dose of the day against the cutoff. No daily tap needed; the Behaviors
          // calendar still overrides a day if the log is wrong.
          { key: "supplements",      emoji: "💊", label: "Supplements",                        color: "var(--rem)",       streakKey: "supplements" },
          { key: "creatine",         emoji: "🧪", label: "Creatine",                           color: "var(--drinks)",    streakKey: "creatine"    },
          // Meditation is auto-derived from Apple Mindful Minutes (services/habit_metrics)
          // and no longer needs a daily tap. Correcting or backfilling a day is still
          // possible on the Behaviors calendar, where an explicit value overrides the
          // derived one.
        ];
        const resolved = (k) => logHabits?.[k] === true || logHabits?.[k] === false;
        const allLogged = !habitExpanded && logHabits && HABIT_ROWS.every(h => resolved(h.key));
        const keptCount = HABIT_ROWS.filter(h => logHabits?.[h.key] === true).length;
        if (allLogged) {
          return (
            <button
              onClick={() => setHabitExpanded(true)}
              className="flex w-full items-center gap-2.5 rounded-2xl border border-rule bg-card px-3 py-2 text-left"
            >
              <span className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">HABITS</span>
              <span className="text-[11px] font-semibold"
                style={{ color: keptCount === HABIT_ROWS.length ? "var(--rhr)" : "var(--ink-2)" }}>
                {keptCount === HABIT_ROWS.length
                  ? "all kept"
                  : `${keptCount}/${HABIT_ROWS.length} kept`}
              </span>
              {/* One dot per habit, filled for kept and hollow for skipped — the whole
                  day's answer without expanding. */}
              <span className="ml-auto flex items-center gap-1">
                {HABIT_ROWS.map(h => {
                  const kept = logHabits?.[h.key] === true;
                  return (
                    <span key={h.key} className="h-2 w-2 rounded-full" title={h.label}
                      style={kept
                        ? { backgroundColor: h.color }
                        : { border: "1px solid var(--ink-3)" }} />
                  );
                })}
                <span className="ml-1 text-[10px] text-ink-3">edit →</span>
              </span>
            </button>
          );
        }
        return (
          <Card className="!p-3">
            <p className="mb-2 text-[9.5px] font-semibold tracking-eyebrow text-ink-3">HABITS</p>
            {/* 2x2 rather than four full-width rows: the labels are short and the
                streak reads fine inline, so the row-per-habit layout was mostly
                empty horizontal space. */}
            <div className="grid grid-cols-2 gap-1">
              {HABIT_ROWS.map(({ key, emoji, label, streakKey }) => {
                const val  = logHabits?.[key];
                const done = val === true;
                const fail = val === false;
                const s    = streaks[streakKey];
                const streak = s?.streak ?? 0;
                const best   = s?.best   ?? 0;
                const timeField = HABIT_TIME_FIELD[key];
                const timeVal = timeField ? logHabits?.[timeField] : null;
                return (
                  <div
                    key={key}
                    className="rounded-lg px-2 py-1.5 transition-colors"
                    style={{
                      backgroundColor: done ? "color-mix(in srgb, var(--rhr) 12%, transparent)"
                                      : fail ? "color-mix(in srgb, var(--over) 10%, transparent)"
                                      : "var(--surface)"
                    }}
                  >
                    <button onClick={() => toggleHabit(key)} className="flex w-full items-start gap-1.5 text-left">
                      <span className={`mt-px w-3.5 h-3.5 flex-shrink-0 rounded-full border-2 flex items-center justify-center transition-colors ${
                        done ? "border-[var(--rhr)] bg-[var(--rhr)]"
                      : fail ? "border-[var(--over)] bg-[var(--over)]"
                      :        "border-ink-3"
                      }`}>
                        {done && <span className="text-canvas text-[9px] leading-none">✓</span>}
                        {fail && <span className="text-canvas text-[9px] leading-none">✕</span>}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className={`block text-[11px] leading-snug ${
                          done ? "text-ink" : fail ? "text-ink-2" : "text-ink-2"
                        }`}>
                          {/* the caffeine label carries its own ☕ — don't print two */}
                          {!label.includes(emoji) && <span className="mr-1">{emoji}</span>}{label}
                        </span>
                        {(streak > 0 || best > 0) && (
                          <span className="mt-0.5 block text-[9.5px] leading-none text-ink-3">
                            {streak > 0 ? `${streak}🔥` : "—"}
                            {best > 0 && <span className="text-ink-3/70"> · best {best}</span>}
                          </span>
                        )}
                      </span>
                    </button>
                    {/* Optional time — feeds a continuous correlation input */}
                    {timeField && (
                      <label className="mt-1 flex items-center gap-1 pl-6 text-[10px] text-slate-500">
                        🕐
                        <input
                          type="time"
                          value={minToHHMM(timeVal)}
                          onChange={(e) => setHabitTime(timeField, e.target.value)}
                          className="bg-transparent text-[10px] text-slate-400 outline-none [color-scheme:dark]"
                        />
                        {timeVal == null && <span className="text-slate-600">add time</span>}
                      </label>
                    )}
                  </div>
                );
              })}
            </div>
          </Card>
        );
      })()}

      {/* Weekly tape reminder, under HABITS where the daily to-dos live. NOT a HabitLog
          column: tape is weekly, so a daily boolean would record six misses a week.
          Shown only when due (no entry in 7 days); taps through to Body's TAPE card. */}
      {view === "log" && tapeDue && (
        <button onClick={() => onNavigate?.("body")}
          className="flex w-full items-center gap-2.5 rounded-2xl border border-rule bg-card px-3 py-2 text-left">
          <span className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">WEEKLY</span>
          <span className="text-[11px] text-ink-2">
            📏 Tape measurement due{tapeDaysAgo != null ? ` — last one ${tapeDaysAgo} days ago` : ""}
          </span>
          <span className="ml-auto text-[10px] text-ink-3">Body →</span>
        </button>
      )}

      {/* Today's briefing, plus the weekly (Sun/Mon) and monthly (1st–3rd) summaries while
          fresh. Renders nothing when there's nothing current. */}
      {view === "state" && <TodaySummaries todayData={todayData} todayStr={todayStr} />}

      {/* Readiness + Activity — two symmetric ring clusters, four stats each */}
      {view === "state" && (
      <div className="rounded-xl bg-slate-800 p-4">
        <div className="flex items-stretch gap-3">

          {/* Readiness column: ring + its four inputs (tap a row for the info modal) */}
          <div className="flex-1 min-w-0 flex flex-col items-center">
            <div className="flex items-center gap-1 mb-2 self-start">
              <p className="text-xs font-medium uppercase tracking-widest text-slate-400">Readiness</p>
              <button onClick={() => setInfoMetric("readiness")} className="text-xs text-slate-600 leading-none">ⓘ</button>
            </div>
            {isLoading
              ? <div className="w-[120px] h-[120px] rounded-full border-4 border-slate-700 flex items-center justify-center">
                  <span className="text-slate-600 text-xs">…</span>
                </div>
              : <ReadinessRing score={metrics?.readiness_score} label={metrics?.readiness_label} size={120} />
            }
            <div className="mt-3 w-full flex flex-col gap-2">
              {[
                { key: "hrv", label: "HRV",
                  value: metrics?.hrv_z_score != null ? `${metrics.hrv_z_score.toFixed(2)}z` : "—",
                  valueColor: metrics?.hrv_z_score != null ? zStatus(metrics.hrv_z_score).color : undefined },
                { key: "rhr", label: "RHR (night)",
                  value: metrics?.rhr_z_score != null ? `${metrics.rhr_z_score.toFixed(2)}z` : "—",
                  valueColor: metrics?.rhr_z_score != null ? zStatus(metrics.rhr_z_score, true).color : undefined },
                { key: "sleep_debt", label: "Sleep debt",
                  value: metrics?.sleep_debt_hours != null ? `${metrics.sleep_debt_hours.toFixed(1)}h` : "—",
                  valueColor: metrics?.sleep_debt_hours != null ? sleepDebtStatus(metrics.sleep_debt_hours).color : undefined },
                { key: "acwr", label: "Workload",
                  value: metrics?.acwr != null ? metrics.acwr.toFixed(2) : "—",
                  valueColor: metrics?.acwr != null ? acwrStatus(metrics.acwr).color : undefined },
              ].map((s) => (
                <StatRow key={s.key} label={s.label} value={s.value} valueColor={s.valueColor}
                  onClick={() => setInfoMetric(s.key)} />
              ))}
            </div>
          </div>

          {/* Divider */}
          <div className="w-px self-stretch bg-slate-700" />

          {/* Activity column: four rings + legend carrying current/goal values */}
          <div className="flex-1 min-w-0 flex flex-col items-center">
            <div className="flex items-center gap-1 mb-2 self-start">
              <p className="text-xs font-medium uppercase tracking-widest text-slate-400">Activity</p>
            </div>
            <ActivityRings
              calories={activity?.active_calories}
              exerciseMins={activity?.exercise_minutes}
              standHours={activity?.stand_hours}
              steps={activity?.steps}
              calGoal={calGoal}
              exerciseGoal={exerciseGoal}
              stepsGoal={stepsGoal}
            />
            <div className="mt-3 w-full flex flex-col gap-2">
              {[
                { key: "move", label: "Move", dotColor: "#f97316",
                  value: `${activity?.active_calories != null ? Math.round(activity.active_calories) : "—"}/${calGoal}` },
                { key: "exercise", label: "Exercise", dotColor: "#22c55e",
                  value: `${activity?.exercise_minutes != null ? Math.round(activity.exercise_minutes) : "—"}/${exerciseGoal}` },
                { key: "stand", label: "Stand", dotColor: "#38bdf8",
                  value: `${activity?.stand_hours != null ? Math.round(activity.stand_hours) : "—"}/${STAND_GOAL}` },
                { key: "steps", label: "Steps", dotColor: "#a78bfa",
                  value: `${stepsDisplay ?? "—"}/${stepsGoal >= 1000 ? `${Math.round(stepsGoal / 1000)}k` : stepsGoal}` },
              ].map((s) => (
                <StatRow key={s.key} label={s.label} value={s.value} dotColor={s.dotColor} />
              ))}
            </div>
          </div>

        </div>
      </div>
      )}

      {/* Today's Nutrition Log */}
      {view === "log" && (
      <div className="rounded-xl bg-slate-800 p-4">
        <div className="mb-3 flex items-center justify-between">
          <p className="text-xs font-medium uppercase tracking-widest text-slate-400">
            Nutrition {logDate === todayStr ? "Today" : logDate}
          </p>
        </div>

        {/* Past-day completeness — incomplete days are excluded from nutrition
            stats until finished or skipped. */}
        {logDate !== todayStr && viewedDaySkipped ? (
          <div className="mb-3 flex items-center gap-2 rounded-lg border border-slate-700/60 bg-slate-800/40 px-3 py-2">
            <span className="flex-1 text-xs italic text-slate-500">Day skipped — excluded from nutrition stats</span>
            <button
              onClick={() => unskipDayMutation.mutate(logDate)}
              className="flex-shrink-0 text-xs font-medium text-slate-500 hover:text-slate-200 transition-colors"
            >Undo</button>
          </div>
        ) : logDate !== todayStr && incompleteDays.includes(logDate) ? (
          <div className="mb-3 flex items-center gap-2 rounded-lg border border-amber-500/25 bg-amber-950/20 px-3 py-2">
            <span className="flex-1 text-xs text-amber-400/90">Incomplete — log or skip each meal, or skip the whole day</span>
            <button
              onClick={() => skipDayMutation.mutate(logDate)}
              className="flex-shrink-0 rounded-lg bg-slate-700 px-2 py-1 text-xs font-medium text-slate-300 hover:bg-slate-600 transition-colors"
            >Skip day</button>
          </div>
        ) : null}

        {/* Compact nutrition summary — calorie balance bar + protein + macro split */}
        {hasNutrition && (
          <div className="mb-3 space-y-3">
            <CalorieBalanceBar
              intake={dayTotals.calories}
              out={outProjected ?? (rmrForOut != null && activeCals != null
                ? rmrForOut + Math.round(activeCals) : null)}
              goal={calorieMode === "fixed" ? calorieGoal : dynamicGoal}
              offset={calorieDynamic ? calorieDailyOffset : 0}
              mode={calorieMode}
              mean={meanIntake}
            />
            {/* What was left out, and how much of the day was even looked at. Renders
                only when there is something to say: silence beats "~0 not counted".
                A day where NOTHING is assessed says nothing worth reading — every
                entry predates the feature, or every one came from the picker — so the
                coverage half needs at least one assessed item to appear. Otherwise
                this would read "7 of 7 not assessed" every day for weeks and train
                you straight past it. A PARTIAL gap is the real signal. */}
            {(dayTotals.omitted > 0
              || (dayTotals.nAssessed > 0 && dayTotals.nAssessed < dayTotals.nItems)) && (
              <p className="-mt-1 text-[10px] leading-snug text-ink-3">
                {dayTotals.omitted > 0 && (
                  <>≈{Math.round(dayTotals.omitted)} kcal not counted
                    <span className="text-ink-3/70"> — unnamed spreads, sides, condiments</span>
                  </>
                )}
                {dayTotals.omitted > 0 && dayTotals.nAssessed < dayTotals.nItems && " · "}
                {dayTotals.nAssessed < dayTotals.nItems && (
                  <span className="text-ink-3/70">
                    {dayTotals.nItems - dayTotals.nAssessed} of {dayTotals.nItems} item
                    {dayTotals.nItems === 1 ? "" : "s"} not assessed
                  </span>
                )}
              </p>
            )}
            {/* Protein and fiber share one row: both are "grams toward a target",
                and side by side the shorter fill is obvious at a glance. */}
            {(proteinGoal || fiberGoal) && (
              <div className="grid grid-cols-2 gap-2 [&>*:only-child]:col-span-2">
                {proteinGoal && (
                  <TrackBar label="PROTEIN" value={nutTotals.protein} goal={proteinGoal} unit="g"
                    color="var(--rhr)" displayValue={`${nutTotals.protein.toFixed(0)}`}
                    mean={nutritionRefs.protein}
                    sub={nutritionRefs.protein
                      ? `┊ mean ${nutritionRefs.protein.value} · usual ${nutritionRefs.protein.lo}–${nutritionRefs.protein.hi}g`
                      : null} />
                )}
                {fiberGoal && (
                  <TrackBar label="FIBER" value={nutTotals.fiber} goal={fiberGoal} unit="g"
                    color="var(--lunch)" displayValue={`${nutTotals.fiber.toFixed(0)}`}
                    mean={nutritionRefs.fiber}
                    sub={nutritionRefs.fiber
                      ? `┊ mean ${nutritionRefs.fiber.value} · usual ${nutritionRefs.fiber.lo}–${nutritionRefs.fiber.hi}g`
                      : null} />
                )}
              </div>
            )}
            {/* Caffeine and alcohol: consumed, dose-bearing, and previously invisible
                here. No goal bar — neither has a target you are trying to HIT, so a
                progress fill would imply one. Plain readouts instead. */}
            {((cafToday?.total_mg ?? 0) > 0 || (alcToday?.total ?? 0) > 0) && (
              <div className="grid grid-cols-2 gap-2">
                <div className="rounded-lg bg-surface px-2.5 py-2">
                  <p className="text-[9px] tracking-micro text-ink-3">CAFFEINE</p>
                  <p className="text-[15px] font-semibold leading-none text-ink">
                    {Math.round(cafToday?.total_mg ?? 0)}
                    <span className="text-[9px] font-normal text-ink-3">mg</span>
                  </p>
                  <AtBedtime
                    value={cafToday?.onboard_at_bed}
                    projected={cafToday?.bedtime_projected}
                    format={(v) => `${Math.round(v)}mg`}
                  />
                </div>
                <div className="rounded-lg bg-surface px-2.5 py-2">
                  <p className="text-[9px] tracking-micro text-ink-3">ALCOHOL</p>
                  <p className="text-[15px] font-semibold leading-none text-ink">
                    {(alcToday?.total ?? 0).toFixed(1)}
                    <span className="text-[9px] font-normal text-ink-3"> SDU</span>
                  </p>
                  <AtBedtime
                    value={alcToday?.onboard_at_bed}
                    projected={alcToday?.bedtime_projected}
                    format={(v) => `${v.toFixed(1)} SDU`}
                  />
                </div>
              </div>
            )}
            <MacroSplit protein={dayTotals.protein} carbs={dayTotals.carbs} fat={dayTotals.fat} avg={avgMacroSplit} />
            {(rmrForOut != null) && (
              <p className="text-[9.5px] leading-snug text-ink-3">
                {outSoFar != null && outProjected != null ? (
                  <>
                    {outSoFar} burned · {outProjected} projected · Out = RMR {Math.round(rmrForOut)}
                    {energy?.rmr_basis === "katch_mcardle" ? " (Katch-McArdle)" : " (Mifflin-St Jeor)"}
                    {energy?.k != null && energy.k !== 1 && ` + ${energy.k}× active`}
                  </>
                ) : (
                  "Out = RMR + active · ±10–15%"
                )}
              </p>
            )}
          </div>
        )}

        {/* Meal sections */}
        <div className="space-y-3">
          {MEALS.map((meal) => {
            const skip = byMeal[meal].find((l) => l.skipped);
            const foods = byMeal[meal].filter((l) => !l.skipped);
            return (
            <div key={meal}>
              <div className={`flex items-center justify-between ${
                !skip && foods.length === 0 ? "" : "mb-1.5"
              }`}>
                <p className="flex items-baseline gap-1.5 text-[11px] font-medium text-ink-2">
                  <span>{MEAL_EMOJI[meal]} {MEAL_LABELS[meal]}</span>
                  {!skip && foods.length === 0 && (
                    <span className="text-[10px] italic text-ink-3">nothing logged</span>
                  )}
                </p>
                {/* Hide "+ Add" while skipped — undo first, then log */}
                {!skip && (
                  <div className="flex items-center gap-1.5">
                    {SKIPPABLE.has(meal) && foods.length === 0 && (
                      <button
                        onClick={() => skipMealMutation.mutate(meal)}
                        className="rounded-md bg-surface px-2 py-0.5 text-[10px] font-medium text-ink-3 transition-colors hover:text-ink"
                      >
                        Skip
                      </button>
                    )}
                    {/* Two or more pieces (not drinks with a dose) → save them as one dish. */}
                    {foods.filter((l) => !l.source_kind).length >= 2 && (
                      <button
                        onClick={() => setComboMeal(meal)}
                        className="rounded-md bg-surface px-2 py-0.5 text-[10px] font-medium text-ink-3 transition-colors hover:text-ink"
                        title="Save some of these as one My Food"
                      >
                        + My Food
                      </button>
                    )}
                    <button
                      onClick={() => openLog(meal)}
                      className="rounded-md bg-surface px-2 py-0.5 text-[10px] font-medium text-ink-2 transition-colors hover:text-ink"
                    >
                      + Add
                    </button>
                  </div>
                )}
              </div>
              {skip ? (
                <div className="flex items-center gap-2 rounded-lg border border-slate-700/60 bg-slate-800/40 px-3 py-2">
                  <span className="flex-1 text-xs italic text-slate-500">Skipped — no {MEAL_LABELS[meal].toLowerCase()}</span>
                  <button
                    onClick={() => deleteLogMutation.mutate(skip.id)}
                    className="flex-shrink-0 text-xs font-medium text-slate-500 hover:text-slate-200 transition-colors"
                  >Undo</button>
                </div>
              ) : foods.length === 0 ? null : (
                <div className="space-y-1.5">
                  {(() => {
                    // Parts of one combination collapse into a single entry at the
                    // position of their first part (note 35).
                    const entries = [];
                    const byGroup = {};
                    for (const log of foods) {
                      if (log.combo_group) {
                        if (!byGroup[log.combo_group]) {
                          byGroup[log.combo_group] = [];
                          entries.push({ group: log.combo_group });
                        }
                        byGroup[log.combo_group].push(log);
                      } else {
                        entries.push({ log });
                      }
                    }
                    return entries.map((e) => (e.group && byGroup[e.group].length > 1
                      ? <ComboLogRow key={`g-${e.group}`} parts={byGroup[e.group]} renderPart={renderLogRow}
                          onDeleteAll={(parts) => parts.forEach((p) => deleteLogMutation.mutate(p.id))} />
                      : renderLogRow(e.log || byGroup[e.group][0])));
                  })()}
                </div>
              )}
            </div>
            );
          })}
        </div>
      </div>
      )}

      {/* Sleep summary */}
      {view === "state" && <SleepSummary sleep={lastSleep} onNavigate={onNavigate} />}

      {/* Nap + day flag side by side (note 82, 2026-10-26) — two full-width stacked rows
          before. The flag control takes both columns while expanded (its reason chips).
          Flag = nonstandard day (travel/sick/…) → excluded from experiments + correlations. */}
      {view === "log" && (() => {
        const t = (tripsData?.trips || [])[0];
        const ago = t ? (new Date(`${todayStr}T12:00:00`) - new Date(`${t.end}T12:00:00`)) / 864e5 : null;
        if (!t || t.unlogged != null || ago < 1 || ago > 7) return null;
        const fmtD = (iso) => new Date(`${iso}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" });
        return (
          <button onClick={() => setShowTrips(true)}
            className="flex w-full items-center justify-between rounded-2xl border border-rule bg-card px-4 py-2.5 text-left">
            <span className="text-[12.5px] text-ink">Trip {fmtD(t.start)}–{fmtD(t.end)}: anything you didn&apos;t log?</span>
            <span className="text-[11px] text-ink-3">→</span>
          </button>
        );
      })()}

      {view === "log" && (
        <div>
          <div className="grid grid-cols-2 gap-2">
            <NapCard date={logDate} compact />
            <DayFlagControl date={logDate} openClassName="col-span-2" />
          </div>
          <div className="mt-1 flex justify-end gap-4">
            <button
              onClick={() => setShowTrips(true)}
              className="text-[11px] text-ink-3 hover:text-ink-2"
            >
              Trips →
            </button>
            <button
              onClick={() => setShowFlagCalendar(true)}
              className="text-[11px] text-ink-3 hover:text-ink-2"
            >
              Flag past days →
            </button>
          </div>
        </div>
      )}
      </div>

      {showCheckinModal && (
        <MorningCheckinModal
          onDone={() => { setShowCheckinModal(false); qc.invalidateQueries({ queryKey: ["today"] }); }}
          onSkip={() => { setShowCheckinModal(false); localStorage.setItem(`checkin_skipped_${todayStr}`, "1"); }}
        />
      )}
      {infoMetric    && <MetricInfoModal   metric={METRIC_INFO[infoMetric]} onClose={() => setInfoMetric(null)} />}
      {showRecovery && (
        <RecoverySignalsModal
          alarm={todayData?.recovery_alarm}
          signals={todayData?.recovery_signals}
          onClose={() => setShowRecovery(false)}
        />
      )}
      {showFlagCalendar && (
        <DayFlagCalendar onClose={() => setShowFlagCalendar(false)} />
      )}
      {showTrips && <TripPlanner onClose={() => setShowTrips(false)} />}
      {comboMeal && (
        <ComboFromMeal meal={comboMeal} mealLabel={MEAL_LABELS[comboMeal]}
          logs={byMeal[comboMeal].filter((l) => !l.skipped && !l.source_kind)}
          onClose={() => setComboMeal(null)} />
      )}
      {showBoard && <ClassBoardSheet onClose={() => setShowBoard(false)} />}
      {rpeTarget && (
        <RpeQuickModal
          workout={rpeTarget}
          onClose={() => setRpeTarget(null)}
          onSave={(rpe) => rpeMutation.mutate({ id: rpeTarget.id, rpe })}
          isPending={rpeMutation.isPending}
          isError={rpeMutation.isError}
        />
      )}
      {showLogModal  && (
        <LogFoodModal
          onClose={() => { setShowLogModal(false); setSelectedLog(null); setReviewJob(null); }}
          defaultDate={reviewJob?.date ?? logDate}
          defaultMeal={reviewJob?.meal_type ?? defaultMeal}
          onSaved={() => {
            onFoodSaved();
            // A reviewed job may be for another day than the one on screen.
            if (reviewJob) qc.invalidateQueries({ queryKey: ["nutrition-logs", reviewJob.date] });
          }}
          existingLog={selectedLog}
          job={reviewJob}
        />
      )}
    </div>
  );
}
