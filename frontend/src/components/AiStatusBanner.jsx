import { useQuery } from "@tanstack/react-query";
import { api } from "../api.js";

// Severity drives colour, not the specific cause: "down" needs a person to act,
// "degraded" usually clears on its own, "disabled" is a choice the user made.
const TONE = {
  down:     "border-rose-500/40 bg-rose-500/10 text-rose-300",
  degraded: "border-amber-500/40 bg-amber-500/10 text-amber-300",
  disabled: "border-slate-600 bg-slate-800/60 text-slate-400",
};

/**
 * Shows ONLY when AI is not working. Silence is the healthy state — a permanent
 * "AI: OK" badge is noise that trains you past the one day it says something else.
 *
 * Exists because of 2026-09-04: the Anthropic credit balance ran out, every parse
 * failed, and the only place that fact appeared was a 503 in the log modal whose
 * text blamed the API key and the AI toggle — both of which were fine. Nothing on
 * Today indicated anything was wrong.
 */
export default function AiStatusBanner() {
  const { data } = useQuery({
    queryKey: ["ai-status"],
    queryFn: api.getAiStatus,
    // The endpoint reads in-memory state and costs nothing, but a container that
    // just restarted reports "unknown" until something uses AI, so re-ask
    // periodically rather than once on mount.
    refetchInterval: 120000,
    staleTime: 60000,
  });

  if (!data || data.state === "ok" || data.state === "unknown") return null;

  return (
    <div className={`rounded-xl border px-3 py-2 text-xs ${TONE[data.state] || TONE.degraded}`}>
      <div className="font-medium">
        AI unavailable — {data.label}
        {data.feature ? <span className="font-normal opacity-70"> ({data.feature.replace(/_/g, " ")})</span> : null}
      </div>
      {data.action && <div className="mt-0.5 opacity-90">{data.action}</div>}
      {/* The raw error, kept because the classifier can be wrong and the verbatim
          message is what actually diagnoses an unrecognised failure. */}
      {data.cause === "unknown" && data.detail && (
        <div className="mt-1 font-mono opacity-70">{data.detail}</div>
      )}
      {data.minutes_since_last_ok != null && (
        <div className="mt-0.5 opacity-60">
          Last worked {data.minutes_since_last_ok} min ago
          {data.consecutive_failures > 1 ? ` · ${data.consecutive_failures} failures since` : ""}
        </div>
      )}
    </div>
  );
}
