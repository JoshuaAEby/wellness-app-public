export function dateStr(d) {
  return d.toISOString().slice(0, 10);
}

export const WINDOW_DAYS = { "1W": 7, "1M": 30, "3M": 90, "6M": 180, "1Y": 365 };

/**
 * Date bounds for a named window, optionally shifted back by whole windows.
 *
 * `offset` = 0 is the window ending today; 1 is the equally-sized window
 * immediately before it, and so on. Paging by the window's OWN length (rather
 * than a fixed step) is what keeps successive views comparable — each page covers
 * the same span, so a 3M page always sits beside another 3M page.
 */
export function windowDates(window, offset = 0) {
  const days = WINDOW_DAYS[window] ?? 30;
  const end = new Date();
  end.setDate(end.getDate() - days * offset);
  const start = new Date(end);
  start.setDate(start.getDate() - days);
  return { start: dateStr(start), end: dateStr(end) };
}

// "2026-04-18", +1 → "2026-04-19" (calendar-safe, no UTC rollover)
export function addDays(iso, n) {
  const d = new Date(iso + "T12:00:00");
  d.setDate(d.getDate() + n);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// "2026-04-18" → "Apr 18"
export function fmtDate(str) {
  if (!str) return "";
  const d = new Date(str + "T12:00:00");
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

// "2026-04-18" → "Apr 18 '26" (for longer windows)
export function fmtDateLong(str) {
  if (!str) return "";
  const d = new Date(str + "T12:00:00");
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "2-digit" });
}

export function fmtDuration(sec) {
  if (!sec) return "—";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

export function fmtMinutes(min) {
  if (min == null) return "—";
  const h = Math.floor(min / 60);
  const m = min % 60;
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

export function clip(val, lo, hi) {
  return Math.max(lo, Math.min(hi, val));
}

// Compute the four readiness component scores from a daily_metrics row
export function readinessComponents(m) {
  if (!m) return { hrv: null, rhr: null, sleep: null, acwr: null };
  return {
    hrv:   m.hrv_z_score  != null ? clip(((m.hrv_z_score  + 2) / 4) * 100, 0, 100) : null,
    rhr:   m.rhr_z_score  != null ? clip(((-m.rhr_z_score + 2) / 4) * 100, 0, 100) : null,
    sleep: m.sleep_debt_hours != null ? clip(100 - (m.sleep_debt_hours / 2) * 100, 0, 100) : null,
    acwr:  m.acwr != null ? clip(100 - Math.max(0, m.acwr - 0.8) * 90, 10, 100) : null,
  };
}

// Simple linear regression — returns {slope, intercept} or null
export function linearRegression(points) {
  const n = points.length;
  if (n < 2) return null;
  const xs = points.map((_, i) => i);
  const ys = points.map(p => p);
  const mx = xs.reduce((a, b) => a + b, 0) / n;
  const my = ys.reduce((a, b) => a + b, 0) / n;
  const num = xs.reduce((s, x, i) => s + (x - mx) * (ys[i] - my), 0);
  const den = xs.reduce((s, x) => s + (x - mx) ** 2, 0);
  if (den === 0) return null;
  const slope = num / den;
  const intercept = my - slope * mx;
  return { slope, intercept, predict: (i) => slope * i + intercept };
}

// lbs ↔ kg
export const lbsToKg = (lbs) => lbs / 2.20462;
export const kgToLbs = (kg) => kg * 2.20462;

// The FOOD day (backend app/foodday.py, 2026-10-21): food and the LOG date bar treat
// 00:00–03:59 as part of the day that just ended. Returns YYYY-MM-DD in local time.
export const FOOD_DAY_START_HOUR = 4;
export function foodToday(now = new Date()) {
  const d = new Date(now.getTime() - FOOD_DAY_START_HOUR * 3600 * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
