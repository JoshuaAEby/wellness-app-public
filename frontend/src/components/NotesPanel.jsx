import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";

/**
 * Quick capture for feature ideas and bugs noticed while using the app.
 *
 * The point is the gap between noticing something and recording it. Anything that
 * widens that gap — a category picker, a priority, a title field — costs more than it
 * adds, so this is one text box and one button. Notes marked done are kept, not
 * deleted: the record of what was asked for is worth more than a tidy list.
 */
export default function NotesPanel({ onClose }) {
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const [showDone, setShowDone] = useState(false);

  const { data } = useQuery({
    queryKey: ["notes", showDone], queryFn: () => api.getNotes(showDone) });
  const invalidate = () => qc.invalidateQueries({ queryKey: ["notes"] });

  const add = useMutation({ mutationFn: api.addNote,
    onSuccess: () => { setText(""); invalidate(); } });
  const patch = useMutation({ mutationFn: ({ id, ...d }) => api.patchNote(id, d),
    onSuccess: invalidate });
  const del = useMutation({ mutationFn: api.deleteNote, onSuccess: invalidate });

  const notes = data?.notes ?? [];
  const submit = () => { if (text.trim()) add.mutate(text.trim()); };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 pt-16"
         onClick={onClose}>
      <div className="max-h-[80vh] w-full max-w-md overflow-y-auto rounded-2xl bg-card p-5"
           onClick={(e) => e.stopPropagation()}>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-[15px] font-semibold text-ink">Notes</h2>
          <button onClick={onClose} className="text-[11px] text-ink-3 hover:text-ink">Close</button>
        </div>

        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(); }}
          placeholder="An idea, or a bug you just hit…"
          rows={3}
          className="w-full rounded-lg bg-surface px-3 py-2 text-[13px] text-ink outline-none placeholder:text-ink-3"
        />
        <div className="mt-2 flex items-center gap-2">
          <button onClick={submit} disabled={!text.trim() || add.isPending}
                  className="rounded-lg bg-surface px-3 py-1.5 text-[12px] text-ink disabled:opacity-40">
            {add.isPending ? "Saving…" : "Add"}
          </button>
          <span className="text-[9px] text-ink-3">⌘↵ to save</span>
          <button onClick={() => setShowDone((v) => !v)}
                  className="ml-auto text-[10px] text-ink-3 hover:text-ink">
            {showDone ? "hide done" : "show done"}
          </button>
        </div>

        <div className="mt-4">
          {notes.length === 0 && (
            <p className="text-[11px] text-ink-3">
              Nothing yet. Anything written here gets picked up next time we work on the app.
            </p>
          )}
          {notes.map((n) => (
            <div key={n.id} className="border-t border-rule py-2 first:border-t-0">
              <p className={`prose-ui text-[12px] leading-snug ${n.status === "done" ? "text-ink-3 line-through" : "text-ink"}`}>
                {n.body}
              </p>
              <div className="mt-1 flex items-center gap-2">
                <span className="text-[9px] text-ink-3">{(n.created_at || "").slice(0, 10)}</span>
                <button
                  onClick={() => patch.mutate({ id: n.id, status: n.status === "done" ? "open" : "done" })}
                  className="ml-auto text-[10px] text-ink-3 hover:text-ink">
                  {n.status === "done" ? "reopen" : "done"}
                </button>
                <button onClick={() => del.mutate(n.id)}
                        className="text-[10px] text-ink-3 hover:text-[color:var(--over)]">delete</button>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
