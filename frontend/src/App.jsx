import { useState, useEffect, lazy, Suspense } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, getToken, saveToken, clearToken, sendQueued } from "./api.js";
import { replay, startSync } from "./offline.js";
import OfflineBanner from "./components/OfflineBanner.jsx";
import Nav from "./components/Nav.jsx";
import MorningCheckinModal from "./components/MorningCheckinModal.jsx";
import EveningCheckinModal from "./components/EveningCheckinModal.jsx";
import SettingsModal from "./components/SettingsModal.jsx";
import Today from "./tabs/Today.jsx";
// Code-split (2026-10-21): Today is ~81% of visits and loads with the app; the other tabs
// are fetched on first open (and then cached by the service worker), which takes them
// out of the startup bundle that every deploy makes the phone re-download.
const Fitness = lazy(() => import("./tabs/Fitness.jsx"));
const Insights = lazy(() => import("./tabs/Insights.jsx"));
const Body = lazy(() => import("./tabs/Body.jsx"));
const Behaviors = lazy(() => import("./tabs/Behaviors.jsx"));
import ErrorBoundary from "./components/ErrorBoundary.jsx";
import NotesPanel from "./components/NotesPanel.jsx";
import SpendMenu from "./components/SpendMenu.jsx";

// ── Login ─────────────────────────────────────────────────────────────────────

