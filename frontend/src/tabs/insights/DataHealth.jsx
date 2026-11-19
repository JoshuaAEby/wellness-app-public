import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api.js";
import { windowDates, fmtDate } from "../../utils.js";
import { Card, Eyebrow, HeroSub, K, Sep, Foot, EmptyState } from "../../components/ui.jsx";

/**
 * How good is the data behind everything else in this tab?
 *
 * Signals had eight sections and still read thin, because it had no stated job —
 * it was whatever wasn't a finding or an experiment. This is the job: every number
 * in Insights rests on ingestion that can silently stop, days that get excluded,
 * and a correlation cache that only refreshes weekly. None of that was visible
 * anywhere, and all three have already caused real confusion:
 *
 *   • VO2max quietly stopped accruing for weeks — invisible until queried by hand.
 *   • Half of recent outdoor walks recorded no heart rate, so they contribute
 *     ZERO training load; ATL/CTL have been reading low because of it.
 *   • Findings is dated by a cache computed on a Sunday, so a Thursday reading is
 *     five days old and looks identical to Wednesday's.
 *
 * A dashboard that can't tell you it has stopped being fed is worse than one that
 * shows nothing.
 */

const DAY_MS = 86400000;

function ago(iso) {
  if (!iso) return null;
  const d = new Date(String(iso).length <= 10 ? `${iso}T12:00:00` : iso);
  if (isNaN(d)) return null;
  return Math.floor((Date.now() - d.getTime()) / DAY_MS);
}

function Block({ label, children }) {
  return (
    <div className="border-t border-rule pt-3">
      <p className="text-[9px] tracking-micro text-ink-3">{label}</p>
      <div className="mt-1.5">{children}</div>
    </div>
  );
}

/**
 * Which inputs are still arriving — collapsed to one line until something is wrong.
 *
 * The `SYNC` block above answers "did Apple Health talk to us today", which is a
 * different and much narrower question: a stream can stop while the sync itself
 * stays healthy. `partner_present` produced zero rows for six weeks that way.
 *
 * Acknowledging is a SNOOZE, never a mute — see services/metric_freshness. The
 * copy has to say so, or the button reads as "make this go away" and the next
 * genuine breakage of the same feed goes unseen.
 */
const STATE_STYLE = {
  never:        { color: "var(--over)",      label: "never arrived" },
  stale:        { color: "var(--breakfast)", label: "stalled" },
  acknowledged: { color: "var(--ink-3)",     label: "acknowledged" },
  new:          { color: "var(--ink-3)",     label: "too new to judge" },
  live:         { color: "var(--under)",     label: "live" },
};

