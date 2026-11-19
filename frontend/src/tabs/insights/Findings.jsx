import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api.js";
import { Card, Eyebrow, Hero, HeroSub, K, Sep, Foot, Legend, SegmentedTabs } from "../../components/ui.jsx";
import Summaries from "./Summaries.jsx";
import Levers from "./Levers.jsx";
import Collapse from "../../components/Collapse.jsx";
import TypicalValues from "./TypicalValues.jsx";

/**
 * Correlational findings, with the evidence shown beside every claim.
 *
 * WHAT CHANGED AND WHY
 * --------------------
 * The old layout listed surviving pairs as "What Works For You", with the
 * corroboration/holdout/controls machinery folded into a collapsed section at the
 * bottom. That inverts the importance: against 798 tested pairs, the fact that only
 * ~10 survive FDR — and that 0 of 24 synthetic controls do — is the reason any row
 * deserves attention at all. Those numbers now lead.
 *
 * THE DENOMINATOR
 * ---------------
 * "ρ = +0.55" means nothing without "out of 798 pairs scanned". Multiple-comparison
 * context is not a footnote here; it is the difference between a finding and a
 * coincidence, and it is shown first.
 *
 * THE LEDGER'S SUB-FLOOR ROWS
 * ---------------------------
 * `finding_ledger` had no minimum-n gate until 2026-08-15, so a few pairs were
 * registered at n=6-17 where |ρ|≥0.81 is reachable by chance. Those rows are NOT
 * edited — the ledger is a pre-registration instrument and rewriting it after
 * seeing outcomes destroys its only guarantee. They are labelled instead, and the
 * holdout column does the real work: the n=6 entry registered at ρ=+0.985 and now
 * reads ρ=−0.22.
 *
 * COLLINEAR CLUSTERS
 * ------------------
 * active_calories / exercise_minutes / daily_load / workout_hiit / hard_sets are
 * near-identical measures of training load. Five rows against the same outcome are
 * one finding, not five, and are grouped so they don't read as five discoveries.
 *
 * Since 2026-10-27 the families come from the backend (`INPUT_FAMILIES`, GET
 * /api/insights/families — was LOAD_FAMILY here): training dose and alcohol, fixed by
 * meaning, plus a data check that flags a member which stopped tracking its family and
 * lists ungrouped inputs that move together. Every family group is COLLAPSED by default
 * to its strongest row per outcome and expands (▸) to every measure — grouping only
 * changes how rows are shown, never which rows exist.
 */

/**
 * Rows sharing keyOf() collapse to one entry: the strongest row (confirmed before
 * suggestive, then |ρ|) stands for the rest. Keeps first-seen order, so pass sorted rows.
 */
function collapseRows(rows, keyOf) {
  const m = new Map();
  for (const r of rows) {
    const k = keyOf(r);
    if (!m.has(k)) m.set(k, []);
    m.get(k).push(r);
  }
  return [...m.entries()].map(([key, rs]) => {
    const sorted = [...rs].sort((a, b) =>
      (Number(b.tier === "confirmed") - Number(a.tier === "confirmed")) || (b.abs_r - a.abs_r));
    return { key, top: sorted[0], rows: sorted };
  });
}

/**
 * Reverse-lag verdict (backend `reverse_check_label`, 2026-10-27): the same input against the
 * outcome measured the day BEFORE it. Only the verdicts that should change how a row is read
 * get a tag in the action lists; `passes` is shown in Surviving only.
 */
const REV_TAG = {
  confounded: { text: "CONFOUNDED", color: "var(--over)",
    title: "The same link shows up the day BEFORE the input — a third factor (or the outcome itself) drives it, so it says nothing about what changing the input would do" },
  inconclusive: { text: "REVERSE-LAG UNCLEAR", color: "var(--ink-3)",
    title: "The day-before correlation is weaker, but too few days to show the forward one clearly beats it" },
  not_testable: { text: "REVERSE-LAG N/A", color: "var(--ink-3)",
    title: "A 7-day rolling input overlaps the day before, so this check cannot separate them" },
  passes: { text: "REV-LAG ✓", color: "var(--ink-3)",
    title: "The outcome the day before shows no comparable association — not explained by a slow confounder" },
};

function RevTag({ check, showPass = false }) {
  const t = REV_TAG[check];
  if (!t || (check === "passes" && !showPass)) return null;
  return (
    <span className="ml-1 whitespace-nowrap text-[8.5px] tracking-micro" style={{ color: t.color }}
          title={t.title}>
      {t.text}
    </span>
  );
}

/**
 * The same verdict as a line UNDER an action row. Those rows used to truncate with an ellipsis,
 * and a tag at the end of one was the first thing cut (13 of 22 clipped in the harness).
 */
const REV_NOTE = {
  inconclusive: "↺ day-before check unclear — too few days to tell",
  not_testable: "↺ day-before check n/a — 7-day rolling input",
};

function RevNote({ check, indent = false }) {
  const text = REV_NOTE[check];
  if (!text) return null;
  return (
    <p className={`text-[9px] leading-snug${indent ? " pl-3" : ""}`}
       style={{ color: "var(--ink-3)" }}
       title={REV_TAG[check]?.title}>
      {text}
    </p>
  );
}

/**
 * TRADE-OFF as a line under its row (2026-10-28). Inline at the end of a truncating row it
 * was the first thing the ellipsis cut ("TRAD…" on a phone) — same fix as RevNote.
 */
function TradeNote() {
  return (
    <p className="pl-3 text-[9px] leading-snug" style={{ color: "var(--breakfast)" }}>
      ⇄ TRADE-OFF — goes against this input's main direction
    </p>
  );
}

/**
 * The CONFOUNDED marker — same chip shape as MORE/LESS, outlined so it can't read as one.
 * `inline`: inside a wrapping row's text, so it wraps as a unit instead of taking a column
 * of its own and squeezing the text beside it to a word per line.
 */
function ConfChip({ inline = false }) {
  return (
    <span className={inline
            ? "ml-1 inline-block whitespace-nowrap rounded border px-1 align-baseline text-[8px] tracking-micro"
            : "flex-none rounded border px-1 text-[8px] tracking-micro"}
          style={{ borderColor: "var(--over)", color: "var(--over)" }}
          title={REV_TAG.confounded.title}>
      CONFOUNDED
    </span>
  );
}

// Why a pair never got a p-value (backend correlations.untestable_reason).
const UNTESTABLE_WHY = {
  by_design: "away nights are excluded from the analysis, so here it is always 'home'",
  few_pairs: "fewer than 5 days with both values",
  no_variation_input: "fewer than 10 days differ from its usual value",
  no_variation_outcome: "the outcome barely varies on those days",
  low_neff: "too few independent days once day-to-day carry-over is allowed for",
};

