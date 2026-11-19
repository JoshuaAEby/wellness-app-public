import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api.js";
import { Card, Eyebrow, HeroSub, K, Sep, Foot } from "../../components/ui.jsx";
import { bestEvidence } from "../../lib/behaviors.js";

/**
 * CONTEXT — the conditions a day happened under, rather than a choice you tick off.
 *
 * All of this was already being ingested (Home Assistant bridge → health_metrics)
 * and already being correlated, but was displayed NOWHERE. Travel and illness were
 * visible only as analysis exclusions in Signals, which answers "how much data did
 * I lose" — not "how often am I away", which is a fact about the life, not the
 * dataset. Same source, deliberately different question; Signals keeps the
 * data-quality framing and this keeps the behavioural one.
 *
 * No new endpoint was needed: /raw-trend serves any health_metrics type, so these
 * ride the same path alcohol already uses.
 *
 * HISTORY IS SHORT — the HA bridge only started writing these in July 2026, so most
 * rows here are still accruing. The card states its own n rather than implying a
 * settled picture.
 */

const SPAN_DAYS = 90;

function iso(d) {
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
}

function Row({ label, value, sub, note }) {
  return (
    <div className="border-t border-rule py-1.5 first:border-t-0">
      <div className="flex items-baseline gap-2">
        <span className="min-w-0 flex-1 truncate text-[11px] text-ink-2">{label}</span>
        <span className="flex-none text-[11px] text-ink">{value}</span>
        {sub && <span className="flex-none text-[9px] text-ink-3">{sub}</span>}
      </div>
      {note && <p className="mt-0.5 text-[9px] leading-snug text-ink-3">{note}</p>}
    </div>
  );
}

export default function Context() {
  const { start, end } = useMemo(() => {
    const e = new Date(); const s = new Date(e);
    s.setDate(s.getDate() - (SPAN_DAYS - 1));
    return { start: iso(s), end: iso(e) };
  }, []);

  // Written out rather than generated from a helper: wrapping useQuery in a local
  // closure only works while every call stays unconditional and in a fixed order,
  // which is a trap for whoever edits this next.
  const S = { staleTime: 9e5 };
  const { data: wfh = [] }  = useQuery({ queryKey: ["ctx-wfh", start, end],
    queryFn: () => api.rawTrend("wfh_day", start, end, "sum"), ...S });
  const { data: away = [] } = useQuery({ queryKey: ["ctx-away", start, end],
    queryFn: () => api.rawTrend("slept_away", start, end, "sum"), ...S });
  const { data: tv = [] }   = useQuery({ queryKey: ["ctx-tv", start, end],
    queryFn: () => api.rawTrend("tv_prebed_min", start, end, "avg"), ...S });
  const { data: temp = [] } = useQuery({ queryKey: ["ctx-temp", start, end],
    queryFn: () => api.rawTrend("bedroom_temp", start, end, "avg"), ...S });
  const { data: hum = [] }  = useQuery({ queryKey: ["ctx-hum", start, end],
    queryFn: () => api.rawTrend("bedroom_humidity", start, end, "avg"), ...S });
  const { data: flags }         = useQuery({
    queryKey: ["day-flags", start, end], queryFn: () => api.getDayFlags(start, end), staleTime: 9e5 });
  const { data: corr }          = useQuery({
    queryKey: ["correlations", 180], queryFn: () => api.getCorrelations(180), staleTime: 18e5 });

  const results = corr?.results ?? corr ?? [];
  const ev = (m, as) => bestEvidence(results, { evidence: m, evidenceAs: as });

  const binary = (rows) => {
    const vals = (rows ?? []).filter((r) => r.value != null);
    return { n: vals.length, hit: vals.filter((r) => r.value > 0).length };
  };
  const cont = (rows) => {
    const vals = (rows ?? []).map((r) => r.value).filter((v) => v != null);
    if (!vals.length) return null;
    const mean = vals.reduce((a,b)=>a+b,0)/vals.length;
    return { n: vals.length, mean, lo: Math.min(...vals), hi: Math.max(...vals) };
  };

  const w = binary(wfh), a = binary(away);
  const t = cont(tv), tp = cont(temp), hm = cont(hum);

  const flagCounts = useMemo(() => {
    const m = {};
    for (const f of flags?.flags ?? []) {
      if (f.reason === "incomplete_data") continue;   // a nutrition gap, not a life event
      m[f.reason] = (m[f.reason] ?? 0) + 1;
    }
    return m;
  }, [flags]);

  const anything = w.n || a.n || t || tp || Object.keys(flagCounts).length;
  if (!anything) return null;

  return (
    <Card>
      <Eyebrow left="CONTEXT" right={`LAST ${SPAN_DAYS}D`} />
      <HeroSub>
        The conditions a day happened under rather than anything you chose to tick
        off. Collected since July 2026, so most of it is still accruing.
      </HeroSub>

      <div className="mt-3">
        {w.n > 0 && (
          <Row label="Worked from home" value={<K>{w.hit}</K>} sub={`of ${w.n} logged days`} />
        )}
        {a.n > 0 && (
          <Row label="Slept away from home" value={<K>{a.hit}</K>} sub={`of ${a.n} nights`} />
        )}
        {t && (
          <Row label="TV before bed" value={<K>{Math.round(t.mean)}</K>} sub={`min avg · n=${t.n}`} />
        )}
        {tp && (
          <Row label="Bedroom temperature" value={<K>{tp.mean.toFixed(1)}°</K>}
               sub={`${tp.lo.toFixed(0)}–${tp.hi.toFixed(0)} · n=${tp.n}`} />
        )}
        {hm && (
          <Row label="Bedroom humidity" value={<K>{Math.round(hm.mean)}%</K>}
               sub={`${Math.round(hm.lo)}–${Math.round(hm.hi)} · n=${hm.n}`} />
        )}
      </div>

      {Object.keys(flagCounts).length > 0 && (
        <div className="mt-3 border-t border-rule pt-3">
          <p className="text-[9px] tracking-micro text-ink-3">NONSTANDARD DAYS</p>
          <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1">
            {Object.entries(flagCounts).sort((x,y)=>y[1]-x[1]).map(([reason, n]) => (
              <span key={reason} className="text-[10px] text-ink-3">
                <b className="font-semibold text-ink-2">{n}</b> {reason.replace(/_/g," ")}
              </span>
            ))}
          </div>
          <p className="mt-1 text-[9px] leading-snug text-ink-3">
            Counted here as facts about the period. Signals reports the same days as
            analysis exclusions — how much data they cost — which is a different
            question about the same source.
          </p>
        </div>
      )}

      <Foot>
        Context is not something to improve. It is here because a week reads
        differently once you can see it contained four nights away.
      </Foot>
    </Card>
  );
}