function InputsBlock() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const freshQ = useQuery({
    queryKey: ["freshness"],
    queryFn: api.getFreshness,
    staleTime: 1000 * 60 * 10,
  });
  const { data } = freshQ;

  const ack = useMutation({
    mutationFn: ({ metric, on }) =>
      on ? api.ackFreshness(metric) : api.unackFreshness(metric),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["freshness"] }),
  });

  const streams = data?.streams ?? [];
  const attention = data?.needs_attention ?? 0;
  // Live streams are the bulk and the boring part; show them only on request.
  const shown = open ? streams : streams.filter((s) => s.state !== "live");

  // This block IS the "is anything not arriving" monitor, so it is the last thing
  // that may vanish silently — a failed request here used to remove the alarm
  // along with the data it guards (note 53).
  if (!streams.length) {
    return (
      <Block label="INPUTS">
        <EmptyState query={freshQ} message="No input streams reported — the freshness check returned nothing, which should never happen with data flowing." />
      </Block>
    );
  }

  return (
    <Block label="INPUTS">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-baseline gap-2 text-left"
      >
        <span className="text-[11.5px] text-ink-2">
          {attention === 0
            ? <><K>{streams.length}</K> inputs, all arriving on schedule</>
            : <span style={{ color: "var(--over)" }}>
                <b className="font-semibold">{attention}</b> of {streams.length} inputs
                not arriving
              </span>}
        </span>
        <span className="ml-auto text-[9px] text-ink-3">{open ? "▲ hide all" : "▼ show all"}</span>
      </button>

      {shown.length > 0 && (
        <div className="mt-1.5 space-y-1">
          {shown.map((s) => {
            const st = STATE_STYLE[s.state] ?? STATE_STYLE.live;
            const actionable = s.state === "stale" || s.state === "acknowledged";
            return (
              <div key={s.metric} className="flex items-baseline gap-2">
                <span className="min-w-0 flex-1 truncate text-[10.5px] text-ink-2">
                  {s.label}
                </span>
                <span className="shrink-0 text-[9.5px]" style={{ color: st.color }}>
                  {s.state === "never"
                    ? "never arrived"
                    : s.state === "live"
                      ? `${s.days_since}d ago`
                      : s.state === "new"
                        ? `${s.n_obs} obs — too new`
                        : `quiet ${s.days_since}d / usually ${s.typical_gap_days}d`}
                </span>
                {actionable && (
                  <button
                    onClick={() => ack.mutate({ metric: s.metric,
                                                on: s.state === "stale" })}
                    disabled={ack.isPending}
                    className="shrink-0 rounded-full border border-rule px-1.5 py-0.5 text-[9px] text-ink-3 hover:text-ink-2 disabled:opacity-40"
                  >
                    {s.state === "stale" ? "that's real" : "un-snooze"}
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}

      <p className="mt-1 text-[9.5px] leading-snug text-ink-3">
        Each threshold comes from that input's own history, so a daily feed is late
        after days and a sporadic one after weeks. Marking a silence real{" "}
        <b className="font-semibold text-ink-2">snoozes</b> it for one more expected
        gap rather than muting it — nothing here is ever excluded from analysis on
        its own, because only you can tell "didn't happen" from "stopped recording".
      </p>
    </Block>
  );
}

export default function DataHealth() {
  const { start, end } = windowDates("3M");

  const { data: todayData } = useQuery({ queryKey: ["today"], queryFn: api.today });
  const { data: flags } = useQuery({
    queryKey: ["day-flags", start, end],
    queryFn: () => api.getDayFlags(start, end),
    staleTime: 1000 * 60 * 30,
  });
  const { data: corr } = useQuery({
    queryKey: ["correlations", 180],
    queryFn: () => api.getCorrelations(180),
    staleTime: 1000 * 60 * 30,
  });

  const { data: errs } = useQuery({
    queryKey: ["error-ledger"],
    queryFn: () => api.getErrorLedger(7),
    staleTime: 1000 * 60 * 5,
  });
  const { data: cit } = useQuery({
    queryKey: ["citation-checks"],
    queryFn: () => api.getCitationChecks(30),
    staleTime: 1000 * 60 * 30,
  });

  const syncDays = ago(todayData?.last_sync_at);
  const missing = todayData?.data_health?.missing_inputs ?? [];

  const cacheAge = useMemo(() => {
    const rows = corr?.results ?? [];
    return ago(rows[0]?.computed_at);
  }, [corr]);

  const coverage = useMemo(() => {
    const rows = flags?.flags ?? [];
    const byReason = {};
    for (const f of rows) byReason[f.reason] = (byReason[f.reason] ?? 0) + 1;
    const spanDays = Math.max(1, Math.round((new Date(`${end}T12:00:00`) - new Date(`${start}T12:00:00`)) / DAY_MS));
    // `incomplete_data` is nutrition-only and is NOT a whole-day exclusion, so it is
    // counted separately rather than folded into "days lost".
    const excluded = rows.filter((f) => f.reason !== "incomplete_data").length;
    return { byReason, spanDays, excluded, incomplete: byReason.incomplete_data ?? 0 };
  }, [flags, start, end]);

  return (
    <Card>
      <Eyebrow left="DATA HEALTH" right={`LAST ${coverage.spanDays}D`} />
      <HeroSub>
        Everything below rests on this. Ingestion can stop without erroring, and a
        metric that quietly stops arriving looks exactly like a metric that stopped
        moving.
      </HeroSub>

      <div className="mt-3 space-y-3">
        <Block label="SYNC">
          <p className="text-[11.5px] text-ink-2">
            {syncDays == null
              ? <span className="text-ink-3">No sync timestamp recorded.</span>
              : syncDays <= 1
                ? <>Apple Health last synced <K>{syncDays === 0 ? "today" : "yesterday"}</K>.</>
                : <span style={{ color: syncDays > 3 ? "var(--over)" : "var(--breakfast)" }}>
                    Apple Health last synced <b className="font-semibold">{syncDays} days ago</b> —
                    today's numbers are stale.
                  </span>}
          </p>
          {missing.length > 0 && (
            <p className="mt-1 text-[10.5px]" style={{ color: "var(--breakfast)" }}>
              Missing today: {missing.join(" · ")}
            </p>
          )}
        </Block>

        <InputsBlock />

        <Block label="COVERAGE">
          <p className="text-[11.5px] text-ink-2">
            <K>{coverage.excluded}</K> of {coverage.spanDays} days excluded from analysis
            {coverage.incomplete > 0 && (
              <><Sep /><K>{coverage.incomplete}</K> more with incomplete nutrition</>
            )}
          </p>
          {Object.keys(coverage.byReason).length > 0 && (
            <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5">
              {Object.entries(coverage.byReason).sort((a, b) => b[1] - a[1]).map(([r, n]) => (
                <span key={r} className="text-[10px] text-ink-3">
                  <b className="font-semibold text-ink-2">{n}</b> {r.replace(/_/g, " ")}
                </span>
              ))}
            </div>
          )}
          <p className="mt-1 text-[9.5px] leading-snug text-ink-3">
            Excluded days are dropped from correlations and experiment eligibility, so
            they slow every accruing result. Incomplete-nutrition days are dropped only
            from nutrition figures.
          </p>
        </Block>

        <Block label="ERRORS · 7 DAYS">
          {!errs ? null : errs.kinds === 0 ? (
            <p className="text-[11px] text-ink-2">No errors logged.</p>
          ) : (
            <>
              <p className="text-[11px] text-ink-2 tabular-nums">
                <K>{errs.kinds}</K> kind{errs.kinds === 1 ? "" : "s"} · {errs.total} occurrence{errs.total === 1 ? "" : "s"}
              </p>
              {errs.errors.slice(0, 8).map((e) => (
                <p key={e.template + e.logger} className="mt-0.5 text-[9.5px] leading-snug text-ink-3">
                  <span className="tabular-nums text-ink-2">{e.count}×</span> {e.logger} — {e.sample}
                  <span className="text-ink-3"> · last {new Date(e.last_at.replace(" ", "T") + "Z").toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}</span>
                </p>
              ))}
            </>
          )}
          <p className="mt-1 text-[9.5px] leading-snug text-ink-3">
            Every error the server logs, grouped by kind. Most are handled and the app carries on
            with a default — which is exactly why they need to be visible. A new kind triggers one
            notification.
          </p>
        </Block>

        <Block label="SUMMARY CITATIONS">
          {!cit || cit.total === 0 ? (
            <p className="text-[11px] text-ink-3">No summaries checked yet — the first runs with tomorrow's briefing.</p>
          ) : (
            <>
              <p className="text-[11px] text-ink-2 tabular-nums">
                {cit.total} summaries checked in {cit.days} d · <K>{cit.clean}</K> clean first time
                {cit.fixed > 0 && <> · {cit.fixed} fixed on rewrite</>}
                {cit.unresolved > 0 && <span style={{ color: "var(--over)" }}> · {cit.unresolved} still quoting unsupported numbers</span>}
              </p>
              {(cit.recent_problems || []).map((r) => (
                <p key={r.checked_at} className="mt-0.5 text-[9.5px] leading-snug text-ink-3">
                  {r.kind} {r.period}: {r.bad_first.join(", ")}
                  {r.bad_final.length ? " — kept after rewrite" : " — corrected"}
                </p>
              ))}
            </>
          )}
          <p className="mt-1 text-[9.5px] leading-snug text-ink-3">
            Every ρ and n a written summary quotes must appear in the data it was given;
            a miss triggers one automatic rewrite.
          </p>
        </Block>

        <Block label="CORRELATION CACHE">
          <p className="text-[11.5px] text-ink-2">
            {cacheAge == null
              ? <span className="text-ink-3">No cache timestamp.</span>
              : <>Recomputed <K>{cacheAge === 0 ? "today" : `${cacheAge} days ago`}</K>
                  {cacheAge >= 6 && (
                    <span className="text-ink-3"> — refreshes Sundays, so this is about to update</span>
                  )}</>}
          </p>
          <p className="mt-1 text-[9.5px] leading-snug text-ink-3">
            Findings is read from this cache, not computed live. Two readings on
            different days will be identical unless a refresh happened in between —
            unchanged numbers there mean "not recomputed", not "nothing moved".
          </p>
        </Block>
      </div>

      <Foot>
        None of this is a metric about you — it is a metric about the pipeline. It sits
        first because a confident number drawn from three-day-old data is worse than no
        number at all.
      </Foot>
    </Card>
  );
}
