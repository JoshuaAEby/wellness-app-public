/**
 * Behaviour taxonomy — three ORTHOGONAL taggings of the same behaviour.
 *
 *   type   → decides which statistic is honest, and therefore the display
 *   theme  → narrative grouping only (weekly summaries: "Mind & Connection is
 *            down this week"). Deliberately NOT a layout axis: a theme card is
 *            forced to render a dose, a binary and a frequency with one stat,
 *            which is how consecutive-day streaks ended up applied to
 *            everything and reading 0 across the board.
 *   evidence → which correlation input this behaviour maps to, so a row can
 *            show what it actually moves.
 *
 * WHY TYPE DECIDES THE STAT (the whole point):
 *
 *   avoidance  no alcohol, no late caffeine   STREAK    — the only place a
 *              consecutive-day run is both plausible and meaningful
 *   binary     creatine, supplements          RATE      — at 100% or 0% a
 *              streak is noise; a rolling rate still says something
 *   frequency  workouts, walks, macros        COUNT/TARGET — a streak actively
 *              lies here: 4x/week is success, not a broken run
 *   dose       caffeine mg, screen minutes    DISTRIBUTION — there is no
 *              "did it" to streak
 *   timing     caffeine cutoff, bedtime       CLOCK IQR — ditto
 */

export const TYPES = { AVOIDANCE: "avoidance", BINARY: "binary", FREQUENCY: "frequency",
                       DOSE: "dose", TIMING: "timing" };

export const THEMES = { SLEEP: "Sleep", MOVEMENT: "Movement",
                        NUTRITION: "Nutrition", MIND: "Mind" };

/**
 * `streakKey` exists because the streaks endpoint uses its OWN key namespace
 * (`caffeine`, `light`, `walk`) which does not match the habit keys
 * (`no_late_caffeine`, `outdoor_light_am`, `went_for_walk`). Reading the habit key
 * off that payload silently yields undefined, which renders as a zero streak — a
 * wrong number, not a missing one.
 *
 * `evidence` is the metric name as the correlation engine knows it, and
 * `evidenceAs` is how to SAY it. Those differ whenever the habit is framed as an
 * avoidance but the measurement is of the thing avoided ("No alcohol" vs
 * `alcohol_sdu`). Stating evidence in terms of the measured metric avoids
 * sign-flipping a rho into the habit's frame, which is an easy way to render a
 * protective effect as a harmful one.
 */
export const BEHAVIORS = {
  no_alcohol:        { label: "No alcohol",        emoji: "🍵", streakKey: "no_alcohol", type: TYPES.AVOIDANCE, theme: THEMES.NUTRITION, evidence: "alcohol_sdu",     evidenceAs: "drinking" },
  no_late_caffeine:  { label: "No late caffeine",  emoji: "☕", streakKey: "caffeine", type: TYPES.AVOIDANCE, theme: THEMES.SLEEP,     evidence: "habit_caffeine",  evidenceAs: "the caffeine cutoff" },

  supplements:       { label: "Supplements",       emoji: "💊", streakKey: "supplements", type: TYPES.BINARY,    theme: THEMES.NUTRITION, evidence: "habit_supplements" },
  creatine:          { label: "Creatine",          emoji: "🧪", streakKey: "creatine", type: TYPES.BINARY,    theme: THEMES.NUTRITION, evidence: "habit_creatine" },
  // Evidence points at mindful_min (DURATION, from Apple) rather than the binary
  // habit_meditation: minutes carry far more information than did/didn't, and the
  // binary was uncorrelatable anyway at 0/90. Auto-derived since 2026-08-19.
  meditation:        { label: "Meditation",        emoji: "🧘", streakKey: "meditation", type: TYPES.BINARY,    theme: THEMES.MIND,      evidence: "mindful_min", evidenceAs: "meditation minutes" },
  outdoor_light_am:  { label: "Outdoor light",     emoji: "☀️", streakKey: "light", type: TYPES.BINARY,    theme: THEMES.SLEEP,     evidence: "time_in_daylight", evidenceAs: "morning daylight" },
  fluids_goal:       { label: "Fluids goal",       emoji: "💧", type: TYPES.BINARY,    theme: THEMES.NUTRITION, evidence: "water_oz", evidenceAs: "water" },
  slept_well:        { label: "Slept well",        emoji: "😴", type: TYPES.BINARY,    theme: THEMES.SLEEP,     evidence: "sleep_quality_pct" },
  consistent_bedtime:  { label: "Consistent bedtime",   emoji: "🛏️", type: TYPES.BINARY, theme: THEMES.SLEEP,   evidence: "bedtime_hour" },
  consistent_waketime: { label: "Consistent wake time", emoji: "🌅", type: TYPES.BINARY, theme: THEMES.SLEEP },

  worked_out:        { label: "Worked out",        emoji: "💪", streakKey: "worked_out", type: TYPES.FREQUENCY, theme: THEMES.MOVEMENT,  evidence: "daily_load",      evidenceAs: "training load" },
  went_for_walk:     { label: "Long walk",         emoji: "🚶", streakKey: "walk", type: TYPES.FREQUENCY, theme: THEMES.MOVEMENT,  evidence: "workout_walk" },
};

