import { useQuery } from "@tanstack/react-query";
import { api } from "../api.js";
import { Card, Eyebrow, HeroSub } from "./ui.jsx";

const COLOR = { improved: "var(--rhr)", worsened: "var(--over)", neutral: "var(--ink)" };

// The answer before the charts (2026-11-02): this page gets far more visits than Progress, which
// holds the verdicts. It covers all of training, so it sits above the window selector. The card
// holds its place while loading, so the view below doesn't jump when it arrives.
export default function ViewVerdict({ view, right }) {
  const q = useQuery({ queryKey: ["progress"], queryFn: () => api.getProgress(), staleTime: 30 * 60 * 1000 });
  const h = q.data?.headlines?.[view];
  if (!q.isLoading && !h) return null;
  return (
    <Card>
      <Eyebrow left="PROGRESS" right={right} />
      {h ? (
        <>
          <p className="prose-ui mt-2 text-[14px] font-semibold leading-snug" style={{ color: COLOR[h.state] ?? COLOR.neutral }}>
            {h.title}
          </p>
          <HeroSub>{h.detail}</HeroSub>
        </>
      ) : <p className="mt-2 min-h-[64px] text-[11px] text-ink-3">Loading…</p>}
    </Card>
  );
}
