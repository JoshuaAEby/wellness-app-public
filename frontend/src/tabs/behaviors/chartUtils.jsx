import { useState, useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { ResponsiveContainer, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend } from "recharts";
import { api } from "../../api.js";
import { fmtDate } from "../../utils.js";
import { ST_CATS, ST_COLOR, ST_LABEL, fmtMin } from "../../screentime.js";

/**
 * Chart plumbing lifted verbatim out of the old Habits tab (2026-08-17) so the
 * Exposures rebuild could re-skin the presentation WITHOUT reimplementing the
 * aggregation, the horizontal-scroll behaviour, or the screen-time app drill-down.
 * Rewriting these from scratch would have risked quietly dropping behaviour that
 * already worked; the logic here is unchanged.
 */

export function aggregateByPeriod(dailyData, period) {
  if (!dailyData?.length) return [];
  const bins = new Map();
  for (const d of dailyData) {
    const dt = new Date(d.date + "T12:00:00"); // noon avoids DST edge-cases
    let key, label;
    if (period === "day") {
      key = d.date;
      label = dt.toLocaleDateString("en-US", { month: "short", day: "numeric" });
    } else if (period === "week") {
      const dayOfWeek = (dt.getDay() + 6) % 7; // 0 = Mon
      const mon = new Date(dt);
      mon.setDate(dt.getDate() - dayOfWeek);
      key = mon.toISOString().slice(0, 10);
      label = mon.toLocaleDateString("en-US", { month: "short", day: "numeric" });
    } else if (period === "month") {
      key = d.date.slice(0, 7);
      label = dt.toLocaleDateString("en-US", { month: "short", year: "2-digit" });
    } else {
      key = d.date.slice(0, 4);
      label = key;
    }
    if (!bins.has(key)) bins.set(key, { key, label, value: 0 });
    bins.get(key).value += d.value || 0;
  }
  return [...bins.values()].sort((a, b) => a.key.localeCompare(b.key));
}

/**
 * Where the year changes, for drawing boundaries on a categorical axis.
 *
 * Returns [{ key, year }] for each bucket that starts a new year — never the first
 * bucket, which needs no separator. Reads `key` (year-aware: "2026-08-10",
 * "2026-08", "2026") and NOT `label`, which omits the year at day and week period
 * and is therefore ambiguous across a multi-year span. That ambiguity was a real
 * bug, not a cosmetic one: joining two series on label stamped 2026's values onto
 * the same week in every prior year.
 *
 * Charts using these must set XAxis dataKey="key" (with a tickFormatter for the
 * label), so a ReferenceLine can address one bucket unambiguously.
 */
export function yearMarkers(rows) {
  const out = [];
  let prev = null;
  for (const r of rows ?? []) {
    const y = String(r.key ?? "").slice(0, 4);
    if (!y) continue;
    if (prev !== null && y !== prev) out.push({ key: r.key, year: y });
    prev = y;
  }
  return out;
}

// key -> label lookup, so an axis keyed on `key` can still show the short label.
export function labelFor(rows) {
  const m = Object.fromEntries((rows ?? []).map((r) => [r.key, r.label]));
  return (k) => m[k] ?? k;
}

// Aggregate per-day screen-time category rows into week/month/year buckets,
// summing each category. Mirrors aggregateByPeriod's bucket keys.
export function aggregateScreentimeByPeriod(dailyRows, period) {
  if (!dailyRows?.length) return [];
  const bins = new Map();
  for (const d of dailyRows) {
    const dt = new Date(d.date + "T12:00:00");
    let key, label;
    if (period === "day") {
      key = d.date;
      label = dt.toLocaleDateString("en-US", { month: "short", day: "numeric" });
    } else if (period === "week") {
      const dow = (dt.getDay() + 6) % 7;
      const mon = new Date(dt);
      mon.setDate(dt.getDate() - dow);
      key = mon.toISOString().slice(0, 10);
      label = mon.toLocaleDateString("en-US", { month: "short", day: "numeric" });
    } else if (period === "month") {
      key = d.date.slice(0, 7);
      label = dt.toLocaleDateString("en-US", { month: "short", year: "2-digit" });
    } else {
      key = d.date.slice(0, 4);
      label = key;
    }
    if (!bins.has(key)) {
      const b = { key, label };
      for (const c of ST_CATS) b[c] = 0;
      bins.set(key, b);
    }
    const b = bins.get(key);
    for (const c of ST_CATS) b[c] += d[c] || 0;
  }
  // `total` MUST be set here (fixed 2026-09-23). Exposures trims the series with
  // `trimEmpty(rows, r => r.total ?? 0)`; without this field every row read as zero, so
  // trimEmpty dropped ALL of them, `screenRows` came back empty and the whole SCREEN TIME
  // section hid itself behind `screenRows.length > 0` — no matter how much data existed.
  // The PER DAY, PEAK and DAYS WITH tiles read the same field.
  return [...bins.values()]
    .map((b) => ({ ...b, total: ST_CATS.reduce((sum, c) => sum + (b[c] || 0), 0) }))
    .sort((a, b) => a.key.localeCompare(b.key));
}

// Drop leading/trailing all-zero buckets so a chart spans only real data (no
// blank padding from fillDateRange reaching the window edges). Interior zeros
// (e.g. a dry week between drinking weeks) are kept — they're meaningful.
export function trimEmpty(rows, valueOf) {
  let lo = 0, hi = rows.length - 1;
  while (lo <= hi && valueOf(rows[lo]) === 0) lo++;
  while (hi >= lo && valueOf(rows[hi]) === 0) hi--;
  return lo > hi ? [] : rows.slice(lo, hi + 1);
}

// Friendly app name from a bundle id (best-effort; falls back to last segment).
const _APP_NAMES = {
  "com.atebits.tweetie2": "X (Twitter)", "com.burbn.instagram": "Instagram",
  "com.google.ios.youtube": "YouTube", "com.apple.mobilesafari": "Safari",
  "com.apple.mobilesms": "Messages", "com.facebook.messenger": "Messenger",
  "com.facebook.facebook": "Facebook", "co.happybits.marcopolo": "Marco Polo",
  "com.substack.substack": "Substack", "org.mozilla.ios.firefox": "Firefox",
  "com.apple.webapp": "Web apps (PWAs)", "com.apple.maps": "Apple Maps",
  "com.google.maps": "Google Maps", "com.anthropic.claude": "Claude",
  "com.anthropic.claudefordesktop": "Claude", "com.brave.browser": "Brave",
  "com.google.gemini": "Gemini", "com.apple.camera": "Camera",
  "com.apple.mobileslideshow": "Photos", "com.linkedin.linkedin": "LinkedIn",
  "com.microsoft.skype.teams": "Teams", "com.spotify.client": "Spotify",
  "com.apple.podcasts": "Podcasts", "com.reddit.reddit": "Reddit",
  "com.apple.incallservice": "Phone", "com.apple.sleeplockscreen": "Lock screen",
};
export function prettifyApp(bundle) {
  const b = (bundle || "").toLowerCase();
  if (_APP_NAMES[b]) return _APP_NAMES[b];
  const seg = (bundle || "").split(".").filter(Boolean).pop() || bundle || "?";
  return seg.replace(/([a-z0-9])([A-Z])/g, "$1 $2");
}

// A horizontally-scrollable bar chart: bars keep a minimum width so long windows
// (hundreds/thousands of daily bars) stay readable and scroll instead of
// collapsing to invisible slivers. A FROZEN y-axis stays pinned on the left
// (the scrollable plot's own axis is hidden), it opens scrolled to the most
// recent data (right edge), and vertical scroll is clipped so height is constant.
// The caller passes `maxValue` + `tickFormatter` so the frozen axis and the
// hidden plot axis share one domain and line up exactly.
export function ScrollChart({ barCount, height, maxValue, tickFormatter, allowDecimals = true, minBarPx = 16, children }) {
  const scrollRef = useRef(null);
  const [cw, setCw] = useState(0);   // measured container width
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const measure = () => setCw(el.clientWidth);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  // Never narrower than the container — so a handful of bars (week/month/year)
  // simply FILL the width and don't scroll; only many bars (day over a long
  // window) exceed it and scroll.
  const innerW = Math.max(Math.round(barCount * minBarPx), cw || 300);
  const niceMax = maxValue && maxValue > 0 ? Math.ceil(maxValue) : undefined;
  const AXIS_W = 40;
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    // Jump to the most recent (right) ONLY when there's real overflow.
    el.scrollLeft = el.scrollWidth > el.clientWidth ? el.scrollWidth : 0;
  }, [barCount, innerW, cw]);
  return (
    <div className="flex" style={{ height }}>
      {/* Frozen y-axis — never scrolls */}
      <div style={{ width: AXIS_W }} className="flex-shrink-0">
        <ResponsiveContainer width="100%" height={height}>
          <BarChart data={[{ _v: niceMax ?? 0 }]} margin={{ top: 4, right: 0, bottom: 0, left: 0 }}>
            <YAxis type="number" domain={[0, niceMax ?? "auto"]} width={AXIS_W} allowDecimals={allowDecimals}
              tick={{ fill: "#64748b", fontSize: 10 }} tickFormatter={tickFormatter} />
            {/* Invisible X-axis reserves the same bottom space as the plot's X-axis
                so the two charts' 0-lines align. */}
            <XAxis dataKey="_x" tick={false} axisLine={false} tickLine={false} />
            <Bar dataKey="_v" fill="transparent" isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      {/* Scrollable plot: x scrolls, y is clipped so the figure height is fixed */}
      <div ref={scrollRef} className="min-w-0 flex-1 overflow-x-auto overflow-y-hidden">
        <div style={{ width: `${innerW}px`, height }}>
          <ResponsiveContainer width="100%" height={height}>
            {children}
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  );
}