/** Numbers for prose: 2 decimals under 1, 1 under 10, else whole. Signed when asked. */
function fmtN(v, signed = false) {
  if (v == null || !isFinite(v)) return "—";
  const a = Math.abs(v);
  // Under 1: two significant figures, not two decimals ("0.0012", never "0.00").
  const s = a < 1 ? String(Number(a.toPrecision(2))) : a < 10 ? a.toFixed(1)
    : Math.round(a).toLocaleString("en-US");
  return (v < 0 ? "−" : signed ? "+" : "") + s;
}
// ρ always to two decimals ("+0.60", never "+0.6").
const fmtRho = (v) => (v == null ? "—" : `${v < 0 ? "−" : "+"}${Math.abs(v).toFixed(2)}`);
const fmtDay = (iso) => new Date(iso + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" });

/**
 * Survivor details (backend correlations.survivor_details, 2026-10-28): the effect in
 * natural units against the outcome's own spread, the input range the data covers, and
 * how much the result rests on a handful of days.
 */
function SurvivorDetails({ d }) {
  const yu = d.y_unit, xu = d.x_unit;
  // A per-unit slope that small ("+0.001 z per kcal") reads as nothing; express it per
  // 10ⁿ units so the number is ≥ 0.1 ("+1.0 z per 1,000 kcal").
  const per = !d.binary && d.slope && Math.abs(d.slope) < 0.1
    ? 10 ** Math.ceil(Math.log10(0.1 / Math.abs(d.slope))) : 1;
  const perTxt = per === 1 ? xu : `${per.toLocaleString("en-US")} ${xu}`;
  return (
    <div className="mt-1 space-y-0.5 rounded-lg bg-surface px-2 py-1.5 text-[10px] leading-snug text-ink-2">
      {d.binary ? (
        <p>
          Yes-days vs no-days: <span className="text-ink">{fmtN(d.slope, true)} {yu}</span>
          <span className="text-ink-3"> [{fmtN(d.slope_lo, true)}, {fmtN(d.slope_hi, true)}] · {d.n_yes} yes-days of {d.n}</span>
        </p>
      ) : (
        <p>
          <span className="text-ink">{fmtN(d.slope * per, true)} {yu} per {perTxt}</span>
          <span className="text-ink-3"> [{fmtN(d.slope_lo * per, true)}, {fmtN(d.slope_hi * per, true)}]</span>
          {" "}· across your usual range ({fmtN(d.x_p05)}–{fmtN(d.x_p95)} {xu}):{" "}
          <span className="text-ink">{fmtN(d.span_effect, true)} {yu}</span>
        </p>
      )}
      <p>
        That is <span className="text-ink">{fmtN(d.span_effect_sd)} SD</span> of the outcome
        <span className="text-ink-3"> (its usual spread: SD {fmtN(d.y_sd)} {yu})</span>
      </p>
      {!d.binary && (
        <p className="text-ink-3">
          Observed {fmtN(d.x_p05)}–{fmtN(d.x_p95)} {xu} (5–95% of days) — says nothing outside that range.
        </p>
      )}
      <p className={d.unstable ? "" : "text-ink-3"} style={d.unstable ? { color: "var(--breakfast)" } : undefined}>
        {d.unstable ? "Unstable: " : "Stable: "}dropping any one day gives ρ {fmtRho(d.drop1_lo)} to {fmtRho(d.drop1_hi)};
        a random 10% of days, {fmtRho(d.drop10_lo)} to {fmtRho(d.drop10_hi)}.
      </p>
      <p className="text-ink-3">
        Most influential days:{" "}
        {d.influential.map((e) => `${fmtDay(e.date)} (ρ ${fmtRho(e.rho_without)} without)`).join(", ")}
      </p>
    </div>
  );
}

/** ▸/▾ toggle, the app's expand convention. */
function ExpandToggle({ open, onClick, label }) {
  return (
    <button type="button" onClick={onClick} aria-expanded={open}
            className="flex-none text-[9px] tracking-micro text-ink-3 hover:text-ink-2">
      {open ? "▾" : "▸"} {label}
    </button>
  );
}

const VERDICT_TONE = {
  replicating: { color: "var(--rhr)", label: "replicating" },
  weakening:   { color: "var(--breakfast)", label: "weakening" },
  reversed:    { color: "var(--over)", label: "reversed" },
  accumulating:{ color: "var(--ink-3)", label: "accumulating" },
};

const LEDGER_MIN_N = 20;   // mirrors services/correlations.LEDGER_MIN_N

function RhoBar({ rho }) {
  // −1 … +1 with zero at centre. Length is the effect; side is the direction.
  if (rho == null) return null;
  const half = Math.min(Math.abs(rho), 1) * 50;
  const pos = rho >= 0;
  return (
    <div className="relative h-[9px] w-full overflow-hidden rounded-[2px] bg-surface">
      <div className="absolute inset-y-0 w-px bg-rule" style={{ left: "50%" }} />
      <div className="absolute inset-y-0"
        style={{
          left: pos ? "50%" : `${50 - half}%`,
          width: `${half}%`,
          backgroundColor: pos ? "var(--hrv)" : "var(--breakfast)",
          opacity: 0.8,
        }} />
    </div>
  );
}

export default function Findings() {
  const [view, setView] = useState("surviving");
  const [actView, setActView] = useState("actions");

  // MUST be wrapped, not passed bare: react-query calls queryFn with a
  // QueryFunctionContext, which would land in `days` and go out as
  // `?days=[object Object]` — FastAPI 422s and the view silently renders empty.
  const { data: coverage } = useQuery({
    queryKey: ["metric-coverage"], queryFn: api.getMetricCoverage, staleTime: 1000 * 60 * 60,
  });

  const { data: corr } = useQuery({
    queryKey: ["correlations", 180],
    queryFn: () => api.getCorrelations(180),
    staleTime: 1000 * 60 * 30,
  });
  const { data: validation } = useQuery({
    queryKey: ["validation"], queryFn: api.getValidation, staleTime: 1000 * 60 * 30,
  });
  // Same wrapping rule as above — this one takes (days, useCache).
  const { data: actionable } = useQuery({
    queryKey: ["actionable", 180],
    queryFn: () => api.getActionableInsights(180),
    staleTime: 1000 * 60 * 30,
  });

  const { data: familyData } = useQuery({
    queryKey: ["input-families"], queryFn: () => api.getInputFamilies(),
    staleTime: 1000 * 60 * 30,
  });
  // input key → its family (with each member's data check). Until it loads, nothing groups.
  const familyOf = useMemo(() => {
    const m = new Map();
    for (const f of familyData?.families ?? []) {
      for (const mem of f.members) m.set(mem.key, f);
    }
    return m;
  }, [familyData]);
  // Which family groups are expanded (keys are per view, so opening one never opens another).
  const [open, setOpen] = useState(() => new Set());
  const toggle = (k) => setOpen((s) => {
    const n = new Set(s);
    if (n.has(k)) n.delete(k); else n.add(k);
    return n;
  });

  const all = corr?.results ?? corr ?? [];
  const stats = useMemo(() => {
    const rows = Array.isArray(all) ? all : [];
    const real = rows.filter((r) => !String(r.metric_a).startsWith("ctrl_")
                                 && !String(r.metric_b).startsWith("ctrl_"));
    const ctrl = rows.filter((r) => String(r.metric_a).startsWith("ctrl_")
                                 || String(r.metric_b).startsWith("ctrl_"));
    // "Tested" = pairs that got a p-value, i.e. the Benjamini–Hochberg family (2026-10-28).
    // It used to be every pair, which counted 133 that never reached a test.
    const untestable = real.filter((r) => r.p_value == null);
    const untestGroups = {};
    for (const r of untestable) {
      const outcomeSide = r.untestable_reason === "no_variation_outcome";
      const k = `${outcomeSide ? r.metric_b : r.metric_a}|${r.untestable_reason}`;
      untestGroups[k] ??= { label: outcomeSide ? (r.metric_b_label ?? r.metric_b) : (r.metric_a_label ?? r.metric_a),
                            reason: r.untestable_reason, n: 0 };
      untestGroups[k].n += 1;
    }
    return {
      tested: real.length - untestable.length,
      untestable: untestable.length,
      untestGroups: Object.values(untestGroups).sort((a, b) => b.n - a.n),
      sig: real.filter((r) => r.significant),
      sug: real.filter((r) => r.suggestive),
      ctrlN: ctrl.length,
      ctrlPassing: ctrl.filter((r) => r.significant || r.suggestive).length,
      ctrlMaxRho: ctrl.reduce((m, r) => Math.max(m, Math.abs(r.spearman_r ?? 0)), 0),
      // Reverse-lag verdicts over every surviving pair (confirmed + suggestive).
      rev: real.filter((r) => r.reverse_check).reduce((c, r) => {
        c[r.reverse_check] = (c[r.reverse_check] ?? 0) + 1;
        return c;
      }, {}),
      computedAt: rows[0]?.computed_at ?? corr?.computed_at ?? null,
    };
  }, [all, corr]);

  // Group family rows that share an outcome — one finding, not five.
  const grouped = useMemo(() => {
    const out = [];
    const byOutcome = new Map();
    for (const r of stats.sig) {
      const fam = familyOf.get(r.metric_a);
      if (fam) {
        const k = `${fam.id}|${r.metric_b}|${r.lag_days}`;
        if (!byOutcome.has(k)) byOutcome.set(k, { fam, rows: [] });
        byOutcome.get(k).rows.push(r);
      } else out.push({ kind: "single", row: r });
    }
    for (const [k, { fam, rows }] of byOutcome) {
      if (rows.length > 1) out.push({ kind: "cluster", key: k, fam, rows, outcome: rows[0] });
      else out.push({ kind: "single", row: rows[0] });
    }
    return out.sort((a, b) => {
      const ra = a.kind === "cluster" ? Math.max(...a.rows.map((x) => Math.abs(x.spearman_r))) : Math.abs(a.row.spearman_r);
      const rb = b.kind === "cluster" ? Math.max(...b.rows.map((x) => Math.abs(x.spearman_r))) : Math.abs(b.row.spearman_r);
      return rb - ra;
    });
  }, [stats.sig, familyOf]);

  // NOTE 36: where every surviving pair went. Without this "10 survive" sat above "7
  // confirmed" with nothing connecting them — the other 3 were real findings routed
  // elsewhere (trade-offs, inputs you cannot change), not missing ones. The backend
  // records a route for every significant/suggestive pair, including the ones it drops.
  const ROUTE_META = {
    act:               { tag: "ACT ON",           label: "to act on" },
    trade_off:         { tag: "TRADE-OFF",        label: "trade-off" },
    cannot_change:     { tag: "CAN'T CHANGE",     label: "you can't change" },
    confounded:        { tag: "CONFOUNDED",       label: "confounded (same link the day before)" },
    below_action_bar:  { tag: "BELOW ACTION BAR", label: "below the action bar (|ρ|<0.2 or n<30)" },
    not_input_outcome: { tag: "NOT A LEVER",      label: "not an input → outcome pair" },
  };
  const routeOf = useMemo(() => {
    const m = new Map();
    for (const rt of actionable?.routes ?? []) m.set(`${rt.metric_a}|${rt.metric_b}|${rt.lag_days}`, rt.route);
    return m;
  }, [actionable]);
  const routeFor = (r) => routeOf.get(`${r.metric_a}|${r.metric_b}|${r.lag_days}`);
  const breakdown = useMemo(() => {
    const c = {};
    for (const r of stats.sig) {
      const k = routeFor(r) ?? "unrouted";
      c[k] = (c[k] ?? 0) + 1;
    }
    return c;
  }, [stats.sig, routeOf]); // eslint-disable-line react-hooks/exhaustive-deps

  const vres = validation?.results ?? [];
  const subFloor = vres.filter((v) => (v.n_at_entry ?? 99) < LEDGER_MIN_N);
  const controls = validation?.controls;

  /**
   * Year-over-year drift. NOT a second estimate to average in — the windows are
   * deliberately non-overlapping so the comparison answers "did this change?"
   * without ever pooling across the change.
   *
   * Silent unless the prior year has a real sample (prev_n >= LEDGER_MIN_N) AND the
   * shift is larger than the two windows' combined noise. Drawing an arrow on every
   * pair would turn sampling noise into a narrative.
   */
  const Drift = ({ r }) => {
    const pr = r.prev_spearman_r, pn = r.prev_n;
    if (pr == null || !pn || pn < LEDGER_MIN_N || r.spearman_r == null) return null;
    const z = (v) => 0.5 * Math.log((1 + v) / (1 - v));           // Fisher
    const se = Math.sqrt(1 / Math.max(pn - 3, 1) + 1 / Math.max((r.n ?? 0) - 3, 1));
    const dz = z(r.spearman_r) - z(pr);
    if (!isFinite(dz) || Math.abs(dz) < 2 * se) {
      return <span className="flex-none text-[9px] text-ink-3" title={`last year ${pr.toFixed(2)} (n=${pn})`}>· stable</span>;
    }
    return (
      <span className="flex-none text-[9px]" style={{ color: "var(--breakfast)" }}
            title={`last year ${pr.toFixed(2)} (n=${pn}) → this year ${r.spearman_r.toFixed(2)}`}>
        · shifted from {pr > 0 ? "+" : ""}{pr.toFixed(2)}
      </span>
    );
  };

  /**
   * "Accruing since <date>" for inputs that are simply too young to have an answer yet.
   *
   * n alone cannot distinguish "you rarely do this" from "this metric is eight days
   * old", and without the distinction a brand-new input reads as a failed hypothesis.
   * Shown only when the metric is younger than NEW_METRIC_DAYS AND the pair is thin —
   * an old metric with low n really does mean "you rarely do this", and should not be
   * excused.
   */
  const NEW_METRIC_DAYS = 120;
  const Accruing = ({ r }) => {
    const cov = coverage?.[r.metric_a] ?? coverage?.[r.metric_b];
    if (!cov || (r.n ?? 0) >= LEDGER_MIN_N) return null;
    const age = Math.round((Date.now() - new Date(`${cov.first}T12:00:00`)) / 86400000);
    if (age > NEW_METRIC_DAYS) return null;
    return (
      <p className="mt-0.5 text-[9.5px] leading-snug" style={{ color: "var(--hrv)" }}>
        new — collecting since {cov.first} ({cov.days} day{cov.days === 1 ? "" : "s"} of
        data). Too young to answer yet, not a null result.
      </p>
    );
  };

  const Row = ({ r, sub }) => (
    <div className="py-1.5">
      <div className="flex items-baseline gap-2">
        <span className="prose-ui min-w-0 flex-1 truncate text-[11.5px] text-ink">
          {r.metric_a_label ?? r.metric_a} → {r.metric_b_label ?? r.metric_b}
        </span>
        <span className="flex-none text-[11px] font-semibold tabular-nums text-ink">
          {r.spearman_r > 0 ? "+" : ""}{r.spearman_r?.toFixed(2)}
        </span>
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5">
        <RhoBar rho={r.spearman_r} />
        <span className="flex-none text-[9px] tabular-nums text-ink-3">
          n={r.n}{r.days_window ? ` · ${Math.round(r.days_window / 365) === 1 ? "1y" : `${r.days_window}d`}` : ""}
          {r.lag_days ? ` · lag ${r.lag_days}d` : " · same day"}
        </span>
        <Drift r={r} />
        <RevTag check={r.reverse_check} showPass />
        {r.survivor_details?.unstable && (
          <span className="whitespace-nowrap text-[8.5px] tracking-micro" style={{ color: "var(--breakfast)" }}
                title="Dropping one day, or a random 10% of days, can change the sign or reach zero">
            UNSTABLE
          </span>
        )}
        {routeFor(r) && (
          <span className="flex-none text-[8.5px] tracking-micro text-ink-3">
            {ROUTE_META[routeFor(r)]?.tag ?? routeFor(r)}
          </span>
        )}
      </div>
      {/* Same bar as the ledger gate. A pair can clear FDR on thin data — HIIT day
          x day quality does, at n=17 — and saying so here keeps one standard for
          "enough evidence" across the whole view. */}
      {(r.n ?? 99) < LEDGER_MIN_N && (
        <p className="mt-0.5 text-[9.5px] leading-snug" style={{ color: "var(--breakfast)" }}>
          thin — only {r.n} paired days; survives correction but is one good week from
          changing
        </p>
      )}
      <Accruing r={r} />
      {sub && <p className="mt-0.5 text-[9.5px] leading-snug text-ink-3">{sub}</p>}
      {r.survivor_details && (() => {
        const k = `det|${r.metric_a}|${r.metric_b}|${r.lag_days}`;
        return (
          <>
            <div className="mt-0.5">
              <ExpandToggle open={open.has(k)} onClick={() => toggle(k)} label="DETAILS" />
            </div>
            {open.has(k) && <SurvivorDetails d={r.survivor_details} />}
          </>
        );
      })()}
    </div>
  );

  // Confirmed levers only. Routing changed 2026-09-07 (note 16): the backend now splits
  // on whether the INPUT can be changed, not on whether the outcome is subjective — the
  // old rule filed "less alcohol → better HRV" as a non-actionable diagnostic
  // while calling sleep duration a lever. `diagnostic` now holds only inputs you cannot
  // act on at all (weather, gait readouts), and a third bucket, `trade_offs`, holds
  // controllable inputs whose effects point BOTH ways across outcomes — real findings
  // that must not be phrased as instructions.
  const acts = [
    ...(actionable?.do_more ?? []).map((x) => ({ ...x, dir: "more" })),
    ...(actionable?.do_less ?? []).map((x) => ({ ...x, dir: "less" })),
  ].sort((a, b) => b.abs_r - a.abs_r);

  // NOTE 25: organised BY ACTION, not ordered by correlation value. Sorting a flat list
  // by |rho| split one lever across the page — "Less alcohol" held positions 4, 5 and 6
  // with unrelated rows above it, so the action with the MOST independent support read
  // as three weak scattered findings instead of one well-evidenced one.
  //
  // Keyed on direction+input so a hypothetical same-input-both-ways case becomes two
  // groups rather than silently merging under whichever direction sorted first. The
  // backend's trade-off guard should prevent that, but this must not be the place that
  // quietly hides it if the guard ever fails.
  const suggestedActs = [
    ...(actionable?.do_more_suggestive ?? []).map((x) => ({ ...x, dir: "more" })),
    ...(actionable?.do_less_suggestive ?? []).map((x) => ({ ...x, dir: "less" })),
  ].sort((a, b) => b.abs_r - a.abs_r);
  const tradeOffs = actionable?.trade_offs ?? [];
  // CONFOUNDED rows (2026-10-27): shown in their group for context, dimmed and last, never
  // a lever — the backend keeps them out of direction voting and out of trade-offs.
  const confRows = useMemo(
    () => (actionable?.confounded ?? []).map((x) => ({ ...x, dir: x.beneficial ? "more" : "less" })),
    [actionable]);
  // BY ACTION holds EXACTLY the rows BY OUTCOME holds, in line (note 38, 2026-09-15):
  // unconfirmed and trade-off rows sit inside their action's group with a tag, instead of
  // two piles at the end of the card. One group per INPUT. Its direction comes from its
  // lever rows (confirmed or not — the backend gives an input one consensus direction);
  // a row that points the other way is a TRADE-OFF inside the same group, which is where
  // "more sleep buys sleep quality and costs early-night HR" is actually legible. An
  // input with no lever rows at all is MIXED.
  // Groups sort exactly as BY OUTCOME does — felt first, then number of effects, then
  // strength — so confirmation is shown by tags, never by position.
  const actGroups = useMemo(() => {
    const m = new Map();
    const tradeRows = tradeOffs.map((t) => ({ ...t, dir: t.beneficial ? "more" : "less" }));
    // A family's members share ONE group (2026-10-27); everything else stays one group
    // per input. The group's direction is still taken from its lever rows below.
    for (const a of [...acts, ...suggestedActs, ...tradeRows, ...confRows]) {
      const fam = familyOf.get(a.input_key);
      const gk = fam ? `fam:${fam.id}` : a.input_key;
      if (!m.has(gk)) m.set(gk, { key: gk, input: fam ? fam.label : a.input, fam, rows: [] });
      m.get(gk).rows.push(a);
    }
    return [...m.values()]
      .map((g) => {
        const leverDirs = [...new Set(g.rows.filter((r) => !r.conflicting && !r.confounded).map((r) => r.dir))];
        const rows = [...g.rows].sort((x, y) => (Number(x.confounded) - Number(y.confounded))
          || (Number(y.felt) - Number(x.felt)) || (y.abs_r - x.abs_r));
        return {
          ...g,
          // Every row confounded → the group itself is labelled so; it is not a lever.
          dir: g.rows.every((r) => r.confounded) ? "confounded"
            : leverDirs.length === 1 ? leverDirs[0] : "mixed",
          rows,
          anyFelt: g.rows.some((r) => r.felt),
          maxR: Math.max(...g.rows.map((r) => r.abs_r)),
          // Collapsed view of a family: its strongest row per effect (+ how many
          // measures sit behind it). Sort uses EFFECTS, so a family is not promoted
          // for being measured more ways. For a single input this is just its rows.
          // Keyed on the EFFECT (outcome, lag, sign of ρ), not on each measure's lever /
          // trade-off label: two measures of one family pointing the same way at one
          // outcome are one effect even if the backend labelled them differently.
          effects: collapseRows(rows, (r) => `${r.outcome_key}|${r.lag_days}|${r.raw_r > 0}|${!!r.confounded}`),
        };
      })
      .sort((x, y) => (Number(y.anyFelt) - Number(x.anyFelt))
                   || (y.effects.length - x.effects.length)
                   || (y.maxR - x.maxR));
  }, [acts, suggestedActs, tradeOffs, confRows, familyOf]);
  // Headline counts by EFFECT, not by row (2026-10-28): a family's measures pointing the
  // same way at one outcome are one line in both views, so they are one finding here too.
  // An effect takes its category from the row that stands for it (confirmed first).
  const effectCounts = useMemo(() => {
    const c = { total: 0, confirmed: 0, trade: 0, sug: 0, conf: 0 };
    for (const g of actGroups) {
      for (const e of g.effects) {
        const t = e.top;
        c.total += 1;
        if (t.confounded) c.conf += 1;
        else if (t.conflicting) c.trade += 1;
        else if (t.tier === "confirmed") c.confirmed += 1;
        else c.sug += 1;
      }
    }
    return c;
  }, [actGroups]);
  const rowCount = acts.length + tradeOffs.length + suggestedActs.length + confRows.length;
  // NOTE 19: the same levers, indexed by what they improve. Assessed as premature in the
  // morning (four surviving pairs across two outcomes would have rendered two near-empty
  // groups); the note-16 routing fix changed that to 11 levers across 7 outcomes, so it
  // now earns its place. Deliberately a TOGGLE on the same card, not a second card —
  // it is one dataset seen from the other end, and duplicating it as a sibling card
  // would read as two different findings.
  const byOutcome = useMemo(() => {
    const m = new Map();
    // Trade-off rows included (notes 30 + 36): for ONE outcome a trade-off row has a
    // clear direction, and BY ACTION shows the same rows in its MIXED section — the two
    // views must hold exactly the same entries.
    const tradeRows = tradeOffs.map((t) => ({ ...t, dir: t.beneficial ? "more" : "less" }));
    for (const r of [...acts, ...tradeRows, ...suggestedActs, ...confRows]) {
      if (!m.has(r.outcome_key)) {
        m.set(r.outcome_key, { outcome: r.outcome, felt: r.felt, rows: [] });
      }
      m.get(r.outcome_key).rows.push(r);
    }
    // Outcomes you FEEL first, then by how much evidence there is — same precedence the
    // backend uses when it decides which direction a lever points.
    return [...m.values()]
      .map((g) => {
        const rows = [...g.rows].sort((a, b) => (Number(a.confounded) - Number(b.confounded))
          || (b.abs_r - a.abs_r));
        // Family members pointing the same way at this outcome collapse to one line.
        const entries = collapseRows(rows, (r) => {
          const fam = familyOf.get(r.input_key);
          return fam ? `fam|${fam.id}|${r.lag_days}|${r.dir}|${!!r.confounded}`
                     : `in|${r.input_key}|${r.lag_days}`;
        }).map((e) => ({ ...e, fam: familyOf.get(e.top.input_key) }));
        return { ...g, rows, entries };
      })
      .sort((a, b) => (Number(b.felt) - Number(a.felt)) || (b.entries.length - a.entries.length));
  }, [acts, suggestedActs, tradeOffs, confRows, familyOf]);
  // (tradeByInput retired 2026-09-15: trade-off rows now sit inside their input's group
  // in actGroups, which keeps both sides of a trade together the same way.)

  // One BY OUTCOME line. `label` is the input (or a family's name on its collapsed line);
  // `toggle` is the ▸ control a family line carries.
  const outcomeLine = (r, label, key, indent, toggle) => (
    <div key={key}>
    <div className={`mt-0.5 flex items-baseline gap-2${indent ? " pl-3" : ""}${r.confounded ? " opacity-60" : ""}`}>
      {/* A confounded row gets no MORE/LESS — under "To improve X" that would read as advice. */}
      {r.confounded ? <ConfChip /> : (
      <span className="flex-none rounded px-1 py-px text-[8px] tracking-micro"
        style={{
          background: r.dir === "more"
            ? "color-mix(in srgb, var(--rhr) 18%, transparent)"
            : "color-mix(in srgb, var(--over) 18%, transparent)",
          color: r.dir === "more" ? "var(--rhr)" : "var(--over)",
        }}>
        {r.dir === "more" ? "MORE" : "LESS"}
      </span>
      )}
      <span className="prose-ui min-w-0 flex-1 break-words text-[11.5px] text-ink-2">
        {label}
        {/* This view mixes confirmed and suggestive rows, so it must say
            which is which (note 30) — BY ACTION separates them spatially. */}
        {r.tier === "suggestive" && (
          <>
            {/* Two pieces: UNCONFIRMED stays whole; the proxy part may wrap at its spaces. */}
            <span className="ml-1 whitespace-nowrap text-[8.5px] tracking-micro text-ink-3">UNCONFIRMED</span>
            {r.proxy_only && (
              <span className="text-[8.5px] tracking-micro text-ink-3"> · BODY MARKERS ONLY</span>
            )}
          </>
        )}
        {/* In the text flow, not a column: as a column it left the label 65 px on a phone. */}
        {toggle && <span className="ml-1 whitespace-nowrap">{toggle}</span>}
      </span>
      <span className="flex-none text-[10.5px] tabular-nums text-ink-3">
        {r.raw_r > 0 ? "+" : ""}{r.raw_r?.toFixed(2)} · n={r.n}
      </span>
    </div>
    {r.conflicting && <TradeNote />}
    <RevNote check={r.reverse_check} indent />
    </div>
  );

  return (
    <div className="space-y-4 p-4">
      {/* ── What to actually do ────────────────────────────────────────────────
          Top of the view because it answers the question the tab exists for. It is
          deliberately a SHORT list: of the 10 pairs that survive correction, only a
          few have an input you can change and an outcome you can feel. The rest are
          diagnostics — real, but not instructions. */}
      <Levers />

      {(acts.length > 0 || suggestedActs.length > 0) && (
        <Collapse title="ALL FINDINGS" hint={`${effectCounts.total}`}>
        <Card>
          <Eyebrow left="WHAT TO ACT ON" right={`${effectCounts.confirmed} CONFIRMED`} />
          <div className="mt-2">
            <SegmentedTabs
              value={actView} onChange={setActView}
              options={[
                { value: "actions", label: "BY ACTION" },
                { value: "outcomes", label: "BY OUTCOME" },
              ]}
            />
          </div>
          {/* Note 36: the two views are one dataset. Saying the count makes that checkable. */}
          <p className="mt-1.5 text-[9.5px] leading-snug text-ink-3">
            Both views hold the same {effectCounts.total} findings
            {rowCount !== effectCounts.total && <> ({rowCount} measured pairs — related measures count once)</>}:{" "}
            {effectCounts.confirmed} confirmed, {effectCounts.trade} trade-off, {effectCounts.sug} not yet confirmed
            {effectCounts.conf > 0 && <>, {effectCounts.conf} confounded</>}.
          </p>
          {actView === "outcomes" && (
            <div className="mt-2 divide-y divide-rule">
              {byOutcome.length === 0 && (
                <HeroSub>Nothing has cleared the bar yet.</HeroSub>
              )}
              {byOutcome.map((g) => (
                <div key={g.outcome} className="py-2">
                  <p className="prose-ui text-[12px] font-semibold text-ink">
                    To improve {g.outcome}
                    {!g.felt && (
                      <span className="ml-1.5 text-[9px] font-normal tracking-micro text-ink-3">
                        PHYSIOLOGICAL
                      </span>
                    )}
                  </p>
                  {g.entries.map((e) => {
                    const k = `out|${g.outcome}|${e.key}`;
                    if (!e.fam || e.rows.length < 2) {
                      return outcomeLine(e.top, e.top.input, k, false, null);
                    }
                    // A family: collapsed to its strongest measure; ▸ lists every one.
                    const isOpen = open.has(k);
                    return (
                      <div key={k}>
                        {outcomeLine(e.top, e.fam.label, `${k}|h`, false,
                          <ExpandToggle open={isOpen} onClick={() => toggle(k)}
                                        label={`${e.rows.length} MEASURES`} />)}
                        {isOpen && e.rows.map((r) => outcomeLine(r, r.input, `${k}|${r.input_key}`, true, null))}
                      </div>
                    );
                  })}
                </div>
              ))}
            </div>
          )}

          {actView === "actions" && acts.length === 0 && (
            <HeroSub>
              Nothing has cleared the confirmed bar yet — every row below is tagged UNCONFIRMED.
            </HeroSub>
          )}
          {actView === "actions" && (
          <div className="mt-2 divide-y divide-rule">
            {actGroups.map((g) => (
              <div key={g.key} className="py-2">
                <div className="flex items-baseline gap-2">
                  {g.dir === "confounded" ? <ConfChip /> : (
                  <span className="flex-none rounded px-1 py-px text-[8px] tracking-micro"
                    style={{
                      background: g.dir === "more"
                        ? "color-mix(in srgb, var(--rhr) 18%, transparent)"
                        : g.dir === "less"
                          ? "color-mix(in srgb, var(--over) 18%, transparent)"
                          : "color-mix(in srgb, var(--breakfast) 18%, transparent)",
                      color: g.dir === "more" ? "var(--rhr)" : g.dir === "less" ? "var(--over)" : "var(--breakfast)",
                    }}>
                    {g.dir === "more" ? "MORE" : g.dir === "less" ? "LESS" : "MIXED"}
                  </span>
                  )}
                  <span className="prose-ui min-w-0 flex-1 truncate text-[12px] font-semibold text-ink">
                    {g.input}
                  </span>
                  {/* The count is the headline for a grouped action: three independent
                      outcomes moving the same way is a different claim from one. */}
                  {g.effects.length > 1 && (
                    <span className="flex-none text-[9px] tracking-micro text-ink-3">
                      {g.effects.length} EFFECTS
                    </span>
                  )}
                  {/* A family shows one line per effect; ▸ lists every measure behind them. */}
                  {g.fam && new Set(g.rows.map((r) => r.input_key)).size > 1 && (
                    <ExpandToggle open={open.has(`act|${g.key}`)} onClick={() => toggle(`act|${g.key}`)}
                                  label={`${new Set(g.rows.map((r) => r.input_key)).size} MEASURES`} />
                  )}
                </div>
                {/* One line per effect, the same shape as BY OUTCOME (2026-10-21) — the
                    n_eff / CI line made this view twice as long for numbers that live in
                    the Surviving list below anyway. */}
                {(g.fam && !open.has(`act|${g.key}`) ? g.effects.map((e) => e.top) : g.rows).map((a) => (
                  <div key={`${a.input_key}${a.outcome_key}${a.lag_days}`}>
                  <div className={`mt-0.5 flex items-baseline gap-2${a.confounded ? " opacity-60" : ""}`}>
                    <span className="prose-ui min-w-0 flex-1 break-words text-[11.5px] text-ink-2">
                      {/* Expanded, a family's lines name the measure they came from. */}
                      {g.fam && open.has(`act|${g.key}`) ? `${a.input} ` : ""}
                      {/* A trade-off row states its own direction: it points against the
                          group's, so "→ Early-night HR" alone would read as a benefit. */}
                      {/* A confounded row describes the link rather than follow the group's lever. */}
                      → {a.conflicting || a.confounded ? `${a.raw_r > 0 ? "higher" : "lower"} ` : ""}{a.outcome}
                      {a.lag_days ? " (next day)" : ""}
                      {!a.felt && (
                        <span className="ml-1 whitespace-nowrap text-[8.5px] tracking-micro text-ink-3">PHYS</span>
                      )}
                      {a.tier === "suggestive" && (
                        <>
                          {/* Two pieces: UNCONFIRMED stays whole; the proxy part may wrap at its spaces. */}
                          <span className="ml-1 whitespace-nowrap text-[8.5px] tracking-micro text-ink-3">UNCONFIRMED</span>
                          {a.proxy_only && (
                            <span className="text-[8.5px] tracking-micro text-ink-3"> · BODY MARKERS ONLY</span>
                          )}
                        </>
                      )}
                      {a.confounded && g.dir !== "confounded" && <ConfChip inline />}
                    </span>
                    <span className="flex-none text-[10.5px] tabular-nums text-ink-3">
                      {a.raw_r > 0 ? "+" : ""}{a.raw_r?.toFixed(2)} · n={a.n}
                    </span>
                  </div>
                  {a.conflicting && <TradeNote />}
                  <RevNote check={a.reverse_check} indent />
                  </div>
                ))}
              </div>
            ))}
          </div>
          )}
          {/* The Trade-offs explanation, kept (it used to head the MIXED pile). */}
          {tradeOffs.length > 0 && (
            <p className="prose-ui mt-2 text-[10px] leading-snug text-ink-3">
              TRADE-OFF rows point against their group's direction (MIXED: no single direction),
              so they are stated rather than prescribed. Where something you FEEL disagrees with
              a physiological marker, the felt outcome sets the direction. A cost on one marker
              is not a reason to stop — it is a reason to know what you are buying.
            </p>
          )}
          {confRows.length > 0 && (
            <p className="prose-ui mt-2 text-[10px] leading-snug text-ink-3">
              <span style={{ color: "var(--over)" }}>CONFOUNDED</span> rows show the same link
              the day <em>before</em> the input, which the input cannot have caused — a third
              factor, or the outcome itself driving the input. Real as a pattern, but they say
              nothing about what changing the input would do, so they are never advice.
            </p>
          )}


          <Foot>
            Only pairs whose INPUT is something you can change. The outcome may be
            something you feel or a physiological readout — both are listed, felt ones
            first, because a reliable effect on HRV is worth acting on even though you
            cannot feel it directly. Inputs you cannot change at all (weather) are counted
            in Evidence below and tagged CAN'T CHANGE in the surviving list. Still
            correlational: the Experiments view is where a lever gets tested directly.
          </Foot>
        </Card>
        </Collapse>
      )}

      {/* The standalone Trade-offs card moved INTO "What to act on" (its MIXED section)
          2026-09-14, notes 30 + 36 — same rows, same text, now in the one card that
          BY OUTCOME also draws from, so the two views hold identical entries. */}

      {/* ── The denominator ────────────────────────────────────────────────── */}
      <Card>
        <Eyebrow left="EVIDENCE" right={stats.computedAt ? String(stats.computedAt).slice(0, 10) : null} />
        <Hero value={stats.sig.length} unit={["SURVIVE", `OF ${stats.tested} TESTED`]} />
        <HeroSub>
          Every pair is tested at once and corrected for multiplicity (Benjamini–Hochberg).
          At this scale, roughly <K>{Math.round(stats.tested * 0.05)}</K> pairs would clear
          an uncorrected p&lt;0.05 by chance alone — which is why the raw p-value is not
          the bar here. <K>{stats.sug.length}</K> more sit in the suggestive band.
        </HeroSub>
        {/* Note 36: the surviving count, accounted for. Every surviving pair lands in
            exactly one of these, so they sum to the number above. */}
        {stats.sig.length > 0 && Object.keys(breakdown).length > 0 && (
          <p className="prose-ui mt-2 text-[10.5px] leading-snug text-ink-2">
            Of the <K>{stats.sig.length}</K>:{" "}
            {Object.entries(breakdown)
              .sort((a, b) => b[1] - a[1])
              .map(([k, n]) => `${n} ${ROUTE_META[k]?.label ?? "not routed"}`)
              .join(" · ")}
            . Both "to act on" and trade-off rows appear above, inside their action's group
            (trade-offs tagged TRADE-OFF); a pair you can't change is listed only here and
            in Surviving below.
          </p>
        )}

        {/* Pairs that never reached a test (2026-10-28, addendum item 12): not in the
            multiple-testing family, so not in the count above — listed with the reason. */}
        {stats.untestable > 0 && (
          <div className="mt-3 border-t border-rule pt-3">
            <div className="flex items-baseline justify-between">
              <p className="text-[9px] tracking-micro text-ink-3">NOT TESTABLE</p>
              <p className="text-[11px] font-semibold text-ink-2">{stats.untestable} pairs</p>
            </div>
            <p className="prose-ui mt-1 text-[10.5px] leading-snug text-ink-2">
              These never reached a test — mostly inputs that hardly vary in your data — so
              they are not part of the correction above, which covers the{" "}
              <K>{stats.tested}</K> that did.
            </p>
            <div className="mt-1">
              <ExpandToggle open={open.has("untestable")} onClick={() => toggle("untestable")}
                            label="WHICH AND WHY" />
            </div>
            {open.has("untestable") && (
              <div className="mt-1 space-y-0.5">
                {stats.untestGroups.map((g) => (
                  <p key={`${g.label}|${g.reason}`} className="prose-ui text-[10px] leading-snug text-ink-2">
                    <span className="text-ink">{g.label}</span>
                    <span className="text-ink-3"> · {g.n} pair{g.n === 1 ? "" : "s"} — {UNTESTABLE_WHY[g.reason] ?? "not testable"}</span>
                  </p>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Controls promoted out of the collapsed section — this is the single best
            evidence that the tier means anything. */}
        {stats.ctrlN > 0 && (
          <div className="mt-3 border-t border-rule pt-3">
            <div className="flex items-baseline justify-between">
              <p className="text-[9px] tracking-micro text-ink-3">SYNTHETIC CONTROLS</p>
              <p className="text-[11px] font-semibold"
                style={{ color: stats.ctrlPassing === 0 ? "var(--rhr)" : "var(--over)" }}>
                {stats.ctrlPassing} of {stats.ctrlN} passed
              </p>
            </div>
            <p className="prose-ui mt-1 text-[10.5px] leading-snug text-ink-2">
              {stats.ctrlPassing === 0
                ? <>Date parity and a random walk are correlated against everything, and
                    should never survive. None did — the strongest reached ρ={stats.ctrlMaxRho.toFixed(2)}
                    {" "}raw and was rejected after correction. That is the check that makes
                    the {stats.sig.length} above worth reading.</>
                : <>A control survived correction. The false-positive machinery is leaking
                    and every finding below should be discounted until that is understood.</>}
            </p>
          </div>
        )}
        {Object.keys(stats.rev).length > 0 && (
          <div className="mt-3 border-t border-rule pt-3">
            <div className="flex items-baseline justify-between">
              <p className="text-[9px] tracking-micro text-ink-3">REVERSE-LAG CHECK</p>
              <p className="text-[11px] font-semibold text-ink-2">
                {stats.rev.passes ?? 0} of {Object.values(stats.rev).reduce((a, b) => a + b, 0)} pass
              </p>
            </div>
            <p className="prose-ui mt-1 text-[10.5px] leading-snug text-ink-2">
              Each surviving pair is also tested against the outcome measured the day
              <K> before</K> the input — which the input cannot have caused. A slow
              confounder or reverse causation shows up both ways; a real short-acting effect
              shows up forward only.{" "}
              {stats.rev.confounded > 0 && <><K>{stats.rev.confounded}</K> are <K>CONFOUNDED</K>:
                the same link is clearly there the day before, so something other than the input
                drives it — a third factor, or the outcome driving the input. They stay visible
                as context, never as advice. </>}
              {stats.rev.inconclusive > 0 && <><K>{stats.rev.inconclusive}</K> are weaker
                backwards but on too few days to tell (REVERSE-LAG UNCLEAR). </>}
              {stats.rev.not_testable > 0 && <><K>{stats.rev.not_testable}</K> use a 7-day
                rolling input, which the check cannot separate from the day before. </>}
            </p>
          </div>
        )}
        <Foot>
          Spearman ρ with Pyper–Peterman effective-n (autocorrelation-corrected) and
          Bonett–Wright intervals. Correlation here is co-occurrence, never cause —
          the Experiments view is the only one that tests a lever directly.
        </Foot>
      </Card>

      <SegmentedTabs
        value={view} onChange={setView}
        options={[
          { value: "surviving", label: "SURVIVING" },
          { value: "holdout", label: "HOLDOUT" },
        ]}
      />

      {/* ── Surviving pairs ────────────────────────────────────────────────── */}
      {view === "surviving" && (
        <>
          {grouped.length === 0 && (
            <Card><HeroSub>Nothing survives correction in the current window.</HeroSub></Card>
          )}
          {grouped.map((g, i) => {
            if (g.kind !== "cluster") {
              return <Card key={`s${i}`}><Row r={g.row} /></Card>;
            }
            const k = `surv|${g.key}`;
            const rows = [...g.rows].sort((a, b) => Math.abs(b.spearman_r) - Math.abs(a.spearman_r));
            const offTrack = g.fam.members.filter((m) => m.tracks === false
              && rows.some((r) => r.metric_a === m.key));
            return (
              <Card key={k}>
                <Eyebrow left={g.fam.label.toUpperCase()} right={`${g.rows.length} MEASURES AGREE`} />
                <p className="prose-ui mt-2 text-[12px] font-semibold leading-snug text-ink">
                  {g.fam.label} → {g.outcome.metric_b_label ?? g.outcome.metric_b}
                </p>
                <HeroSub>
                  These inputs are {g.fam.blurb}, so this is <K>one</K> finding measured{" "}
                  {g.rows.length} ways — not {g.rows.length} independent discoveries.
                </HeroSub>
                {/* Collapsed: the strongest measure stands for the finding. */}
                <div className="mt-2 divide-y divide-rule">
                  {(open.has(k) ? rows : rows.slice(0, 1))
                    .map((r) => <Row key={`${r.metric_a}${r.lag_days}`} r={r} />)}
                </div>
                <div className="mt-1">
                  <ExpandToggle open={open.has(k)} onClick={() => toggle(k)}
                    label={open.has(k) ? "SHOW STRONGEST ONLY" : `ALL ${rows.length} MEASURES`} />
                </div>
                {open.has(k) && offTrack.length > 0 && (
                  <p className="mt-1 text-[9.5px] leading-snug" style={{ color: "var(--breakfast)" }}>
                    {offTrack.map((m) => `${m.label} (median |ρ| ${m.median_abs_rho?.toFixed(2)})`).join(", ")}
                    {" "}does not move with the rest of this family day to day — grouped by
                    meaning, but the data disagree.
                  </p>
                )}
              </Card>
            );
          })}
          {/* The data check behind the grouping (backend family_check). Silent when it
              has nothing to say. */}
          {familyData && (familyData.candidates.length > 0
            || familyData.families.some((f) => f.members.some((m) => m.tracks === false))) && (
            <Card>
              <Eyebrow left="GROUPING CHECK" right={`${familyData.days}D`} />
              <HeroSub>
                Families are grouped by what the inputs measure, not re-clustered each week.
                The data only check them.
              </HeroSub>
              {familyData.families.map((f) => {
                const off = f.members.filter((m) => m.tracks === false);
                return off.length > 0 && (
                  <p key={f.id} className="prose-ui mt-1.5 text-[10.5px] leading-snug text-ink-2">
                    <span className="font-semibold">{f.label}:</span>{" "}
                    {off.map((m) => `${m.label} (median |ρ| ${m.median_abs_rho?.toFixed(2)})`).join(", ")}
                    {" "}does not track the rest (bar {familyData.thresholds.min_track}).
                  </p>
                );
              })}
              {familyData.candidates.length > 0 && (
                <>
                  <p className="mt-2 text-[9px] tracking-micro text-ink-3">
                    MOVE TOGETHER, NOT GROUPED (|ρ| ≥ {familyData.thresholds.candidate_rho})
                  </p>
                  {familyData.candidates.map((c) => (
                    <div key={`${c.a}|${c.b}`} className="mt-0.5 flex items-baseline gap-2">
                      <span className="prose-ui min-w-0 flex-1 truncate text-[11px] text-ink-2">
                        {c.a_label} · {c.b_label}
                      </span>
                      <span className="flex-none text-[10.5px] tabular-nums text-ink-3">
                        {c.rho > 0 ? "+" : ""}{c.rho.toFixed(2)} · n={c.n}
                      </span>
                    </div>
                  ))}
                </>
              )}
              <Foot>
                Moving together is not the same as being one lever: habits that happen on the
                same days stay separate unless they measure the same thing.
              </Foot>
            </Card>
          )}
        </>
      )}

      {/* ── Prospective holdout ────────────────────────────────────────────── */}
      {view === "holdout" && (
        <>
          <Card>
            <Eyebrow left="PROSPECTIVE VALIDATION" right={`${vres.length} REGISTERED`} />
            <HeroSub>
              Each pair was registered on the date it first crossed a tier, then re-tested
              only on data that arrived <K>after</K> that date. A finding that cannot
              reproduce itself forward was a pattern in the past, not a property of you.
              {" "}This is a <K>different set</K> from Surviving: it keeps every pair that
              ever crossed a tier, including ones that have since fallen back below it, so
              its count is not meant to match today's.
            </HeroSub>
            <Legend items={[
              { label: "replicating", color: "var(--rhr)" },
              { label: "weakening", color: "var(--breakfast)" },
              { label: "reversed", color: "var(--over)" },
              { label: "accumulating", color: "var(--ink-3)" },
            ]} />
          </Card>

          {subFloor.length > 0 && (
            <Card>
              <Eyebrow left="REGISTERED BELOW THE CURRENT BAR" right={`${subFloor.length}`} />
              <HeroSub>
                The ledger had no minimum-n gate before 2026-08-15. At <K>n=6</K> a
                correlation needs only |ρ|≥0.81 to clear an uncorrected p&lt;0.05, so
                scanning hundreds of pairs makes such a hit near-certain. These entries
                are kept, not deleted — the ledger is a pre-registration record and
                editing it after seeing results would defeat its purpose. Their holdout
                columns show what actually happened.
              </HeroSub>
              <div className="mt-2 divide-y divide-rule">
                {subFloor.map((v) => {
                  const t = VERDICT_TONE[v.verdict] ?? VERDICT_TONE.accumulating;
                  return (
                    <div key={`${v.metric_a}${v.metric_b}${v.lag_days}`} className="py-2">
                      <p className="prose-ui truncate text-[11.5px] text-ink">
                        {v.metric_a_label ?? v.metric_a} → {v.metric_b_label ?? v.metric_b}
                      </p>
                      <p className="mt-0.5 text-[10px] tabular-nums text-ink-2">
                        registered <b className="font-semibold text-ink">
                          {v.rho_at_entry > 0 ? "+" : ""}{v.rho_at_entry?.toFixed(2)}
                        </b> at n={v.n_at_entry}
                        <span className="text-ink-3"> → </span>
                        holdout <b className="font-semibold" style={{ color: t.color }}>
                          {v.rho_holdout > 0 ? "+" : ""}{v.rho_holdout?.toFixed(2)}
                        </b> at n={v.n_holdout}
                        {v.n_holdout < v.n_needed && (
                          <span className="text-ink-3"> · {v.n_needed - v.n_holdout} more days to a verdict</span>
                        )}
                      </p>
                    </div>
                  );
                })}
              </div>
            </Card>
          )}

          <Card>
            <Eyebrow left="ALL REGISTERED FINDINGS" />
            <div className="mt-2 divide-y divide-rule">
              {vres.slice().sort((a, b) => {
                const order = { reversed: 0, weakening: 1, accumulating: 2, replicating: 3 };
                return (order[a.verdict] ?? 9) - (order[b.verdict] ?? 9);
              }).map((v) => {
                const t = VERDICT_TONE[v.verdict] ?? VERDICT_TONE.accumulating;
                return (
                  <div key={`${v.metric_a}${v.metric_b}${v.lag_days}`} className="flex items-baseline gap-2 py-1.5">
                    <span className="prose-ui min-w-0 flex-1 truncate text-[11px] text-ink-2">
                      {v.metric_a_label ?? v.metric_a} → {v.metric_b_label ?? v.metric_b}
                    </span>
                    <span className="flex-none text-[9px] tabular-nums text-ink-3">
                      n={v.n_at_entry}→{v.n_holdout}
                    </span>
                    <span className="flex-none text-[9px] tracking-micro" style={{ color: t.color }}>
                      {t.label}
                    </span>
                  </div>
                );
              })}
            </div>
            <Foot>
              Ordered worst-first: reversed and weakening findings are the ones worth
              knowing about, because they are the ones you might still be acting on.
            </Foot>
          </Card>
        </>
      )}

      {/* Your typical value for every daily variable, over the same normal days these
          findings use (note 86). Moved here from Insights → Analysis 2026-11-04 (note 99). */}
      <Collapse title="YOUR TYPICAL DAY">
        <div className="px-3 pb-3"><TypicalValues embedded /></div>
      </Collapse>

      {/* Last, and deliberately so — prose that hasn't passed any of the gates the
          cards above did should not be the first thing read. */}
      <Summaries />
    </div>
  );
}
