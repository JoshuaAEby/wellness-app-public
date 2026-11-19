// Does the PRODUCTION BUILD actually start? (2026-10-21)
//
// Born from a ~5 h blank-screen outage: a manualChunks split made one chunk read React's
// internals before React's chunk had evaluated. `vite dev` doesn't bundle and smoke.py
// only checks the API, so nothing caught it. This loads backend/static's real entry
// module into happy-dom and renders it, failing on any uncaught error or empty root.
//
//   node scripts/bootcheck.mjs            # logged out → expects the sign-in screen
//   node scripts/bootcheck.mjs --authed   # token set, every /api answered with [] →
//                                         #   expects the dashboard shell to render
//
// One scenario per process: ES modules evaluate once per process, so the logged-out and
// logged-in runs can't share one. scripts/deploy.sh runs both before syncing.
import { GlobalRegistrator } from "@happy-dom/global-registrator";
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const AUTHED = process.argv.includes("--authed");
const STATIC = join(dirname(fileURLToPath(import.meta.url)), "../../backend/static");

const html = readFileSync(join(STATIC, "index.html"), "utf8");
const entry = html.match(/<script[^>]+type="module"[^>]+src="\/assets\/([^"]+\.js)"/)?.[1];
if (!entry || !readdirSync(join(STATIC, "assets")).includes(entry)) {
  console.error(`bootcheck: entry script not found in index.html / assets (${entry})`);
  process.exit(1);
}

GlobalRegistrator.register({ url: "http://localhost:8093/", width: 390, height: 844 });
document.body.innerHTML = '<div id="root"></div>';

const errors = [];
window.addEventListener("error", (e) => errors.push(`error: ${e.message ?? e.error}`));
window.addEventListener("unhandledrejection", (e) => errors.push(`unhandled: ${e.reason}`));
process.on("uncaughtException", (e) => errors.push(`uncaught: ${e?.stack ?? e}`));
process.on("unhandledRejection", (e) => errors.push(`unhandled: ${e?.stack ?? e}`));
const realConsoleError = console.error;
console.error = (...a) => { errors.push(`console.error: ${a.map(String).join(" ").slice(0, 300)}`); };

// No network: every API call gets an empty 200; the shell must render on no data.
const empty = () => new Response("[]", { status: 200, headers: { "content-type": "application/json" } });
globalThis.fetch = window.fetch = async () => empty();
try { localStorage.clear(); if (AUTHED) localStorage.setItem("wellness_token", "bootcheck"); } catch { /* */ }

try {
  await import(pathToFileURL(join(STATIC, "assets", entry)).href);
} catch (e) {
  errors.push(`import: ${e?.stack ?? e}`);
}
window.dispatchEvent(new Event("load"));
await new Promise((r) => setTimeout(r, 2500));

const root = document.getElementById("root");
const text = (root?.textContent ?? "").replace(/\s+/g, " ").trim();
console.error = realConsoleError;
const want = AUTHED ? /STATE/ : /Sign in/;
const ok = errors.length === 0 && want.test(text);
console.log(`bootcheck (${AUTHED ? "logged in" : "logged out"}): ${ok ? "OK" : "FAIL"} — ${text.length} chars rendered${ok ? "" : `: "${text.slice(0, 120)}"`}`);
for (const e of errors.slice(0, 8)) console.log("  " + e.split("\n").slice(0, 4).join("\n    "));
await GlobalRegistrator.unregister().catch(() => {});
process.exit(ok ? 0 : 1);
