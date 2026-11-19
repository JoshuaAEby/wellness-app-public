import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";
import { fmtDate } from "../utils.js";

// "Who watched?" for TV sessions (note 85): the TV cannot say. Sessions left unanswered are
// unknown, so ignoring a row is always fine. Tapped rows
// stay put with their own Undo, the one-tap convention, so nothing moves under a thumb;
// once every row is answered the card goes, after UNDO_MS so the last Undo is reachable.
const UNDO_MS = 4000;
const WATCHERS = [["me", "Me"], ["family", "Family"], ["other", "Other"]];
const clock = (hhmm) => {
  const [h, m] = hhmm.split(":").map(Number);
  return `${h % 12 || 12}:${String(m).padStart(2, "0")} ${h < 12 ? "am" : "pm"}`;
};

function Row({ s, answer, setAnswer }) {
  const send = (mine) => { setAnswer(mine); api.answerTv(s.id, mine).catch(() => setAnswer(null)); };
  return (
    <div className="flex items-center gap-2 py-1.5">
      <p className="min-w-0 flex-1 text-[11.5px] leading-snug text-ink-2">
        {fmtDate(s.date)}, {clock(s.start)}
        <span className="block text-[10px] text-ink-3">{Object.keys(s.apps).join(" · ")} · {s.minutes} min</span>
      </p>
      {answer === null ? WATCHERS.map(([key, label]) => (
        <button key={key} onClick={() => send(key)}
          className={`rounded-md border border-rule px-2 py-1 text-[11px] ${key === "me" ? "font-semibold text-ink-2" : "text-ink-3"}`}>
          {label}
        </button>
      )) : (
        <button onClick={() => send(null)} className="px-1 py-1 text-[11px] text-ink-3">
          ✓ {WATCHERS.find(([k]) => k === answer)[1]} · Undo
        </button>
      )}
    </div>
  );
}

export default function TvCheck() {
  const qc = useQueryClient();
  const { data: sessions = [] } = useQuery({ queryKey: ["tvPending"], queryFn: api.getTvPending });
  const [answers, setAnswers] = useState({});
  const allDone = sessions.length > 0 && sessions.every((s) => answers[s.id] != null);
  useEffect(() => {
    if (!allDone) return undefined;
    const t = setTimeout(() => qc.invalidateQueries({ queryKey: ["tvPending"] }), UNDO_MS);
    return () => clearTimeout(t);
  }, [allDone, qc]);
  if (!sessions.length) return null;
  return (
    <div className="rounded-2xl border border-rule bg-card px-4 py-2.5">
      <p className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">TV · WHO WATCHED?</p>
      <div className="mt-1 divide-y divide-rule">
        {sessions.map((s) => (
          <Row key={s.id} s={s} answer={answers[s.id] ?? null}
            setAnswer={(v) => setAnswers((a) => ({ ...a, [s.id]: v }))} />
        ))}
      </div>
    </div>
  );
}
