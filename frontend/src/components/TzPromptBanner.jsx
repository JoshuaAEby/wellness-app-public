import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";

// "You're traveling — where are you?" (services/tz.prompt_state). Renders nothing unless
// the server is unsure which time zone today belongs to: the phone is on a non-home
// offset with no zone entered, a sync mixed offsets, or it's an away day with no signal.
// The answer becomes a manual time-zone period, so days abroad bucket correctly.

const city = (z) => z.split("/").pop().replace(/_/g, " ");

function offsetLabel(zone) {
  try {
    const part = new Intl.DateTimeFormat("en-US", { timeZone: zone, timeZoneName: "shortOffset" })
      .formatToParts(new Date()).find((p) => p.type === "timeZoneName");
    return part ? part.value : "";
  } catch {
    return "";
  }
}

const REASON_TEXT = {
  offset: (o) => `Your phone is on UTC${o >= 0 ? "+" : "−"}${Math.abs(o) / 60}. Where are you?`,
  mixed: () => "Your phone reported more than one time zone. Where are you?",
  no_signal: () => "You're traveling and the app has no time-zone signal today. Where are you?",
};

export default function TzPromptBanner() {
  const qc = useQueryClient();
  const [other, setOther] = useState("");
  const [showOther, setShowOther] = useState(false);
  const { data } = useQuery({ queryKey: ["tz-prompt"], queryFn: api.getTzPrompt, staleTime: 5 * 60_000 });

  const allZones = useMemo(() => {
    try { return Intl.supportedValuesOf("timeZone"); } catch { return []; }
  }, []);

  const confirm = useMutation({
    mutationFn: (tz) => api.confirmTz(tz),
    onSuccess: () => {
      ["tz-prompt", "today", "trips"].forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
      setOther(""); setShowOther(false);
    },
  });

  if (!data?.needed) return null;
  const text = (REASON_TEXT[data.reason] || REASON_TEXT.no_signal)(data.offset_min ?? 0);

  return (
    <div className="rounded-xl border px-3 py-3"
         style={{ borderColor: "color-mix(in srgb, var(--hrv) 45%, transparent)",
                  background: "color-mix(in srgb, var(--hrv) 10%, transparent)" }}>
      <p className="text-sm text-white">🌍 {text}</p>
      <p className="mt-0.5 text-[11px] text-slate-400">
        So today's data lands on the right day. Your answer carries forward while you're away.
      </p>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {(data.candidates || []).map((z) => (
          <button key={z} onClick={() => confirm.mutate(z)} disabled={confirm.isPending}
            className="rounded-lg bg-slate-800 px-2.5 py-1 text-xs text-slate-200 disabled:opacity-50">
            {z === data.home ? `Home (${city(z)})` : city(z)}{" "}
            <span className="text-slate-500">{offsetLabel(z)}</span>
          </button>
        ))}
        <button onClick={() => setShowOther((v) => !v)}
          className="rounded-lg px-2.5 py-1 text-xs text-slate-400">Other…</button>
      </div>
      {showOther && (
        <div className="mt-2 flex gap-2">
          <input list="tz-all" value={other} onChange={(e) => setOther(e.target.value)}
            placeholder="Search, e.g. Europe/Istanbul"
            className="flex-1 rounded-lg bg-slate-700 px-2 py-1.5 text-sm text-white placeholder:text-slate-500" />
          <datalist id="tz-all">
            {allZones.map((z) => <option key={z} value={z} />)}
          </datalist>
          <button onClick={() => confirm.mutate(other.trim())}
            disabled={!other.trim() || confirm.isPending}
            className="rounded-lg bg-indigo-600 px-3 text-xs font-semibold text-white disabled:opacity-40">Set</button>
        </div>
      )}
      {confirm.isError && <p className="mt-1 text-xs text-rose-400">{String(confirm.error?.message || confirm.error)}</p>}
    </div>
  );
}