// Category drill-down: apps in a category over the range, as a horizontal-bar
// table (app on the left, time bar + minutes on the right).
export function ScreentimeCategoryModal({ category, start, end, device, onClose }) {
  const { data: apps = [], isLoading } = useQuery({
    queryKey: ["screentime-apps", category, start, end, device],
    queryFn: () => api.getScreentimeApps(start, end, category, device),
  });
  const max = Math.max(1, ...apps.map((a) => a.minutes));
  const color = ST_COLOR[category] || "#94a3b8";
  return (
    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/60 px-4 pb-6"
         onClick={onClose}>
      <div className="w-full max-w-md rounded-2xl bg-slate-800 p-5 max-h-[80vh] overflow-y-auto"
           onClick={(e) => e.stopPropagation()}>
        <div className="mb-4 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="h-3 w-3 rounded-sm" style={{ background: color }} />
            <p className="text-sm font-semibold text-white">{ST_LABEL[category] || category} — apps</p>
          </div>
          <button onClick={onClose} className="text-lg leading-none text-slate-400 hover:text-white">×</button>
        </div>
        {isLoading ? (
          <p className="text-xs text-slate-500">Loading…</p>
        ) : apps.length === 0 ? (
          <p className="text-xs text-slate-500">No apps in this category for the selected range.</p>
        ) : (
          <div className="space-y-2">
            {apps.map((a) => (
              <div key={a.app} className="flex items-center gap-2">
                <span className="w-28 flex-shrink-0 truncate text-xs text-slate-300" title={a.app}>
                  {prettifyApp(a.app)}
                </span>
                <div className="h-4 flex-1 overflow-hidden rounded bg-slate-900">
                  <div className="h-full rounded" style={{ width: `${(a.minutes / max) * 100}%`, background: color }} />
                </div>
                <span className="w-16 flex-shrink-0 text-right text-xs font-semibold text-white">{fmtMin(a.minutes)}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export function fillDateRange(data, start, end) {
  // KEEPS THE WHOLE SOURCE ROW (fixed 2026-09-23). This used to read only `r.value` and
  // rebuild each day as {date, label, value} — which silently discarded every other column.
  // Screen-time rows carry one field PER CATEGORY and no `value`, so filling the range
  // zeroed all of them: the chart aggregated to nothing, trimEmpty dropped every bucket and
  // Exposures hid its SCREEN TIME section entirely. Single-series callers pass {date, value} and are unaffected.
  const byDate = {};
  data.forEach((r) => { byDate[r.date] = r; });
  const result = [];
  const cur = new Date(start + "T12:00:00");
  const endDate = new Date(end + "T12:00:00");
  while (cur <= endDate) {
    const d = cur.toISOString().slice(0, 10);
    const src = byDate[d];
    result.push({ ...(src || {}), date: d, label: fmtDate(d), value: src?.value ?? 0 });
    cur.setDate(cur.getDate() + 1);
  }
  return result;
}

// ── Streak grid ───────────────────────────────────────────────────────────────
