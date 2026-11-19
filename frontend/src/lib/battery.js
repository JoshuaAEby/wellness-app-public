// Shared baseline maths for the daily battery.
//
// Extracted from PvtCard when it was folded into BatteryCard, because the Recovery
// results card needs the same "is this actually different from usual?" test and two
// copies would inevitably drift apart.

// Below this many prior sessions the spread of your own values can't be estimated, so a
// delta is shown WITHOUT a verdict rather than with a guessed one.
export const MIN_BASELINE_N = 8;

// Irreducible measurement floor for PVT median RT. Bootstrapped within-session SE is
// ~3.4 ms on real sessions, so a band narrower than ±2 SE would call measurement noise a
// change no matter how tight the day-to-day history looks.
const FLOOR_MS = 7;

// Robust centre and a "still your usual" band for the day-to-day spread.
//
// MAD rather than SD because a single bad night is a real observation that shouldn't be
// allowed to widen the band until nothing registers. 1.4826 rescales MAD to a
// normal-consistent SD. sqrt(1 + 1/n) accounts for the baseline having its own error,
// not just today's reading.
export function baselineStats(priors, floor = FLOOR_MS) {
  const n = priors.length;
  if (n < 3) return { n, center: null, band: null };
  const sorted = [...priors].sort((a, b) => a - b);
  const center = median(sorted);
  const mad = median([...priors].map((v) => Math.abs(v - center)).sort((a, b) => a - b));
  const sd = 1.4826 * mad;
  return { n, center, band: Math.max(floor, 2 * sd * Math.sqrt(1 + 1 / n)) };
}

// PVT comparisons run on MEAN SPEED (mean 1/RT), shown as its reciprocal in ms so the
// unit stays familiar: 1000 / mean_reciprocal_rt is the harmonic-mean RT, a monotone
// transform of mean speed (note 52 follow-up, 2026-09-23). At 90 s mean speed is the
// more reliable single-session metric — 0.66 vs 0.45 for median RT — see services/pvt.py.
export function pvtRt(s) {
  return s?.mean_reciprocal_rt ? 1000 / s.mean_reciprocal_rt : null;
}

// Measurement floor for pvtRt at 90 s: bootstrapped SE of mean speed ≈ 0.085/s at ~3.4/s,
// i.e. ≈ 7 ms in RT terms; 2 SE ≈ 15 ms. (The 7 ms FLOOR_MS below was 2 SE of the MEDIAN
// on 2-min sessions, and its 3.4 ms SE was itself optimistic — 4.7 ms measured.)
export const PVT_FLOOR_MS = 15;

export function median(sorted) {
  const n = sorted.length;
  return n % 2 ? sorted[(n - 1) / 2] : (sorted[n / 2 - 1] + sorted[n / 2]) / 2;
}
