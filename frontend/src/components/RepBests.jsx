import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api.js";
import { fmtDate } from "../utils.js";
import { Card, Eyebrow, Hero, HeroSub, K, Sep, Foot } from "./ui.jsx";

// Rep-range bests — the PR tracker (2026-10-29). His logged sets are mostly class sets at
// fixed reps, warm-ups and time-capped rounds, so averages (and per-session e1RM) wobble
// with the class format. A best only moves when he actually lifts more: the heaviest load
// for AT LEAST 8 / 10 / 12 / 15 reps. Computed in services/strength.rep_bests_from_sets.
// Hero = PR days in the window; the table is the selected lift's bests now vs the window start.

const short = (t) => t.replace(/ \((Dumbbell|Barbell|Kettlebell|Cable|Machine)\)$/, "");

export default function RepBests({ days }) {
  const [sel, setSel] = useState(null);
  const { data } = useQuery({
    queryKey: ["hevy-rep-bests", days],
    queryFn: () => api.hevyRepBests(days, 10),
    staleTime: 1000 * 60 * 30,
  });
  const lifts = data?.lifts ?? [];
  if (!lifts.length) return null;
  const prs = data.prs ?? [];
  // Lifts arrive most-recently-done first, so it opens on what he just did (10-29: today's
  // lateral raises and a first-ever KB sumo row were missing from the first version).
  const lift = lifts.find((l) => l.title === sel) ?? lifts[0];
  // "40 × 10 ×3" — identical sets folded.
  const lastSummary = (() => {
    const groups = [];
    for (const x of lift.last_sets ?? []) {
      const g = groups.find((y) => y.lb === x.lb && y.reps === x.reps);
      if (g) g.n += 1; else groups.push({ ...x, n: 1 });
    }
    return groups.map((g) => `${g.lb} × ${g.reps}${g.n > 1 ? ` ×${g.n}` : ""}`).join(", ");
  })();
  const latest = prs[0];
  const liftPrs = prs.filter((p) => p.title === lift.title);

  return (
    <Card>
      <Eyebrow left="REP-RANGE BESTS · PRs" right={`SINCE ${fmtDate(data.window_start).toUpperCase()}`} />
      <Hero value={prs.length} unit={[prs.length === 1 ? "PR DAY" : "PR DAYS", `${lifts.length} LIFTS DONE`]} />
      <HeroSub>
        {latest ? (
          <>Latest: <K>{short(latest.title)} {latest.lb} × {latest.reps}</K>
            <Sep />{fmtDate(latest.date)}<Sep />best for {latest.brackets.map((r) => `${r}+`).join(", ")} reps</>
        ) : (
          <>No lift beat a previous best in this window.</>
        )}
      </HeroSub>

      <div className="mt-3 flex flex-wrap gap-1.5">
        {lifts.map((l) => {
          const active = l.title === lift.title;
          return (
            <button key={l.title} onClick={() => setSel(l.title)}
              className={`rounded-full px-2.5 py-1 text-[10px] font-medium transition-colors ${
                active ? "bg-surface text-ink ring-1 ring-inset ring-[var(--rem)]"
                       : "bg-surface text-ink-3 hover:text-ink-2"}`}>
              {short(l.title)}
            </button>
          );
        })}
      </div>

      {lastSummary && (
        <p className="mt-2.5 text-[11px] tabular-nums text-ink-2">
          Last done {fmtDate(lift.last_date)}: <span className="text-ink">{lastSummary}</span>
          {lift.sessions === 1 && <span className="text-ink-3"> · first time</span>}
        </p>
      )}
      <table className="mt-2 w-full text-[11.5px] tabular-nums">
        <thead>
          <tr className="text-left text-[9px] tracking-micro text-ink-3">
            <th className="pb-1 font-semibold">REPS</th>
            <th className="pb-1 font-semibold">BEST</th>
            <th className="pb-1 font-semibold">SET ON</th>
            <th className="pb-1 text-right font-semibold">VS {fmtDate(data.window_start).toUpperCase()}</th>
          </tr>
        </thead>
        <tbody>
          {lift.brackets.map((b) => {
            const d = b.start_lb != null ? b.best_lb - b.start_lb : null;
            return (
              <tr key={b.reps} className="border-t border-rule">
                <td className="py-1.5 text-ink-2">{b.reps}+</td>
                <td className="py-1.5 font-semibold text-ink">{b.best_lb} lb</td>
                <td className="py-1.5 text-ink-2">
                  {fmtDate(b.best_date)}
                  {b.matched_date && (
                    <span className="block text-[9.5px] text-ink-3">matched {fmtDate(b.matched_date)}</span>
                  )}
                </td>
                <td className="py-1.5 text-right text-ink-2">
                  {d == null ? "new" : d === 0 ? "—" : <K>+{d} lb</K>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {liftPrs.length > 0 && (
        <p className="mt-2 text-[10.5px] text-ink-3">
          {liftPrs.length} PR day{liftPrs.length > 1 ? "s" : ""} for this lift in the window.
        </p>
      )}
      <Foot>
        Heaviest weight lifted for at least that many reps, warm-up sets excluded, since
        Apr 2 (when dumbbells started being logged as pairs). A best only rises when you
        actually lift more, so easy, short or time-capped sets can't drag it down. Equalling
        a best shows as "matched"; a rep range's first appearance isn't counted as a PR —
        nothing was beaten. Lifts are listed most recently done first.
      </Foot>
    </Card>
  );
}