/** Macro goals are frequency behaviours too — "hit the target on N of 7 days".
 *
 * NO LONGER RENDERED HERE. Protein and fiber goal attainment moved to
 * Body > Nutrition on 2026-08-22, where they sit alongside calories in the
 * weekly consistency card instead of as two loose tiles in What's Holding.
 * Kept as a definition because it still names the evidence metrics correctly. */
export const MACRO_BEHAVIORS = [
  { key: "protein", label: "Protein goal", emoji: "🥩", theme: THEMES.NUTRITION, evidence: "protein_g", color: "var(--protein, #a78bfa)" },
  { key: "fiber",   label: "Fiber goal",   emoji: "🌾", theme: THEMES.NUTRITION, evidence: "fiber_g",   color: "#a3e635" },
];

const MIN_EVIDENCE_N = 30;

/**
 * Strongest cached link for one behaviour.
 *
 * Returns null when the behaviour has no evaluable pair — which is NOT the same
 * as "no effect", and callers must not render it as one. `reason` distinguishes
 * the two cases that look identical on screen but mean opposite things:
 *   "sparse"      — too few paired days to test yet (keep logging)
 *   "no_variance" — the behaviour never varies, so it is uncorrelatable by
 *                   construction and more logging will never help
 */
export function bestEvidence(results, behavior, { rate } = {}) {
  const metric = behavior?.evidence;
  if (!metric) return null;
  const rows = (results ?? []).filter(
    (r) => (r.metric_a === metric || r.metric_b === metric)
        && !String(r.metric_a).startsWith("ctrl_")
        && !String(r.metric_b).startsWith("ctrl_")
        && r.spearman_r != null
  );
  if (!rows.length) return null;

  const usable = rows.filter((r) => (r.n ?? 0) >= MIN_EVIDENCE_N);
  if (!usable.length) {
    const noVariance = rate != null && (rate >= 98 || rate <= 2);
    return { reason: noVariance ? "no_variance" : "sparse",
             maxN: Math.max(...rows.map((r) => r.n ?? 0)) };
  }

  const graded = usable.filter((r) => r.significant || r.suggestive);
  const pool = graded.length ? graded : usable;
  const best = pool.reduce((a, b) =>
    Math.abs(b.spearman_r) > Math.abs(a.spearman_r) ? b : a);

  const otherIsSubject = best.metric_a === metric;
  return {
    subject: behavior.evidenceAs ?? (otherIsSubject ? best.metric_a_label ?? best.metric_a
                                                    : best.metric_b_label ?? best.metric_b),
    outcome: otherIsSubject ? (best.metric_b_label ?? best.metric_b)
                            : (best.metric_a_label ?? best.metric_a),
    rho: best.spearman_r,
    n: best.n,
    lag: best.lag_days,
    tier: best.significant ? "confirmed" : best.suggestive ? "suggestive" : "weak",
  };
}
