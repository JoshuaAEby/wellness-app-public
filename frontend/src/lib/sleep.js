/**
 * Sleep-session plausibility.
 *
 * WHY THIS EXISTS
 * ---------------
 * Health Auto Export reports one sleep row per DAY, with `sleepStart` = the first
 * sleep sample and `sleepEnd` = the last. If you nap in the afternoon, that nap's
 * end becomes the night's `wake_time`, producing spans like 03:23 → 19:38 (16.3 h).
 * Five such nights exist in the last 365.
 *
 * WHAT IS AND ISN'T CORRUPTED (measured, 2026-08-15)
 * -------------------------------------------------
 * `total_min` is the SUM OF STAGE DURATIONS, so it stays correct on those nights —
 * excluding them moves the 365-day mean by 0.3 minutes (427.7 → 427.4). Duration
 * averages are therefore fine and must NOT be filtered; doing so would discard real
 * sleep for no gain.
 *
 * The clock fields are what break. A wake time 8–14 h late blows out any bedtime or
 * wake-time axis, which is exactly what made the schedule charts unreadable. So the
 * guard is SCOPED: drop these nights from clock-based views only.
 *
 * Older data has a second, different problem: rows before ~2025 carry `total_min`
 * up to 1318 (22 h) with all stages folded into `core`, and some where total EXCEEDS
 * the bed→wake span, which is physically impossible. Those DO corrupt duration
 * averages, but only on windows reaching back past ~1 year.
 */

const MAX_SPAN_MIN = 16 * 60;   // a night longer than this has a nap merged in
const MAX_TOTAL_MIN = 12 * 60;  // more than 12 h of scored sleep is not a night

export function spanMinutes(s) {
  if (!s?.bedtime || !s?.wake_time) return null;
  const b = new Date(s.bedtime.endsWith("Z") ? s.bedtime : s.bedtime + "Z");
  const w = new Date(s.wake_time.endsWith("Z") ? s.wake_time : s.wake_time + "Z");
  if (isNaN(b) || isNaN(w)) return null;
  return (w - b) / 60000;
}

/**
 * Why this night's CLOCK TIMES can't be trusted, or null if they can.
 * Duration is judged separately — see `durationSuspect`.
 */
export function clockSuspect(s) {
  const span = spanMinutes(s);
  if (span == null) return "no bed/wake times";
  if (span < 0) return "wake before bedtime";
  if (span > MAX_SPAN_MIN) return "nap merged into the night";
  if (s.total_min != null && s.total_min > span + 5) return "asleep longer than time in bed";
  return null;
}

/** Why this night's DURATION can't be trusted, or null if it can. */
export function durationSuspect(s) {
  if (s?.total_min == null) return "no total";
  if (s.total_min > MAX_TOTAL_MIN) return "over 12 h scored as one night";
  const span = spanMinutes(s);
  if (span != null && s.total_min > span + 5) return "asleep longer than time in bed";
  return null;
}

/** Nights usable for bedtime/wake-time analysis. */
export function clockUsable(sessions) {
  return (sessions ?? []).filter((s) => clockSuspect(s) == null);
}

/** Nights usable for duration analysis. Deliberately far more permissive. */
export function durationUsable(sessions) {
  return (sessions ?? []).filter((s) => durationSuspect(s) == null);
}

/** Local minutes-since-midnight for a stored naive-UTC timestamp. */
export function localMinutes(iso, offsetMin) {
  if (!iso) return null;
  const d = new Date(iso.endsWith("Z") ? iso : iso + "Z");
  if (isNaN(d)) return null;
  // With the server's offset (where you WERE that night — /sleep's bed_offset_min /
  // wake_offset_min), read the wall clock there; otherwise fall back to the phone's zone.
  if (typeof offsetMin === "number") {
    return (((d.getUTCHours() * 60 + d.getUTCMinutes() + offsetMin) % 1440) + 1440) % 1440;
  }
  return d.getHours() * 60 + d.getMinutes();
}

/**
 * Minutes on an axis that starts at `originHour` (default 18:00), so an evening
 * bedtime and a morning wake sit on one continuous line and midnight isn't a
 * discontinuity in the middle of the data.
 */
export function clockToAxis(mins, originHour = 18) {
  if (mins == null) return null;
  return (mins - originHour * 60 + 24 * 60) % (24 * 60);
}

export function axisToLabel(axisMins, originHour = 18) {
  const t = Math.round((axisMins + originHour * 60) % (24 * 60));
  const h = Math.floor(t / 60), m = t % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

/** Quantiles by nearest-rank on an already-sorted-or-not numeric array. */
export function quantile(values, p) {
  const v = values.filter((x) => x != null).slice().sort((a, b) => a - b);
  if (!v.length) return null;
  return v[Math.min(v.length - 1, Math.floor(p * v.length))];
}