function LoginForm({ onLogin }) {
  const [username, setUsername] = useState("owner");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const data = await api.login(username, password);
      saveToken(data.access_token);
      onLogin();
    } catch {
      setError("Invalid credentials");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-canvas">
      <div className="w-full max-w-sm rounded-2xl border border-rule bg-card p-8 shadow-xl">
        <h1 className="mb-2 text-center text-2xl font-bold text-white">Wellness</h1>
        <p className="mb-8 text-center text-sm text-slate-400">Sign in to continue</p>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="mb-1 block text-xs font-medium text-slate-400">Username</label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="w-full rounded-lg bg-slate-700 px-4 py-2 text-white outline-none focus:ring-2 focus:ring-indigo-500"
              required
            />
          </div>
          <div>
            <label className="mb-1 block text-xs font-medium text-slate-400">Password</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-lg bg-slate-700 px-4 py-2 text-white outline-none focus:ring-2 focus:ring-indigo-500"
              required
            />
          </div>
          {error && <p className="text-sm text-rose-400">{error}</p>}
          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-lg bg-indigo-600 py-2 font-semibold text-white transition hover:bg-indigo-500 disabled:opacity-50"
          >
            {loading ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}

// ── Main app ──────────────────────────────────────────────────────────────────

// `fitness` wraps Cardio, Strength, Recovery and Sleep behind one nav entry (see tabs/Fitness.jsx).
// `training`/`recovery` stay mapped so any saved tab id from before the merge still
// resolves instead of rendering a blank screen.
const TAB_COMPONENTS = { today: Today, fitness: Fitness, training: Fitness, recovery: Fitness, trends: Insights, insights: Insights, body: Body, habits: Behaviors };

// Tabs that show a TimeWindowSelector — they share the same window state
const WINDOWED_TABS = new Set(["fitness", "training", "recovery", "body"]);

function Dashboard({ onLogout }) {
  // Offline queue (src/offline.js): replay on start, on reconnect, when the app becomes
  // visible and every 20 s while anything waits; refetch everything once logs land.
  const qcSync = useQueryClient();
  useEffect(() => startSync(sendQueued, () => qcSync.invalidateQueries()), [qcSync]);
  const [tab, setTab] = useState("today");
  // How many whole windows back the windowed tabs are showing. Lives here rather
  // than per-tab so switching Fitness → Body keeps the period you were looking at.
  const [windowOffset, setWindowOffset] = useState(0);
  const [showSettings, setShowSettings] = useState(false);
  const [showNotes, setShowNotes] = useState(false);
  const [timeWindow, setTimeWindow] = useState("3M");
  const [checkinDone, setCheckinDone] = useState(false);
  const [eveningDone, setEveningDone] = useState(false);
  const TabComponent = TAB_COMPONENTS[tab];
  // Screen-view counting (2026-09-13) — see routers/telemetry.
  useEffect(() => { api.logView(`tab/${tab}`); }, [tab]);

  // Check-in takeovers. The ["today"] query is also fetched by the Today tab,
  // so this shares the same cache entry. Morning owns the screen before the
  // evening hour; from EVENING_HOUR on, the day-quality prompt takes priority
  // (a morning check-in filled 12h late is stale data anyway — it stays
  // reachable from the Today tab).
  const EVENING_HOUR = 19;
  const { data: todayData } = useQuery({ queryKey: ["today"], queryFn: api.today });
  const { data: notesData } = useQuery({ queryKey: ["notes", false], queryFn: () => api.getNotes(false) });
  const openNotes = notesData?.open_count ?? 0;
  const hour = new Date().getHours();
  const needsCheckin =
    !checkinDone && todayData !== undefined && !todayData.away &&
    todayData.morning_checkin === null && hour < EVENING_HOUR;
  const needsEvening =
    !eveningDone && !needsCheckin && todayData !== undefined && !todayData.away &&
    hour >= EVENING_HOUR && (todayData.morning_checkin?.day_score ?? null) === null;

  const handleRefresh = () => {
    window.location.reload();
  };

  return (
    <div className="fixed inset-0 flex flex-col bg-canvas">
      {/* Header */}
      <header className="sticky top-0 z-40 border-b border-rule bg-canvas px-4 py-3 flex items-center justify-between">
        <button
          onClick={handleRefresh}
          className="text-base font-bold text-white flex items-center gap-2 active:opacity-70 transition-opacity"
        >
          Wellness
          <svg
            viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
            className="w-3.5 h-3.5 text-slate-500"
          >
            <path strokeLinecap="round" strokeLinejoin="round"
              d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
        </button>
        <div className="flex items-center gap-3">
          <SpendMenu />
          <button
            onClick={() => setShowNotes(true)}
            aria-label="Notes"
            className="relative rounded-lg p-1.5 text-slate-400 hover:text-white transition-colors"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="w-5 h-5">
              <path strokeLinecap="round" strokeLinejoin="round"
                d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
            </svg>
            {openNotes > 0 && (
              <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-[9px] font-semibold text-white"
                    style={{ background: "var(--hrv)" }}>
                {openNotes}
              </span>
            )}
          </button>
          <button
            onClick={() => setShowSettings(true)}
            className="rounded-lg p-1.5 text-slate-400 hover:text-white transition-colors"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="w-5 h-5">
              <path strokeLinecap="round" strokeLinejoin="round" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
              <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
            </svg>
          </button>
        </div>
      </header>

      {/* Offline / sync status — renders nothing when online with an empty queue. */}
      <OfflineBanner onSyncNow={() => replay(sendQueued, () => qcSync.invalidateQueries())} />

      {/* Tab content — scrollable, padded above nav */}
      <main className="flex-1 overflow-y-auto pb-4">
        {/* Keyed by tab so switching tabs clears any error state. The boundary sits
            INSIDE <main>, leaving the header and nav mounted — a broken tab must
            never cost you the ability to navigate away from it. */}
        <ErrorBoundary key={tab} label={tab}>
          <Suspense fallback={<p className="p-6 text-center text-[11px] text-ink-3">Loading…</p>}>
          <TabComponent
            onNavigate={setTab}
            {...(tab === "recovery" ? { initialView: "recovery" } : {})}
            {...(WINDOWED_TABS.has(tab)
              ? { window: timeWindow, onWindowChange: setTimeWindow,
                  windowOffset, onWindowOffsetChange: setWindowOffset }
              : {})}
          />
          </Suspense>
        </ErrorBoundary>
      </main>

      <Nav active={tab === "training" || tab === "recovery" ? "fitness" : tab} onSelect={setTab} />

      {showSettings && <SettingsModal onClose={() => setShowSettings(false)} onLogout={onLogout} />}
      {showNotes && <NotesPanel onClose={() => setShowNotes(false)} />}
      {needsCheckin && <MorningCheckinModal onDone={() => setCheckinDone(true)} />}
      {needsEvening && <EveningCheckinModal onDone={() => setEveningDone(true)} />}
    </div>
  );
}

// ── Root ──────────────────────────────────────────────────────────────────────

export default function App() {
  const [authed, setAuthed] = useState(!!getToken());

  useEffect(() => {
    const check = () => setAuthed(!!getToken());
    window.addEventListener("storage", check);
    return () => window.removeEventListener("storage", check);
  }, []);

  if (!authed) {
    return <LoginForm onLogin={() => setAuthed(true)} />;
  }

  return (
    <Dashboard
      onLogout={() => { clearToken(); setAuthed(false); }}
    />
  );
}
