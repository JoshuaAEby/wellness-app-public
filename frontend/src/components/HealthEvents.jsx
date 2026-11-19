import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";
import { fmtDate } from "../utils.js";
import { Card, Eyebrow, HeroSub, Foot } from "./ui.jsx";

// Health events (note 93, 2026-10-31): vaccines, procedures, medication changes. A dated
// log, plus the days around an event against his own baseline. One event cannot size an
// effect, so the table states numbers and rivals and never a verdict. Bold marks "unusual
// for you", in either direction: not a good/bad call, hence no state colour.
const KINDS = [
  { key: "vaccine", label: "VACCINE" }, { key: "procedure", label: "PROCEDURE" },
  { key: "medication", label: "MEDICATION" }, { key: "other", label: "OTHER" },
];
const METRICS = [
  { key: "hrv", label: "HRV", d: 0 }, { key: "rhr", label: "RHR", d: 0 },
  { key: "breathing", label: "BREATH", d: 1 }, { key: "wrist_temp", label: "TEMP", d: 1 },
  { key: "energy", label: "ENERGY", d: 1 }, { key: "mood", label: "MOOD", d: 1 },
];
const offsetLabel = (o) => (o > 0 ? `+${o}` : `${o}`);

function EventForm({ onDone }) {
  const qc = useQueryClient();
  const [date, setDate] = useState(new Date().toLocaleDateString("en-CA"));
  const [kind, setKind] = useState("vaccine");
  const [name, setName] = useState("");
  const [notes, setNotes] = useState("");
  const save = useMutation({
    mutationFn: () => api.logHealthEvent({ date, kind, name, notes: notes || null }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["healthEvents"] }); onDone(); },
  });
  const field = "w-full rounded-md border border-rule bg-canvas px-2 py-1.5 text-[12px] text-ink";
  return (
    <div className="mt-3 space-y-2 border-t border-rule pt-3">
      <div className="grid grid-cols-2 gap-2">
        <input type="date" value={date} onChange={(e) => setDate(e.target.value)} className={field} />
        <select value={kind} onChange={(e) => setKind(e.target.value)} className={field}>
          {KINDS.map((k) => <option key={k.key} value={k.key}>{k.label}</option>)}
        </select>
      </div>
      <input value={name} onChange={(e) => setName(e.target.value)} maxLength={120}
        placeholder="Flu shot" className={field} />
      <input value={notes} onChange={(e) => setNotes(e.target.value)}
        placeholder="Notes (optional)" className={field} />
      {save.isError && <p className="text-[11px] text-over">{save.error?.message ?? "Could not save."}</p>}
      <div className="flex gap-2">
        <button onClick={onDone}
          className="flex-1 rounded-md border border-rule py-1.5 text-[11px] font-semibold tracking-micro text-ink-3">
          CANCEL
        </button>
        <button onClick={() => save.mutate()} disabled={!name.trim() || !date || save.isPending}
          className="flex-1 rounded-md border border-rule py-1.5 text-[11px] font-semibold tracking-micro text-ink-2 disabled:opacity-40">
          SAVE
        </button>
      </div>
    </div>
  );
}

