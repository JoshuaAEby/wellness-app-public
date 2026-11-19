// Offline mode, layer 1 (2026-10-21) — the write queue and the online/offline state.
//
// The user missed logs when out with poor or no signal. With the service worker (public/sw.js)
// the app now OPENS offline on cached data; this module makes LOGGING work offline:
//
//   • A queueable write (creates only — see QUEUEABLE) that can't reach the server is
//     stored in localStorage and replayed later. Edits/deletes are not queued: replaying
//     "delete entry 123" for an entry itself created offline cannot work.
//   • The tap time is written INTO the request before it is stored, for endpoints whose
//     server would otherwise stamp "now" on arrival (caffeine, snacks/drinks without a
//     time) — so a coffee logged offline at 08:00 doesn't land at 10:30.
//   • Every queueable write carries an Idempotency-Key from its FIRST attempt; the server
//     (app/idempotency.py) applies a key at most once, so a timed-out request that did
//     land, then replays, isn't logged twice.
//   • iOS has no Background Sync, so replay runs when the app is open: on start, on the
//     `online` event, when the app becomes visible, and every 20 s while anything waits.
//
// localStorage (not IndexedDB) on purpose: the queue is tiny and synchronous access keeps
// the enqueue path impossible to lose between an await and a closed app.

import { foodToday } from "./utils.js";

const QKEY = "wellness_offline_queue_v1";
const FKEY = "wellness_offline_failed_v1";

const QUEUEABLE = [
  /^\/api\/nutrition\/logs(\/batch)?$/,
  /^\/api\/nutrition\/parse-jobs$/,
  /^\/api\/nutrition\/skip$/,
  /^\/api\/caffeine\/logs$/,
  /^\/api\/alcohol\/logs$/,
  /^\/api\/water\/logs$/,
  /^\/api\/nap\/logs$/,
  /^\/api\/habits$/,
  /^\/api\/manual\/wellbeing$/,
];

export function isQueueable(path, method) {
  return method === "POST" && QUEUEABLE.some((r) => r.test(path.split("?")[0]));
}

