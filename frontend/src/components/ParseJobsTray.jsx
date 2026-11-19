import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";
import { Card, Eyebrow } from "./ui.jsx";

// Background food parses waiting for review (2026-10-21). "Parse in background" in the log
// modal hands the text to the server and closes; finished parses land here. Tapping one
// reopens the modal pre-filled, so portions are still reviewed before anything is saved.
// Renders nothing when the queue is empty; polls only while something is still parsing.
const clip = (s, n = 42) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

export default function ParseJobsTray({ onReview }) {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["parse-jobs"],
    queryFn: api.getParseJobs,
    refetchInterval: (q) => (q.state.data?.jobs ?? []).some((j) => j.status === "pending") ? 3000 : false,
  });
  const update = useMutation({
    mutationFn: ({ id, status }) => api.updateParseJob(id, status),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["parse-jobs"] }),
  });
  const jobs = data?.jobs ?? [];
  if (!jobs.length) return null;

  return (
    <Card>
      <Eyebrow left="TO REVIEW" right={`${jobs.length} PARSE${jobs.length === 1 ? "" : "S"}`} />
      <div className="mt-1 divide-y divide-rule">
        {jobs.map((j) => (
          <div key={j.id} className="flex items-center gap-2 py-2">
            <button
              type="button"
              disabled={j.status !== "done"}
              onClick={() => onReview(j)}
              className="min-w-0 flex-1 text-left disabled:cursor-default"
            >
              <p className="text-[12px] text-ink">
                <span className="text-ink-3 capitalize">{j.meal_type}</span> · {clip(j.description)}
              </p>
              <p className="text-[10px]"
                 style={{ color: j.status === "error" ? "var(--over)" : "var(--ink-3)" }}>
                {j.status === "pending" && "Parsing…"}
                {j.status === "done" && `Ready — tap to review (${(j.result?.items ?? []).length} item${(j.result?.items ?? []).length === 1 ? "" : "s"})`}
                {j.status === "error" && `Failed: ${clip(j.error || "unknown error", 60)}`}
              </p>
            </button>
            {j.status === "error" && (
              <button type="button" onClick={() => update.mutate({ id: j.id, status: "pending" })}
                className="text-[11px] text-ink-2">Retry</button>
            )}
            {j.status !== "pending" && (
              <button type="button" aria-label="Dismiss"
                onClick={() => update.mutate({ id: j.id, status: "dismissed" })}
                className="px-1 text-ink-3">✕</button>
            )}
          </div>
        ))}
      </div>
    </Card>
  );
}