function ResponseTable({ item }) {
  const { data } = useQuery({
    queryKey: ["healthEventResponse", item.key],
    queryFn: item.fetch,
  });
  if (!data) return null;
  const { rows } = data;
  const fmt = (v, d) => (v == null ? "—" : v.toFixed(d));
  const row = (label, cell) => (
    <tr key={label}>
      <td className="py-0.5 pr-1 text-[8.5px] tracking-micro text-ink-3">{label}</td>
      {rows.map((r) => cell(r))}
    </tr>
  );
  const plain = (text, r) => <td key={r.offset} className="py-0.5 text-right text-[11px] text-ink-3">{text}</td>;
  return (
    <div className="mt-2">
      <table className="w-full table-fixed">
        <thead>
          <tr>
            <th className="w-[58px]" />
            {rows.map((r) => (
              <th key={r.offset} className="text-right text-[8.5px] font-normal tracking-micro text-ink-3">
                {offsetLabel(r.offset)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {METRICS.map((m) => row(m.label, (r) => {
            const c = r[m.key];
            return (
              <td key={r.offset} title={c.z != null ? `z ${c.z}` : undefined}
                className={`py-0.5 text-right text-[11px] ${c.unusual ? "font-bold text-ink" : "text-ink-2"}`}>
                {fmt(c.value, m.d)}
              </td>
            );
          }))}
          {row("DRINKS", (r) => plain(r.drinks_day_before.toFixed(1), r))}
          {row("KCAL", (r) => plain(r.kcal_day_before ?? "—", r))}
        </tbody>
      </table>
      <p className="prose-ui mt-2 text-[10.5px] leading-snug text-ink-3">
        {item.sick
          ? <>Morning of each day: <b>0</b> is the first sick day, −3 to −1 the run-up.</>
          : <>Morning of each day, relative to {fmtDate(item.date)}: <b>+1</b> is the first night after.</>}{" "}
        Drinks and kcal are the day before each morning. Bold is {data.unusual_z}+ SD from the {data.baseline_days} days
        before the first column.
      </p>
    </div>
  );
}

export default function HealthEvents() {
  const qc = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [openId, setOpenId] = useState(null);
  const { data: events = [] } = useQuery({ queryKey: ["healthEvents"], queryFn: api.getHealthEvents });
  const del = useMutation({
    mutationFn: (id) => api.deleteHealthEvent(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["healthEvents"] }),
  });
  // Sick episodes come from the day flags: shown here, flagged and removed on Today.
  const { data: episodes = [] } = useQuery({ queryKey: ["sickEpisodes"], queryFn: api.getSickEpisodes });
  const items = [
    ...events.map((e) => ({
      key: `event-${e.id}`, date: e.date, name: e.name, kind: e.kind, notes: e.notes, event: e,
      fetch: () => api.getHealthEventResponse(e.id),
    })),
    ...episodes.map((s) => ({
      key: `sick-${s.start}`, date: s.start, name: "Sick", kind: "sick", sick: true,
      span: s.end === s.start ? null : `to ${fmtDate(s.end)} · ${s.days} days`,
      fetch: () => api.getSickResponse(s.start),
    })),
  ].sort((a, b) => (a.date < b.date ? 1 : -1));
  // Newest open by default: that is the one being watched.
  const shown = openId ?? items[0]?.key;

  return (
    <Card>
      <Eyebrow left="HEALTH EVENTS" right={items.length ? `${items.length} LOGGED` : "VACCINES · PROCEDURES"} />
      {items.length === 0 && !adding && (
        <HeroSub>Nothing logged. Add a vaccine, procedure or medication change, including past ones.</HeroSub>
      )}
      <div className="mt-1 divide-y divide-rule">
        {items.map((it) => (
          <div key={it.key} className="py-2">
            <div className="flex items-baseline gap-2">
              <button onClick={() => setOpenId(it.key)} className="min-w-0 flex-1 text-left">
                <span className="text-[12px] font-semibold text-ink">{it.name}</span>
                <span className="ml-2 text-[10.5px] text-ink-3">{fmtDate(it.date)}{it.span && ` ${it.span}`}</span>
              </button>
              <span className="text-[8.5px] tracking-micro text-ink-3">{it.kind.toUpperCase()}</span>
              {it.event && (
                <button onClick={() => { if (confirm(`Delete ${it.name}, ${fmtDate(it.date)}?`)) del.mutate(it.event.id); }}
                  className="text-ink-3" title="Delete">×</button>
              )}
            </div>
            {it.notes && <p className="prose-ui mt-0.5 text-[11px] leading-snug text-ink-2">{it.notes}</p>}
            {shown === it.key && <ResponseTable item={it} />}
          </div>
        ))}
      </div>
      {adding
        ? <EventForm onDone={() => setAdding(false)} />
        : <button onClick={() => setAdding(true)}
            className="mt-2 w-full rounded-md border border-rule py-1.5 text-[11px] font-semibold tracking-micro text-ink-2">
            + ADD EVENT
          </button>}
      <Foot>
        Logged events stay in your baselines and findings; sick days stay out. One case can't show cause:
        about 1 reading in 20 lands in bold by chance, and drinks or a big dinner can move the same numbers.
      </Foot>
    </Card>
  );
}