export function newKey() {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
  return `k${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
}

const load = (k) => { try { return JSON.parse(localStorage.getItem(k) || "[]"); } catch { return []; } };
const save = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* full/private */ } };

let state = { offline: false, staleAt: null, queued: load(QKEY).length, failed: load(FKEY).length, syncing: false };
const subs = new Set();
const set = (patch) => { state = { ...state, ...patch }; subs.forEach((f) => f(state)); };

export function subscribe(fn) { subs.add(fn); fn(state); return () => subs.delete(fn); }
export function getState() { return state; }
export function noteOnline() { if (state.offline || state.staleAt) set({ offline: false, staleAt: null }); }
export function noteOffline(staleAt = null) { set({ offline: true, staleAt: staleAt ?? state.staleAt }); }

const pad = (n) => String(n).padStart(2, "0");

/** Write the tap time into a body whose server would otherwise stamp arrival time. */
export function stampTime(path, body) {
  const b = { ...(body || {}) };
  const now = new Date();
  const hhmm = `${pad(now.getHours())}:${pad(now.getMinutes())}`;
  const p = path.split("?")[0];
  // The FOOD day (00:00–03:59 belongs to the day that just ended), matching the LOG
  // bar's default; the backend turns a pre-04:00 time on that day into its real moment.
  const today = foodToday(now);
  if (/^\/api\/nutrition\/(logs(\/batch)?|parse-jobs)$/.test(p)) {
    const field = p.endsWith("parse-jobs") ? "time" : "at_time";
    const sameDay = !b.date || b.date === today;
    // Main meals keep their default slot (what an online save does); a snack/drink
    // without a time would otherwise get the moment the replay reaches the server.
    if (!b[field] && sameDay && ["snack", "drinks"].includes(b.meal_type)) b[field] = hhmm;
    if (!b.date) b.date = today;
  } else if (p === "/api/caffeine/logs") {
    if (!b.timestamp) b.timestamp = now.toISOString().slice(0, 19);   // naive UTC, as the card sends
  } else if (/^\/api\/(alcohol|water|nap)\/logs$/.test(p)) {
    if (!b.at_time) b.at_time = hhmm;
    if (!b.date) b.date = today;
  }
  return b;
}

function describe(path, body) {
  const p = path.split("?")[0];
  if (p.includes("/batch")) return (body.items || []).map((i) => i.name || i.description).join(", ") || "food";
  if (p.endsWith("/nutrition/logs")) return body.name || body.description || "food";
  if (p.endsWith("parse-jobs")) return `"${(body.description || "").slice(0, 40)}" (to parse)`;
  if (p.endsWith("/skip")) return `skip ${body.meal_type}`;
  if (p.includes("caffeine")) return `caffeine (${body.kind || "custom"})`;
  if (p.includes("alcohol")) return `alcohol (${body.kind || body.category || "drink"})`;
  if (p.includes("water")) return `water${body.oz ? ` ${body.oz} oz` : ""}`;
  if (p.includes("nap")) return "nap";
  if (p.endsWith("/habits")) return `habits (${Object.keys(body).filter((k) => k !== "date").join(", ")})`;
  if (p.endsWith("wellbeing")) return "check-in";
  return p;
}

export function enqueue({ path, method, body, key }) {
  const q = load(QKEY);
  const stamped = stampTime(path, body);
  q.push({ key, path, method, body: stamped, label: describe(path, stamped), queued_at: new Date().toISOString() });
  save(QKEY, q);
  set({ queued: q.length, offline: true });
  return { queued: true, key, label: describe(path, stamped) };
}

/** Remove a still-queued write (Undo on something saved offline). */
export function removeQueued(key) {
  const q = load(QKEY).filter((x) => x.key !== key);
  save(QKEY, q);
  set({ queued: q.length });
}

export const queuedItems = () => load(QKEY);
export const failedItems = () => load(FKEY);
export function discardFailed(key) {
  const f = load(FKEY).filter((x) => x.key !== key);
  save(FKEY, f);
  set({ failed: f.length });
}

let running = false;
/** Replay the queue in order. `send(item)` resolves to a Response or throws (network). */
export async function replay(send, onSynced) {
  if (running) return;
  const q0 = load(QKEY);
  if (!q0.length) return;
  running = true;
  set({ syncing: true });
  let synced = 0;
  try {
    for (const item of q0) {
      let res;
      try { res = await send(item); } catch { noteOffline(); break; }   // still offline
      if (res.ok) {
        synced++;
        save(QKEY, load(QKEY).filter((x) => x.key !== item.key));
      } else if (res.status === 401 || res.status >= 500) {
        // 502/503/504 = app still down behind the proxy; any other 5xx we also keep —
        // the idempotency key makes an eventual success apply exactly once.
        break;                       // log in again / server trouble: keep it, try later
      } else {
        // 4xx: the server will never accept it as is — park it where it's visible.
        save(QKEY, load(QKEY).filter((x) => x.key !== item.key));
        const f = load(FKEY);
        f.push({ ...item, error: `${res.status} ${(await res.text()).slice(0, 160)}` });
        save(FKEY, f);
      }
    }
  } finally {
    running = false;
    set({ syncing: false, queued: load(QKEY).length, failed: load(FKEY).length });
    if (synced) { noteOnline(); onSynced?.(synced); }
  }
}

/** Start the replay loop; returns a stop function. */
export function startSync(send, onSynced) {
  const tick = () => replay(send, onSynced);
  const onVis = () => { if (document.visibilityState === "visible") tick(); };
  window.addEventListener("online", tick);
  document.addEventListener("visibilitychange", onVis);
  const iv = setInterval(() => { if (load(QKEY).length) tick(); }, 20000);
  tick();
  return () => {
    window.removeEventListener("online", tick);
    document.removeEventListener("visibilitychange", onVis);
    clearInterval(iv);
  };
}
