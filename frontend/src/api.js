import { enqueue, isQueueable, newKey, noteOffline, noteOnline } from "./offline.js";

export function getToken() { return localStorage.getItem("wellness_token"); }
export function saveToken(t) { localStorage.setItem("wellness_token", t); }
export function clearToken() { localStorage.removeItem("wellness_token"); }

// Offline mode (2026-10-21, src/offline.js): queueable writes carry an Idempotency-Key
// and, if the server can't be reached, are queued on the phone and replayed later;
// GETs answered from the service worker's cache flag the app as offline.
const WRITE_TIMEOUT_MS = 10000;

function authHeaders(extra = {}) {
  const token = getToken();
  return {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...extra,
  };
}

async function fetchWithTimeout(path, opts, ms) {
  if (!ms) return fetch(path, opts);
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), ms);
  try { return await fetch(path, { ...opts, signal: ctl.signal }); }
  finally { clearTimeout(t); }
}

/** Replays one queued write (used by the sync loop in App). */
export function sendQueued(item) {
  return fetchWithTimeout(item.path, {
    method: item.method,
    headers: authHeaders({ "Idempotency-Key": item.key }),
    body: JSON.stringify(item.body),
  }, WRITE_TIMEOUT_MS);
}

async function apiFetch(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const queueable = isQueueable(path, method);
  const key = queueable ? newKey() : null;
  const opts = { ...options, headers: authHeaders({ ...(key ? { "Idempotency-Key": key } : {}), ...(options.headers || {}) }) };
  const enqueueThis = () => enqueue({ path, method, body: options.body ? JSON.parse(options.body) : {}, key });

  // Known-offline: queue straight away rather than waiting out a timeout.
  if (queueable && typeof navigator !== "undefined" && navigator.onLine === false) return enqueueThis();

  let res;
  try {
    res = await fetchWithTimeout(path, opts, queueable ? WRITE_TIMEOUT_MS : 0);
  } catch (e) {
    if (queueable) return enqueueThis();
    noteOffline();
    if (method !== "GET") throw new Error("You're offline — this change needs a connection.");
    throw e;
  }
  if (method === "GET") {
    if (res.headers.get("x-sw-cached")) noteOffline(res.headers.get("date"));
    else if (res.headers.get("x-sw-offline")) noteOffline();
    else noteOnline();
  } else if ([502, 503, 504].includes(res.status) && queueable) {
    return enqueueThis();            // app down behind Caddy (deploy restart): same as offline
  } else {
    noteOnline();
  }
  if (res.status === 401) { clearToken(); throw new Error("Unauthorized"); }
  if (!res.ok) { throw new Error(await res.text()); }
  // AN EMPTY BODY IS NOT AN ERROR. This used to `return res.json()` unconditionally,
  // which throws "Unexpected end of JSON input" on a 204 — and every DELETE in this app
  // returns 204 (alcohol, water and nap log deletes, plus notes).
  //
  // The failure was invisible in the worst way: the request SUCCEEDS and the server
  // deletes the row, then the client throws, so react-query's onSuccess never fires, the
  // list is never invalidated, and the entry sits on screen looking undeleted until a
  // reload. "Delete button not working" — while the data was already gone.
  //
  // Checked on body rather than only on status 204, so any empty 200 is safe too.
  if (res.status === 204) return null;
  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

// apiFetch throws the raw response body; this returns the server's sentence instead
// of its JSON (FastAPI `detail` as a string, {message}, or a validation list).
export function apiErrorText(e) {
  const raw = String(e?.message ?? e);
  try {
    const d = JSON.parse(raw).detail;
    if (typeof d === "string") return d;
    if (d?.message) return d.message;
    if (Array.isArray(d)) return d.map((x) => x.msg ?? JSON.stringify(x)).join("; ");
  } catch { /* not JSON — use as is */ }
  return raw;
}

export const api = {
  login: (username, password) =>
    apiFetch("/api/auth/token", { method: "POST", body: JSON.stringify({ username, password }) }),

  today: () => apiFetch("/api/metrics/today"),

  daily: (start, end) => apiFetch(`/api/metrics/daily?start=${start}&end=${end}`),

  getScreentime: (start, end) =>
    apiFetch(`/api/metrics/screentime?start=${start}&end=${end}`),

  getWeather: () => apiFetch("/api/metrics/weather"),

  getScreentimeApps: (start, end, category, device) =>
    apiFetch(`/api/metrics/screentime/apps?start=${start}&end=${end}` +
      (category ? `&category=${encodeURIComponent(category)}` : "") +
      (device ? `&device=${encodeURIComponent(device)}` : "")),

  workouts: (limit = 100) => apiFetch(`/api/metrics/workouts?limit=${limit}`),

  workoutDetail: (id) => apiFetch(`/api/metrics/workouts/${id}/detail`),

  sleep: (start, end) => apiFetch(`/api/metrics/sleep?start=${start}&end=${end}`),

  events: (start, end) => apiFetch(`/api/metrics/events?start=${start}&end=${end}`),

  rawTrend: (metricType, start, end, agg = "avg") =>
    apiFetch(`/api/metrics/raw-trend?metric_type=${metricType}&start=${start}&end=${end}&agg=${agg}`),

  logWeight: (weight_kg, notes) =>
    apiFetch("/api/manual/weight", { method: "POST", body: JSON.stringify({ weight_kg, notes }) }),

  getWeight: (days = 365) => apiFetch(`/api/manual/weight?days=${days}`),

  // Tape measurements — cm on the wire; the Body tab enters inches.
  logMeasurement: (body) =>
    apiFetch("/api/manual/measurements", { method: "POST", body: JSON.stringify(body) }),
  getMeasurements: (days = 365) => apiFetch(`/api/manual/measurements?days=${days}`),
  deleteMeasurement: (date) =>
    apiFetch(`/api/manual/measurements?date=${date}`, { method: "DELETE" }),
  // Body scans (InBody) — lb, one per date; each row carries the nearest home-scale reading.
  getScans: () => apiFetch("/api/manual/scans"),
  logScan: (body) => apiFetch("/api/manual/scans", { method: "POST", body: JSON.stringify(body) }),
  deleteScan: (date) => apiFetch(`/api/manual/scans?date=${date}`, { method: "DELETE" }),
  // TV sessions and who watched (note 85); watcher: null undoes an answer.
  getTvPending: () => apiFetch("/api/tv/sessions/pending"),
  answerTv: (id, watcher) => apiFetch(`/api/tv/sessions/${id}/answer`, { method: "POST", body: JSON.stringify({ watcher }) }),
  getTvRange: (start, end) => apiFetch(`/api/tv/range?start=${start}&end=${end}`),
  // Health events (vaccines, procedures, medication changes) — note 93.
  getHealthEvents: () => apiFetch("/api/health-events"),
  logHealthEvent: (body) => apiFetch("/api/health-events", { method: "POST", body: JSON.stringify(body) }),
  getHealthEventResponse: (id) => apiFetch(`/api/health-events/${id}/response`),
  getSickEpisodes: () => apiFetch("/api/health-events/sick-episodes"),
  getSickResponse: (start) => apiFetch(`/api/health-events/sick-episodes/${start}/response`),
  deleteHealthEvent: (id) => apiFetch(`/api/health-events/${id}`, { method: "DELETE" }),

  getWellbeing: (days = 365) => apiFetch(`/api/manual/wellbeing?days=${days}`),

  getSettings: () => apiFetch("/api/settings"),

  updateSettings: (data) =>
    apiFetch("/api/settings", { method: "PUT", body: JSON.stringify(data) }),

  recompute: () =>
    apiFetch("/api/settings/recompute", { method: "POST" }),

  refreshDailySummary: () =>
    apiFetch("/api/metrics/today/refresh-summary", { method: "POST" }),

  refreshWeeklySummary: () =>
    apiFetch("/api/metrics/weekly/refresh-summary", { method: "POST" }),

  getUsage: () => apiFetch("/api/metrics/usage"),
  getProgress: () => apiFetch("/api/metrics/progress"),
  // Nutrition scorecard: recent 4 weeks vs ~3 months earlier (Progress).
  // Data for `[chart: id]` markers in a weekly/monthly summary (note 66).
  getSummaryCharts: (kind, start, end, ids) =>
    apiFetch(`/api/metrics/summary-charts?kind=${kind}&start=${start}&end=${end}&ids=${ids.join(",")}`),
  getNutritionScorecard: () => apiFetch(`/api/metrics/nutrition-scorecard`),
  getAnchors: () => apiFetch("/api/metrics/anchors"),

  getActivityForDate: (date) => apiFetch(`/api/metrics/activity-for-date?date=${date}`),

  getWeeklyDigest: () => apiFetch("/api/metrics/weekly-digest"),

  // Nutrition
  // fresh = skip the instant-repeat lookup and ask the model ("Re-estimate").
  // One-tap re-log of usual foods around now (2026-10-21).
  getErrorLedger: (days = 7) => apiFetch(`/api/freshness/errors?days=${days}`),
  getCitationChecks: (days = 30) => apiFetch(`/api/freshness/citations?days=${days}`),
  getQuickLog: (limit = 8, perMeal = 0) =>
    apiFetch(`/api/nutrition/quick-log?limit=${limit}${perMeal ? `&per_meal=${perMeal}` : ""}`),
  // Most frequent caffeine / water / alcohol entry (one-tap chips, note 81).
  getUsualIntake: (days = 30) => apiFetch(`/api/intake/usual?days=${days}`),
  // Typical values per daily variable (note 86) — mean ± SD, median, IQR.
  getTypicalValues: (start, end) => apiFetch(`/api/insights/typical?start=${start}&end=${end}`),
  // Background parsing (2026-10-21): the server parses; the result waits for review.
  createParseJob: (description, meal_type, date, time = null) =>
    apiFetch("/api/nutrition/parse-jobs", { method: "POST",
      body: JSON.stringify({ description, meal_type, date, time }) }),
  getParseJobs: () => apiFetch("/api/nutrition/parse-jobs"),
  updateParseJob: (id, status) =>
    apiFetch(`/api/nutrition/parse-jobs/${id}`, { method: "PATCH", body: JSON.stringify({ status }) }),
  parseFood: (description, meal_type, fresh = false) =>
    apiFetch("/api/nutrition/parse", { method: "POST", body: JSON.stringify({ description, meal_type, fresh }) }),

  // Screen-view counting. Fire-and-forget: a failure here must never surface.
  logView: (view) =>
    apiFetch("/api/telemetry/view", { method: "POST", body: JSON.stringify({ view }) }).catch(() => null),
  getViewCounts: (days = 14) => apiFetch(`/api/telemetry/views?days=${days}`),

  // My Foods (docs/MY_FOODS.md)
  listMyFoods: (includeArchived = false) => apiFetch(`/api/my-foods?include_archived=${includeArchived}`),
  createMyFood: (body) => apiFetch("/api/my-foods", { method: "POST", body: JSON.stringify(body) }),
  // Base + toppings + combo in one all-or-nothing request (reuses existing names).
  createMyFoodCombo: (body) => apiFetch("/api/my-foods/combo", { method: "POST", body: JSON.stringify(body) }),
  // A meal logged piece by piece → one combination (logged entries or tray items).
  comboFromItems: (body) => apiFetch("/api/my-foods/combo-from-items", { method: "POST", body: JSON.stringify(body) }),
  updateMyFood: (id, body) => apiFetch(`/api/my-foods/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  archiveMyFood: (id) => apiFetch(`/api/my-foods/${id}`, { method: "DELETE" }),
  myFoodCandidates: (perGroup = 15) => apiFetch(`/api/my-foods/candidates?per_group=${perGroup}`),
  dismissMyFoodCandidate: (item_key) =>
    apiFetch("/api/my-foods/candidates/dismiss", { method: "POST", body: JSON.stringify({ item_key }) }),
  myFoodPastEntries: (id) => apiFetch(`/api/my-foods/${id}/past-entries`),
  myFoodFromLog: (logId, body = {}) =>
    apiFetch(`/api/my-foods/from-log/${logId}`, { method: "POST", body: JSON.stringify(body) }),
  applyMyFoodToPast: (id, ids = null) =>
    apiFetch(`/api/my-foods/${id}/apply-to-past`, { method: "POST", body: JSON.stringify({ ids }) }),
  undoMyFoodBackfill: (backfillId) =>
    apiFetch(`/api/my-foods/backfills/${backfillId}/undo`, { method: "POST" }),

  // My Recipes (note 37) — household-app recipes, read-only, with per-serving nutrition
  // estimated once and stored on the wellness side.
  listRecipes: (includeHidden = false) => apiFetch(`/api/recipes?include_hidden=${includeHidden}`),
  estimateRecipe: (id, servings = null) =>
    apiFetch(`/api/recipes/${id}/estimate${servings ? `?servings=${servings}` : ""}`, { method: "POST" }),
  updateRecipe: (id, body) => apiFetch(`/api/recipes/${id}`, { method: "PUT", body: JSON.stringify(body) }),

  getNutritionLogs: (date) => apiFetch(`/api/nutrition/logs?date=${date}`),

  createNutritionLog: (data) =>
    apiFetch("/api/nutrition/logs", { method: "POST", body: JSON.stringify(data) }),

  // One meal's foods as separate rows from one parse. Date/time/meal_type sit on
  // the envelope, not the items — they describe the sitting, not the food.
  createNutritionLogBatch: (data) =>
    apiFetch("/api/nutrition/logs/batch", { method: "POST", body: JSON.stringify(data) }),

  updateNutritionLog: (id, data) =>
    apiFetch(`/api/nutrition/logs/${id}`, { method: "PUT", body: JSON.stringify(data) }),

  deleteNutritionLog: (id) =>
    apiFetch(`/api/nutrition/logs/${id}`, { method: "DELETE" }),

  skipMeal: (date, meal_type) =>
    apiFetch("/api/nutrition/skip", { method: "POST", body: JSON.stringify({ date, meal_type }) }),

  getIncompleteDays: (days = 14) =>
    apiFetch(`/api/nutrition/incomplete?days=${days}`),
  skipDay: (date) =>
    apiFetch("/api/nutrition/skip-day", { method: "POST", body: JSON.stringify({ date }) }),
  unskipDay: (date) =>
    apiFetch(`/api/nutrition/skip-day?date=${date}`, { method: "DELETE" }),

  // Caffeine instance logging
  getCaffeinePresets: () => apiFetch("/api/caffeine/presets"),
  getCaffeineDay: (date) => apiFetch(`/api/caffeine/day?date=${date}`),
  // ── Alcohol & water (instances into health_metrics — see services/intake) ──
  // Categories + the physical constants, so the custom-entry form can preview SDU and
  // calories as you type without a request per keystroke and without a second copy of
  // the carb densities.
  getAlcoholCategories: () => apiFetch("/api/alcohol/categories"),
  getAlcoholPresets: () => apiFetch("/api/alcohol/presets"),
  saveAlcoholPresets: (presets) =>
    apiFetch("/api/alcohol/presets", { method: "PUT", body: JSON.stringify({ presets }) }),
  getAlcoholDay: (date) => apiFetch(`/api/alcohol/day?date=${date}`),
  logAlcohol: (body) =>
    apiFetch("/api/alcohol/logs", { method: "POST", body: JSON.stringify(body) }),
  patchAlcohol: (id, body) =>
    apiFetch(`/api/alcohol/logs/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteAlcohol: (id) => apiFetch(`/api/alcohol/logs/${id}`, { method: "DELETE" }),

  getWaterDay: (date) => apiFetch(`/api/water/day?date=${date}`),
  logWater: (body) =>
    apiFetch("/api/water/logs", { method: "POST", body: JSON.stringify(body) }),
  patchWater: (id, body) =>
    apiFetch(`/api/water/logs/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteWater: (id) => apiFetch(`/api/water/logs/${id}`, { method: "DELETE" }),
  getWaterPresets: () => apiFetch("/api/water/presets"),

  getHRRecovery: (days = 365) => apiFetch(`/api/metrics/hr-recovery?days=${days}`),

  getAudioExposure: (weeks = 52) =>
    apiFetch(`/api/metrics/audio-exposure?weeks=${weeks}`),

  getDaylightTiming: (start, end) =>
    apiFetch(`/api/metrics/daylight-timing?start=${start}&end=${end}`),

  // Quick-capture notes (feature ideas / bugs)
  getNotes: (includeDone = false) => apiFetch(`/api/notes?include_done=${includeDone}`),
  addNote: (body) => apiFetch("/api/notes", { method: "POST", body: JSON.stringify({ body }) }),
  patchNote: (id, data) =>
    apiFetch(`/api/notes/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  deleteNote: (id) => apiFetch(`/api/notes/${id}`, { method: "DELETE" }),

  getNapDay: (date) => apiFetch(`/api/nap/day?date=${date}`),
  logNap: (body) => apiFetch("/api/nap/logs", { method: "POST", body: JSON.stringify(body) }),
  patchNap: (id, body) =>
    apiFetch(`/api/nap/logs/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteNap: (id) => apiFetch(`/api/nap/logs/${id}`, { method: "DELETE" }),
  saveWaterPresets: (presets) =>
    apiFetch("/api/water/presets", { method: "PUT", body: JSON.stringify({ presets }) }),

  getCaffeineRange: (start, end) =>
    apiFetch(`/api/caffeine/range?start=${start}&end=${end}`),
  getFluidsRange: (start, end) =>
    apiFetch(`/api/water/fluids-range?start=${start}&end=${end}`),
  logCaffeine: (body) =>
    apiFetch("/api/caffeine/logs", { method: "POST", body: JSON.stringify(body) }),
  updateCaffeine: (id, body) =>
    apiFetch(`/api/caffeine/logs/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteCaffeine: (id) =>
    apiFetch(`/api/caffeine/logs/${id}`, { method: "DELETE" }),
  ackCaffeine: (date, ack) =>
    apiFetch("/api/caffeine/ack", { method: "POST", body: JSON.stringify({ date, ack }) }),
  clearCaffeineAck: (date) =>
    apiFetch(`/api/caffeine/ack?date=${date}`, { method: "DELETE" }),
  getCaffeineIncomplete: (days = 14) =>
    apiFetch(`/api/caffeine/incomplete?days=${days}`),
  saveCaffeinePresets: (presets) =>
    apiFetch("/api/caffeine/presets", { method: "PUT", body: JSON.stringify({ presets }) }),

  // Day flags (nonstandard-day exclusion)
  getFreshness: () => apiFetch("/api/freshness"),

  // Last observed AI outcome. Does NOT make a probe call, so polling it is free.
  getAiStatus: () => apiFetch("/api/settings/ai-status"),
  ackFreshness: (metric, note = null) =>
    apiFetch(`/api/freshness/${encodeURIComponent(metric)}/ack`,
             { method: "POST", body: JSON.stringify({ note }) }),
  unackFreshness: (metric) =>
    apiFetch(`/api/freshness/${encodeURIComponent(metric)}/ack`, { method: "DELETE" }),

  // `includeLegacySick` adds days marked sick on the morning check-in that never got a
  // DayFlag row — what the correlation engine and the AI summaries have always seen.
  // Read-only consumers (chart bands) opt in; DayFlagControl must NOT, because those
  // synthetic entries have no id and cannot be deleted.
  getDayFlags: (start, end, { includeLegacySick = false } = {}) =>
    apiFetch(`/api/day-flags?start=${start}&end=${end}`
             + (includeLegacySick ? "&include_legacy_sick=true" : "")),
  setDayFlag: (date, reason, note = null) =>
    apiFetch("/api/day-flags", { method: "POST", body: JSON.stringify({ date, reason, note }) }),
  clearDayFlag: (date, reason) =>
    apiFetch(`/api/day-flags?date=${date}&reason=${reason}`, { method: "DELETE" }),
  // "Where are you?" time-zone prompt (services/tz.prompt_state).
  getTzPrompt: () => apiFetch("/api/tz/prompt"),
  confirmTz: (tzName) =>
    apiFetch("/api/tz/confirm", { method: "POST", body: JSON.stringify({ tz_name: tzName }) }),
  // Trips = runs of travel flags (note 72).
  getTrips: () => apiFetch("/api/day-flags/trips"),
  addTrip: (start, end, note = null, tzName = null) =>
    apiFetch("/api/day-flags/trips", { method: "POST",
      body: JSON.stringify({ start, end, note, tz_name: tzName || null }) }),
  removeTrip: (start, end) =>
    apiFetch(`/api/day-flags/trips?start=${start}&end=${end}`, { method: "DELETE" }),
  // What wasn't logged on a trip (note 102).
  setTripUnlogged: (start, end, items) =>
    apiFetch("/api/day-flags/trips/unlogged", { method: "PUT", body: JSON.stringify({ start, end, items }) }),
  getTripGaps: (start, end) => apiFetch(`/api/day-flags/trip-gaps?start=${start}&end=${end}`),

  getAcwrForecast: (horizon = 7) =>
    apiFetch(`/api/metrics/acwr-forecast?horizon=${horizon}`),
  getNutritionDaily: (start, end) =>
    apiFetch(`/api/nutrition/daily?start=${start}&end=${end}`),

  getNutritionBreakdown: (start, end) =>
    apiFetch(`/api/nutrition/daily-breakdown?start=${start}&end=${end}`),

  // Nutrition-label photo -> per-serving macros. Multipart, so it bypasses
  // apiFetch (which always sets application/json); the browser must set its own
  // boundary. Same reason as parseBloodPdf.
  parseNutritionLabel: async (blob) => {
    const fd = new FormData();
    fd.append("file", blob, "label.jpg");
    const res = await fetch("/api/nutrition/parse-label", {
      method: "POST",
      headers: { Authorization: `Bearer ${getToken()}` },
      body: fd,
    });
    if (!res.ok) {
      let msg = `Label parse failed (${res.status})`;
      try { msg = (await res.json()).detail || msg; } catch { /* keep default */ }
      throw new Error(msg);
    }
    return res.json();
  },
  // Meal picker. `q` searches all meal types (you might want a snack item at
  // dinner); without a query it scopes to the meal you're logging.
  // Rule-based suggestions for the top of Today (services/nudges).
  getNudges: () => apiFetch(`/api/nutrition/nudges`),
  getRecentMeals: ({ limit = 40, q = null, meal_type = null, sort = null } = {}) => {
    const p = new URLSearchParams({ limit: String(limit) });
    if (q) p.set("q", q);
    if (meal_type) p.set("meal_type", meal_type);
    if (sort) p.set("sort", sort);   // score (default) | recent | frequent
    return apiFetch(`/api/nutrition/recent?${p}`);
  },
  // The most recent earlier food day's entries for one meal — "same as last lunch" (#22).
  getLastMeal: (meal_type, before) =>
    apiFetch(`/api/nutrition/last-meal?meal_type=${encodeURIComponent(meal_type)}${before ? `&before=${before}` : ""}`),
  // Brands already used, most-used first — suggestions for the brand field (note 60).
  getBrands: () => apiFetch(`/api/nutrition/brands`),
  toggleSaveMeal: (id) => apiFetch(`/api/nutrition/logs/${id}/save`, { method: "POST" }),

  getEnergy: (start, end) =>
    apiFetch(`/api/metrics/energy?start=${start}&end=${end}`),
  getEnergyCalibration: (days = 120) =>
    apiFetch(`/api/metrics/energy/calibration?days=${days}`),

  refreshMonthlySummary: () =>
    apiFetch("/api/metrics/monthly/refresh-summary", { method: "POST" }),

  getCorrelations: (days = 90, useCache = true) =>
    apiFetch(`/api/insights/correlations?days=${days}&use_cache=${useCache}`),

  refreshCorrelations: (days = 90) =>
    apiFetch(`/api/insights/correlations/refresh?days=${days}`, { method: "POST" }),

  getActionableInsights: (days = 90, useCache = true) =>
    apiFetch(`/api/insights/actionable?days=${days}&use_cache=${useCache}`),

  getInputFamilies: () => apiFetch("/api/insights/families"),

  getThresholdBands: (days = 180) =>
    apiFetch(`/api/insights/threshold-bands?days=${days}`),

  getStratifiedInsights: (stratum, days = 180) =>
    apiFetch(`/api/insights/stratified?stratum=${stratum}&days=${days}`),

  getLimitations: (weeks = 12) =>
    apiFetch(`/api/insights/limitations?weeks=${weeks}`),

  getTrends: (days = 90) =>
    apiFetch(`/api/insights/trends?days=${days}`),

  getCorroboration: (days = 180) =>
    apiFetch(`/api/insights/corroboration?days=${days}`),

  getValidation: () => apiFetch(`/api/insights/validation`),
  getLevers: () => apiFetch(`/api/insights/levers`),
  getMetricCoverage: () => apiFetch(`/api/insights/metric-coverage`),

  // Blood markers (lab panels)
  parseBloodReport: (text) =>
    apiFetch(`/api/blood/parse`, { method: "POST", body: JSON.stringify({ text }) }),
  // Multipart: the browser must set its own Content-Type (with the boundary), so this
  // bypasses apiFetch, which always sets application/json.
  parseBloodPdf: async (file) => {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch("/api/blood/parse-pdf", {
      method: "POST",
      headers: { Authorization: `Bearer ${getToken()}` },
      body: fd,
    });
    if (!res.ok) {
      let msg = "Upload failed";
      try { msg = (await res.json()).detail || msg; } catch {}
      throw new Error(msg);
    }
    return res.json();
  },
  // Screenshots of ONE report, in order — repeated "files" parts; the server keeps order.
  parseBloodImages: async (images) => {
    const fd = new FormData();
    images.forEach(({ blob, name }, k) => fd.append("files", blob, name || `screenshot-${k + 1}.jpg`));
    const res = await fetch("/api/blood/parse-images", {
      method: "POST",
      headers: { Authorization: `Bearer ${getToken()}` },
      body: fd,
    });
    if (!res.ok) {
      let msg = "Upload failed";
      try { msg = (await res.json()).detail || msg; } catch {}
      throw new Error(msg);
    }
    return res.json();
  },
  getBloodMarkers: () => apiFetch(`/api/blood`),
  // Class board (note 91): a photo of the board, or the board typed out → targets.
  getGymToday: () => apiFetch("/api/class-board/gym-today"),
  parseClassBoard: async ({ blob, text }) => {
    const fd = new FormData();
    if (blob) fd.append("file", blob, "board.jpg");
    else fd.append("text", text);
    const res = await fetch("/api/class-board/parse", {
      method: "POST",
      headers: { Authorization: `Bearer ${getToken()}` },
      body: fd,
    });
    if (!res.ok) {
      let msg = "Couldn't read the board";
      try { msg = (await res.json()).detail || msg; } catch {}
      throw new Error(msg);
    }
    return res.json();
  },
  classBoardTarget: (body) =>
    apiFetch("/api/class-board/target", { method: "POST", body: JSON.stringify(body) }),
  getClassBoardExercises: () => apiFetch("/api/class-board/exercises"),
  getClassBoardLast: () => apiFetch("/api/class-board/last"),
  getClassBoardToday: () => apiFetch("/api/class-board/today"),
  setClassBoardEasy: (id, easy) =>
    apiFetch(`/api/class-board/${id}/easy`, { method: "POST", body: JSON.stringify({ easy }) }),
  saveBloodPanel: (date, markers) =>
    apiFetch(`/api/blood`, { method: "POST", body: JSON.stringify({ date, markers }) }),
  deleteBloodMarker: (id) =>
    apiFetch(`/api/blood/${id}`, { method: "DELETE" }),
  // Personal targets — distinct from the lab's reference range. Null bounds are a
  // valid state ("no target here"); DELETE falls back to the seeded default.
  setBloodTarget: (marker, target_low, target_high) =>
    apiFetch(`/api/blood/targets/${marker}`,
      { method: "PUT", body: JSON.stringify({ target_low, target_high }) }),
  resetBloodTarget: (marker) =>
    apiFetch(`/api/blood/targets/${marker}`, { method: "DELETE" }),

  // Randomized coach experiments
  getExperiments: () => apiFetch(`/api/experiments`),
  getExperimentAnalysis: (id) => apiFetch(`/api/experiments/${id}/analysis`),
  getExperimentAccrual: () => apiFetch(`/api/experiments/active/accrual`),
  getExperimentToday: (id) => apiFetch(`/api/experiments/${id}/today`),
  getAssignments: (id, start, end) =>
    apiFetch(`/api/experiments/${id}/assignments?start=${start}&end=${end}`),
  logDose: (id, body) =>
    apiFetch(`/api/experiments/${id}/dose`, { method: "POST", body: JSON.stringify(body) }),
  scheduleAhead: (id, days = 7) =>
    apiFetch(`/api/experiments/${id}/schedule?days=${days}`, { method: "POST" }),
  // Draw the next N days' arms now — for interventions prepared in advance (a week's
  // pill organiser). Idempotent; already-drawn days come back unchanged.
  scheduleExperiment: (id, days = 7) =>
    apiFetch(`/api/experiments/${id}/schedule?days=${days}`, { method: "POST" }),
  createExperiment: (data) =>
    apiFetch(`/api/experiments`, { method: "POST", body: JSON.stringify(data) }),
  endExperiment: (id) =>
    apiFetch(`/api/experiments/${id}/end`, { method: "POST" }),

  getNutritionConsistency: (weeks = 12) =>
    apiFetch(`/api/metrics/nutrition-consistency?weeks=${weeks}`),

  listInterventions: () => apiFetch(`/api/interventions`),
  // category (0066): supplement | training | diet | sleep | other. Omitted on update =
  // keep the stored one.
  createIntervention: (date, label, notes, category = null) =>
    apiFetch(`/api/interventions`, { method: "POST", body: JSON.stringify({ date, label, notes, category }) }),
  updateIntervention: (id, date, label, notes, category) =>
    apiFetch(`/api/interventions/${id}`, { method: "PUT", body: JSON.stringify(
      category === undefined ? { date, label, notes } : { date, label, notes, category }) }),
  deleteIntervention: (id) =>
    apiFetch(`/api/interventions/${id}`, { method: "DELETE" }),
  interventionAnalysis: (id, washout = 7) =>
    apiFetch(`/api/interventions/${id}/analysis?washout=${washout}`),

  getDayOfWeek: (outcome = "readiness_score") =>
    apiFetch(`/api/metrics/day-of-week?outcome=${outcome}`),

  getWorkoutTypeContrast: (outcome = "subj_energy") =>
    apiFetch(`/api/metrics/workout-type-contrast?outcome=${outcome}`),

  getTrainingZones: (days = 30) =>
    apiFetch(`/api/metrics/training-zones?days=${days}`),

  getMonthlyHistory: (months = 12) =>
    apiFetch(`/api/metrics/monthly-history?months=${months}`),

  getStreaks: () => apiFetch("/api/streaks"),
  getStreakRecords: () => apiFetch("/api/streaks/records"),

  logWellbeing: (data) =>
    apiFetch("/api/manual/wellbeing", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    }),

  setWorkoutRpe: (workoutId, rpe) =>
    apiFetch(`/api/metrics/workouts/${workoutId}/rpe`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rpe }),
    }),

  getHabits: (date) =>
    apiFetch(`/api/habits?date=${date}`),

  logHabits: (data) =>
    apiFetch("/api/habits", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    }),

  // Supplement regimen (what's taken, and since when) — distinct from the daily
  // "took my supplements" habit toggle.
  getSupplements: () => apiFetch(`/api/supplements`),
  getStackOn: (day) => apiFetch(`/api/supplements/on/${day}`),
  addSupplement: (body) =>
    apiFetch(`/api/supplements`, { method: "POST", body: JSON.stringify(body) }),
  stopSupplement: (id, body = {}) =>
    apiFetch(`/api/supplements/${id}/stop`, { method: "POST", body: JSON.stringify(body) }),

  getHabitHistory: (days = 30) =>
    apiFetch(`/api/habits/history?days=${days}`),

  getHabitsCalendar: (start, end) =>
    apiFetch(`/api/habits/calendar?start=${start}&end=${end}`),

  getHabitsAdherence: (days = 30) =>
    apiFetch(`/api/habits/adherence?days=${days}`),

  // Gamification (docs/GAMIFICATION.md) — read-only mirror over existing data
  game: () => apiFetch("/api/game"),
  gameHistory: (stat, days = 365) =>
    apiFetch(`/api/game/history/${stat}?days=${days}`),

  // Psychomotor Vigilance Task — client posts RAW trials only; all scoring is
  // server-side (backend/app/services/pvt.py).
  submitPvt: (body) =>
    apiFetch("/api/pvt", { method: "POST", body: JSON.stringify(body) }),
  getPvtHistory: (days = 90) => apiFetch(`/api/pvt?days=${days}`),
  getPvtToday: () => apiFetch("/api/pvt/today"),

  // Daily battery — KSS + tapping (PVT has its own endpoints above)
  submitKss: (body) =>
    apiFetch("/api/battery/kss", { method: "POST", body: JSON.stringify(body) }),
  getKssHistory: (days = 90) => apiFetch(`/api/battery/kss?days=${days}`),
  getKssToday: () => apiFetch("/api/battery/kss/today"),
  submitTapping: (body) =>
    apiFetch("/api/battery/tapping", { method: "POST", body: JSON.stringify(body) }),
  getTappingHistory: (days = 90) => apiFetch(`/api/battery/tapping?days=${days}`),
  getTappingToday: () => apiFetch("/api/battery/tapping/today"),

  // Hevy strength integration
  hevyStatus: () => apiFetch("/api/hevy/status"),
  hevyBackfill: () => apiFetch("/api/hevy/backfill", { method: "POST" }),
  hevySync: () => apiFetch("/api/hevy/sync", { method: "POST" }),
  hevyStrengthDaily: (start, end) =>
    apiFetch(`/api/hevy/strength/daily?start=${start}&end=${end}`),
  hevyMuscleGroups: (weeks = 8, building = false) =>
    apiFetch(`/api/hevy/strength/muscle-groups?weeks=${weeks}&building=${building}`),
  hevyE1rm: (days = 365, top = 5) =>
    apiFetch(`/api/hevy/strength/e1rm?days=${days}&top=${top}`),
  hevyForWorkout: (workoutId) => apiFetch(`/api/hevy/for-workout/${workoutId}`),
  hevyRepBests: (days = 90, top = 8) =>
    apiFetch(`/api/hevy/strength/rep-bests?days=${days}&top=${top}`),

  // Food composition + the coverage tripwire. Tags live on the food ITEM, so a
  // reused saved food carries its own composition — see services/food_tags.py.
  getFoodComposition: (days = 90) =>
    apiFetch(`/api/nutrition/composition?days=${days}`),
  getFoodCompositionTrend: (weeks = 16) =>
    apiFetch(`/api/nutrition/composition/trend?weeks=${weeks}`),
  getFoodCoverage: () => apiFetch("/api/nutrition/coverage"),
  getFoodTagStatus: () => apiFetch("/api/nutrition/tags/status"),

  getHabitGoals: () => apiFetch("/api/habits/goals"),
  getConsistencyTargets: () => apiFetch("/api/habits/consistency-targets"),
  setHabitGoal: (habitKey, target, period) =>
    apiFetch(`/api/habits/goals/${habitKey}`, {
      method: "PUT",
      body: JSON.stringify({ target, period }),
    }),
  deleteHabitGoal: (habitKey) =>
    apiFetch(`/api/habits/goals/${habitKey}`, { method: "DELETE" }),
};
