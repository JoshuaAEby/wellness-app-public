/* Wellness service worker — offline mode, layer 1 (2026-10-21).
 *
 * The user missed logs when out with bad or no signal: every open needed the server. This
 * keeps the app usable without it:
 *   • app shell  — index.html network-first (a deploy shows up as soon as you're online),
 *                  falling back to the cached copy; /assets/* are content-hashed, so
 *                  cache-first forever.
 *   • API GETs   — network-first with a short timeout, falling back to the last good
 *                  response, marked `x-sw-cached: 1` so the app can say "offline —
 *                  showing data from 09:42" (the response keeps the server's Date header).
 *   • writes     — never touched here. The app queues them (src/offline.js) and replays
 *                  with an Idempotency-Key, because iOS has no Background Sync.
 *
 * Bump VERSION to drop every cache on the next activation.
 */
const VERSION = "v1";
const SHELL = `shell-${VERSION}`;
const ASSETS = `assets-${VERSION}`;
const API = `api-${VERSION}`;
const API_TIMEOUT_MS = 6000;
const NAV_TIMEOUT_MS = 4000;
const MAX_ASSETS = 80;
const MAX_API = 300;          // distinct API URLs kept (date-parameterised URLs add up)

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL)
      .then((c) => c.addAll(["/", "/manifest.json"]).catch(() => {}))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keep = new Set([SHELL, ASSETS, API]);
    for (const k of await caches.keys()) if (!keep.has(k)) await caches.delete(k);
    await self.clients.claim();
  })());
});

function withTimeout(promise, ms) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error("timeout")), ms);
    promise.then((v) => { clearTimeout(t); resolve(v); }, (e) => { clearTimeout(t); reject(e); });
  });
}

async function markCached(res) {
  const headers = new Headers(res.headers);
  headers.set("x-sw-cached", "1");
  return new Response(await res.blob(), { status: res.status, statusText: res.statusText, headers });
}

async function apiNetworkFirst(req) {
  const cache = await caches.open(API);
  try {
    const res = await withTimeout(fetch(req), API_TIMEOUT_MS);
    // Caddy up but the app down (a deploy restart, a crash) answers 502/504: for the
    // phone that is the same as no connection, so fall back to the cache.
    if ([502, 503, 504].includes(res.status)) throw new Error(`upstream ${res.status}`);
    if (res.ok) {
      await cache.put(req, res.clone());
      if (Math.random() < 0.05) {                 // occasional trim, oldest first
        const keys = await cache.keys();
        for (const k of keys.slice(0, Math.max(0, keys.length - MAX_API))) await cache.delete(k);
      }
    }
    return res;
  } catch {
    const hit = await cache.match(req);
    if (hit) return markCached(hit);
    return new Response(JSON.stringify({ detail: "offline", offline: true }), {
      status: 503, headers: { "content-type": "application/json", "x-sw-offline": "1" },
    });
  }
}

async function assetCacheFirst(req) {
  const cache = await caches.open(ASSETS);
  const hit = await cache.match(req);
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok) {
    await cache.put(req, res.clone());
    const keys = await cache.keys();
    for (const k of keys.slice(0, Math.max(0, keys.length - MAX_ASSETS))) await cache.delete(k);
  }
  return res;
}

async function shellNetworkFirst(req) {
  const cache = await caches.open(SHELL);
  try {
    const res = await withTimeout(fetch(req), NAV_TIMEOUT_MS);
    if (res.ok) cache.put("/", res.clone());
    return res;
  } catch {
    return (await cache.match("/")) || Response.error();
  }
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/auth")) return;
  if (url.pathname.startsWith("/api/")) { event.respondWith(apiNetworkFirst(req)); return; }
  if (url.pathname.startsWith("/assets/")) { event.respondWith(assetCacheFirst(req)); return; }
  if (req.mode === "navigate") { event.respondWith(shellNetworkFirst(req)); return; }
  // manifest, icons: cache then refresh in the background
  event.respondWith((async () => {
    const cache = await caches.open(SHELL);
    const hit = await cache.match(req);
    const net = fetch(req).then((res) => { if (res.ok) cache.put(req, res.clone()); return res; })
      .catch(() => hit || Response.error());
    return hit || net;
  })());
});
