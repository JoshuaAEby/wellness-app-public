import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, apiErrorText } from "../api.js";
import { removeQueued } from "../offline.js";
import { foodToday } from "../utils.js";
import { DRINK_KINDS, DRINK_KIND_BY_ID, isAlcoholic, sduOf } from "../drinks.js";
import CaffeineCard from "./CaffeineCard.jsx";
import DrinkParse from "./DrinkParse.jsx";
import AlcoholPresetsModal from "./AlcoholPresetsModal.jsx";
import WaterPresetsModal from "./WaterPresetsModal.jsx";
import { Card, Eyebrow, Foot, BrandTag } from "./ui.jsx";

/**
 * The three things you consume that have both a DOSE and a TIME: caffeine, alcohol,
 * water. One row, one tap each.
 *
 * Caffeine keeps its existing card verbatim, opened in a sheet — it already has
 * presets, custom doses, backfill and per-entry time editing, and its bedtime-residual
 * model is the reason per-instance logging exists at all. Rewriting it to match the
 * other two would risk that for cosmetics.
 *
 * Alcohol and water write instances into health_metrics (see services/intake): alcohol
 * because four years of history already lives there, water because it costs nothing to
 * make it available to /raw-trend and the correlation frame from day one.
 *
 * Every logger backfills. You will not always catch the moment, and a logger that only
 * accepts "now" quietly teaches you to skip entries.
 */

export function localToday() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
export function nowHHMM() {
  const d = new Date();
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
function hhmmLocal(iso) {
  const d = new Date(iso.endsWith("Z") ? iso : iso + "Z");   // stored naive UTC
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

const REF_DAYS = 30;
const MIN_REF_DAYS = 5;          // below this a "typical" line is just noise

function daysAgoStr(n) {
  const d = new Date(); d.setDate(d.getDate() - n);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/**
 * Mean over days that actually have an entry — NOT over the calendar.
 *
 * All three range endpoints omit days with nothing logged, and an absent day means
 * "not logged", which is not the same as a real zero. Dividing by 30 would quietly
 * halve the reference for anything logged intermittently, and would make water —
 * logged since today — show a typical day of about zero.
 */
function typicalOf(rows) {
  const vals = (rows ?? []).map((r) => r.value ?? 0).filter((v) => v > 0);
  if (vals.length < MIN_REF_DAYS) return null;
  return vals.reduce((a, b) => a + b, 0) / vals.length;
}

/**
 * Today against a typical logged day: the tick is the reference, the fill is today.
 * The scale keeps headroom above the reference so "well over" looks different from
 * "just over", instead of both pinning the bar full.
 */
function MiniBar({ value, typical, color, refLabel = "typical" }) {
  if (!typical) {
    return <div className="mt-1.5 h-[4px] rounded-[2px] bg-canvas" title="not enough history yet" />;
  }
  const scale = Math.max(value, typical * 1.6, 1);
  return (
    <>
      <div className="relative mt-1.5 h-[4px] overflow-hidden rounded-[2px] bg-canvas">
        <div className="absolute inset-y-0 left-0 rounded-[2px]"
             style={{ width: `${Math.min(100, (value / scale) * 100)}%`, background: color, opacity: 0.75 }} />
        <div className="absolute inset-y-0 w-px bg-ink-2"
             style={{ left: `${(typical / scale) * 100}%` }} />
      </div>
      <div className="mt-0.5 text-[8px] leading-none text-ink-3">
        {refLabel} {typical >= 100 || refLabel === "target" ? Math.round(typical) : typical.toFixed(1)}
      </div>
    </>
  );
}

export function Sheet({ title, onClose, children }) {
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/60 pb-16" onClick={onClose}>
      <div className="max-h-[85vh] w-full max-w-md overflow-y-auto rounded-t-2xl bg-card p-5 pb-10"
           onClick={(e) => e.stopPropagation()}>
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-[15px] font-semibold text-ink">{title}</h2>
          <button onClick={onClose} className="text-[11px] text-ink-3 hover:text-ink">Close</button>
        </div>
        {children}
      </div>
    </div>
  );
}

/** Shared date+time row — the backfill control for every logger. Without `setDate` the
 *  date is fixed by the LOG view's single date picker (2026-10-21) and only shown. */
export function WhenRow({ date, setDate, time, setTime }) {
  return (
    <div className="mb-3 flex gap-2">
      {setDate ? (
        <input type="date" value={date} max={localToday()} onChange={(e) => setDate(e.target.value)}
               className="min-w-0 flex-1 rounded-lg bg-surface px-3 py-2 text-[13px] text-ink outline-none" />
      ) : (
        <span className="flex min-w-0 flex-1 items-center rounded-lg px-1 text-[12px] text-ink-2">
          {date === localToday() ? "Today" : new Date(`${date}T12:00:00`).toLocaleDateString(
            "en-US", { weekday: "short", month: "short", day: "numeric" })}
          <span className="ml-1 text-[10px] text-ink-3">(change at the top of LOG)</span>
        </span>
      )}
      <input type="time" value={time} onChange={(e) => setTime(e.target.value)} aria-label="Time"
             className="w-[7.5rem] flex-none rounded-lg bg-surface px-3 py-2 text-[13px] text-ink outline-none" />
    </div>
  );
}

/**
 * Logged entries, editable in place. You will mistype a time or tap the wrong preset,
 * and an entry you cannot correct is one you delete and re-enter — which loses the
 * original timestamp.
 *
 * Apple-sourced rows are shown but neither editable nor deletable: this logger owns
 * only what it wrote, and quietly rewriting four years of imported history is not
 * something a small inline control should be able to do.
 */
export function EntryList({ entries, unit, onDelete, onPatch, fmt = (v) => v, amountLabel = "amount" }) {
  const [editing, setEditing] = useState(null);
  const [time, setTime] = useState("");
  const [amount, setAmount] = useState("");

  if (!entries?.length) return <p className="mt-3 text-[11px] text-ink-3">Nothing logged yet.</p>;

  const open = (e) => {
    setEditing(e.id);
    setTime(hhmmLocal(e.timestamp));
    setAmount(String(e.value));
  };
  const commit = (id) => {
    onPatch(id, { at_time: time, amount: amount === "" ? null : parseFloat(amount) });
    setEditing(null);
  };

  return (
    <div className="mt-3 border-t border-rule pt-2">
      {entries.map((e) => {
        const mine = e.source === "wellness_app";
        return (
          <div key={e.id} className="border-b border-rule py-1.5 last:border-b-0">
            <div className="flex items-baseline gap-2">
              <span className="text-[11px] tabular-nums text-ink-2">{hhmmLocal(e.timestamp)}</span>
              <span className="flex-1 text-[11px] text-ink">{fmt(e.value)} {unit}</span>
              {mine ? (
                <>
                  <button onClick={() => (editing === e.id ? setEditing(null) : open(e))}
                          className="text-[10px] text-ink-3 hover:text-ink">
                    {editing === e.id ? "cancel" : "edit"}
                  </button>
                  <button onClick={() => onDelete(e.id)}
                          className="text-[10px] text-ink-3 hover:text-[color:var(--over)]">delete</button>
                </>
              ) : (
                <span className="text-[9px] text-ink-3">{e.source === "apple_health" ? "from Apple" : e.source}</span>
              )}
            </div>
            {editing === e.id && (
              <div className="mt-1.5 flex items-center gap-2">
                <input type="time" value={time} onChange={(ev) => setTime(ev.target.value)}
                       aria-label="Time"
                       className="w-[7rem] rounded-lg bg-surface px-2 py-1 text-[12px] text-ink outline-none" />
                <input inputMode="decimal" value={amount} onChange={(ev) => setAmount(ev.target.value)}
                       aria-label={amountLabel}
                       className="w-20 rounded-lg bg-surface px-2 py-1 text-[12px] text-ink outline-none" />
                <span className="text-[10px] text-ink-3">{unit}</span>
                <button onClick={() => commit(e.id)}
                        className="ml-auto rounded-lg bg-surface px-3 py-1 text-[12px] text-ink">Save</button>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// The old one-kind sheets stay as thin wrappers (unused since the Drinks sheet,
// 2026-11-11) until the Drinks sheet has had a week of real use; then they go.
function AlcoholSheet({ onClose, date }) {
  const [time, setTime] = useState(nowHHMM());
  return (
    <Sheet title="Log alcohol" onClose={onClose}>
      <WhenRow date={date} time={time} setTime={setTime} />
      <AlcoholPanel date={date} time={time} />
    </Sheet>
  );
}

function AlcoholPanel({ date, time }) {
  const qc = useQueryClient();
  const [oz, setOz] = useState("");
  const [abv, setAbv] = useState("");
  const [kcal, setKcal] = useState("");
  const [editPresets, setEditPresets] = useState(false);

  const { data: presetData } = useQuery({ queryKey: ["alcohol-presets"], queryFn: api.getAlcoholPresets });
  const { data: day } = useQuery({ queryKey: ["alcohol-day", date], queryFn: () => api.getAlcoholDay(date) });
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["alcohol-day"] });
    qc.invalidateQueries({ queryKey: ["alcohol-trend"] });
    qc.invalidateQueries({ queryKey: ["today"] });
    // Every drink also writes a Drinks row, and a library drink bumps its use count.
    qc.invalidateQueries({ queryKey: ["nutrition-logs"] });
    qc.invalidateQueries({ queryKey: ["my-foods"] });
  };
  const log = useMutation({ mutationFn: api.logAlcohol, onSuccess: invalidate });

  // MY DRINKS (note 34): named drinks from the library, one tap each, searchable and
  // split by kind. Only alcoholic ones — oz x ABV is what makes them loggable here.
  const { data: myFoods = [] } = useQuery({ queryKey: ["my-foods"], queryFn: () => api.listMyFoods() });
  const myDrinks = myFoods.filter(isAlcoholic);
  const [dq, setDq] = useState("");
  const [dKind, setDKind] = useState(null);
  const [drinkName, setDrinkName] = useState("");
  const [drinkBrand, setDrinkBrand] = useState("");   // optional (note 98)
  const kindsPresent = DRINK_KINDS.filter((k) => myDrinks.some((f) => f.drink_kind === k.id));
  const needle = dq.trim().toLowerCase();
  const shownDrinks = myDrinks
    .filter((f) => (!dKind || f.drink_kind === dKind)
      && (!needle || f.name.toLowerCase().includes(needle) || (f.brand || "").toLowerCase().includes(needle)
          || (f.aliases || []).some((a) => a.includes(needle))))
    .sort((a, b) => (b.uses ?? 0) - (a.uses ?? 0) || a.name.localeCompare(b.name));
  // "Log + save": the custom drink becomes a My Food, then is logged AS that food, so the
  // first pour is already linked to the library entry.
  const saveDrink = useMutation({
    mutationFn: (body) => api.createMyFood(body),
    onSuccess: (f) => {
      qc.invalidateQueries({ queryKey: ["my-foods"] });
      log.mutate({ date, at_time: time, my_food_id: f.id });
      setOz(""); setAbv(""); setKcal(""); setDrinkName(""); setDrinkBrand("");
    },
  });
  const del = useMutation({ mutationFn: api.deleteAlcohol, onSuccess: invalidate });
  const patch = useMutation({
    mutationFn: ({ id, body }) => api.patchAlcohol(id, body), onSuccess: invalidate });

  const presets = presetData?.presets ?? [];
  // Categories and constants come from the backend so the carb densities have one home;
  // the arithmetic runs locally so the preview updates per keystroke without a request
  // per keystroke. Mirrors services/intake.estimate_alcohol_nutrition by using ITS
  // constants rather than copies of them.
  const { data: catData } = useQuery({
    queryKey: ["alcohol-categories"], queryFn: api.getAlcoholCategories,
    staleTime: 1000 * 60 * 60,
  });
  const categories = catData?.categories ?? [];
  const [category, setCategory] = useState(null);
  const activeCat = category ?? catData?.default ?? null;
  const cat = categories.find((c) => c.id === activeCat);

  const nOz = parseFloat(oz), nAbv = parseFloat(abv);
  const valid = Number.isFinite(nOz) && Number.isFinite(nAbv) && nOz > 0 && nAbv > 0;
  const customSdu = valid
    ? (nOz * (nAbv / 100)) / (catData?.ethanol_oz_per_sdu ?? 0.6)
    : null;
  // SDU is exact arithmetic. Only the residual carbohydrate is a category guess, so this
  // is near-exact for spirits and widest for beer.
  const estKcal = valid && catData && cat
    ? Math.round(catData.kcal_per_g_ethanol * (nOz * nAbv * catData.ethanol_g_per_oz_pct)
                 + 4 * nOz * cat.carbs_g_per_oz + 4 * nOz * cat.protein_g_per_oz)
    : null;

  return (
    <>
      {myDrinks.length > 0 && (
        <div className="mb-3">
          <div className="mb-1.5 flex items-center gap-2">
            <p className="flex-none text-[9px] tracking-micro text-ink-3">MY DRINKS</p>
            <input value={dq} onChange={(e) => setDq(e.target.value)} placeholder="Search…"
                   className="min-w-0 flex-1 rounded-md bg-surface px-2 py-1 text-[12px] text-ink outline-none" />
          </div>
          {kindsPresent.length > 1 && (
            <div className="mb-1.5 flex flex-wrap gap-1">
              {[{ id: null, label: "All", emoji: "" }, ...kindsPresent].map((k) => (
                <button key={k.id ?? "all"} onClick={() => setDKind(k.id)}
                        className="rounded-full px-2 py-0.5 text-[10.5px]"
                        style={dKind === k.id
                          ? { background: "color-mix(in srgb, var(--over) 20%, transparent)", color: "var(--over)" }
                          : { background: "var(--surface)", color: "var(--ink-2)" }}>
                  {k.emoji} {k.label}
                </button>
              ))}
            </div>
          )}
          <div className="max-h-48 space-y-1 overflow-y-auto">
            {shownDrinks.map((f) => (
              <button key={f.id} disabled={log.isPending}
                      onClick={() => log.mutate({ date, at_time: time, my_food_id: f.id })}
                      className="flex w-full items-baseline gap-2 rounded-lg bg-surface px-2.5 py-2 text-left disabled:opacity-50">
                <span className="flex-none text-[13px]">{DRINK_KIND_BY_ID[f.drink_kind]?.emoji ?? "🍺"}</span>
                <span className="min-w-0 flex-1 truncate text-[12px] text-ink">{f.name}<BrandTag brand={f.brand} /></span>
                <span className="flex-none text-[10px] tabular-nums text-ink-3">
                  {f.serving_qty} oz · {f.abv}% · {sduOf(f.serving_qty, f.abv, catData?.ethanol_oz_per_sdu).toFixed(2)} SDU
                  {f.calories ? ` · ${Math.round(f.calories)} kcal` : ""}
                </span>
              </button>
            ))}
            {!shownDrinks.length && <p className="text-[11px] text-ink-3">No drink matches.</p>}
          </div>
        </div>
      )}
      <div className="mb-1.5 flex justify-end">
        <button onClick={() => setEditPresets(true)} className="text-[10px] text-ink-3 hover:text-ink">
          ⚙ edit presets
        </button>
      </div>
      <div className="grid grid-cols-3 gap-2">
        {presets.map((p) => (
          <button key={p.id} disabled={log.isPending}
                  onClick={() => log.mutate({ date, at_time: time, kind: p.id })}
                  className="rounded-lg bg-surface px-2 py-2.5 text-center disabled:opacity-50">
            <div className="text-[15px]">{p.emoji}</div>
            <div className="mt-0.5 text-[10.5px] text-ink">{p.label}</div>
            <div className="text-[9px] text-ink-3">
              {p.sdu} SDU{p.kcal ? ` · ${p.kcal} kcal` : ""}
            </div>
          </button>
        ))}
      </div>

      <div className="mt-3 border-t border-rule pt-3">
        <p className="text-[9px] tracking-micro text-ink-3">CUSTOM</p>
        {categories.length > 0 && (
          <>
            <div className="mt-1.5 flex flex-wrap gap-1">
              {categories.map((c) => (
                <button key={c.id} onClick={() => setCategory(c.id)}
                        className="rounded-full px-2 py-1 text-[10.5px]"
                        style={activeCat === c.id
                          ? { background: "color-mix(in srgb, var(--over) 20%, transparent)",
                              color: "var(--over)" }
                          : { background: "var(--surface)", color: "var(--ink-2)" }}>
                  {c.emoji} {c.label}
                </button>
              ))}
            </div>
            {cat?.note && (
              <p className="mt-1 text-[9.5px] leading-snug text-ink-3">{cat.note}</p>
            )}
          </>
        )}
        <div className="mt-1.5 flex items-center gap-2">
          <input inputMode="decimal" value={oz} onChange={(e) => setOz(e.target.value)} placeholder="oz"
                 className="w-16 rounded-lg bg-surface px-2 py-1.5 text-[13px] text-ink outline-none" />
          <input inputMode="decimal" value={abv} onChange={(e) => setAbv(e.target.value)} placeholder="% ABV"
                 className="w-20 rounded-lg bg-surface px-2 py-1.5 text-[13px] text-ink outline-none" />
          <span className="flex-1 text-[11px] text-ink-3">
            {customSdu
              ? <>= <b className="text-ink-2">{customSdu.toFixed(2)}</b> SDU
                  {estKcal != null && <> · ≈{estKcal} kcal</>}</>
              : "oz × ABV ÷ 0.6"}
          </span>
          <input inputMode="numeric" value={kcal} onChange={(e) => setKcal(e.target.value)}
                 placeholder={estKcal != null ? String(estKcal) : "kcal"}
                 className="w-16 rounded-lg bg-surface px-2 py-1.5 text-[13px] text-ink outline-none" />
          <button disabled={!customSdu || log.isPending}
                  onClick={() => { log.mutate({ date, at_time: time, oz: parseFloat(oz), abv: parseFloat(abv),
                                                category: activeCat, brand: drinkBrand.trim() || null,
                                                kcal: kcal ? parseFloat(kcal) : null });
                                   setOz(""); setAbv(""); setKcal(""); setDrinkBrand(""); }}
                  className="rounded-lg bg-surface px-3 py-1.5 text-[12px] text-ink disabled:opacity-40">Log</button>
        </div>
        {/* Name it to keep it (note 34): saved to My Foods as a drink of the chosen
            kind, then logged as that drink. */}
        <div className="mt-1.5 flex items-center gap-2">
          <input value={drinkName} onChange={(e) => setDrinkName(e.target.value)}
                 placeholder="Name (to save)"
                 className="min-w-0 flex-1 rounded-lg bg-surface px-2 py-1.5 text-[13px] text-ink outline-none" />
          <input value={drinkBrand} onChange={(e) => setDrinkBrand(e.target.value)} placeholder="Brand" maxLength={40}
                 className="w-24 flex-none rounded-lg bg-surface px-2 py-1.5 text-[13px] text-ink outline-none" />
          <button
            disabled={!customSdu || !drinkName.trim() || (!kcal && estKcal == null) || saveDrink.isPending || log.isPending}
            onClick={() => saveDrink.mutate({
              name: drinkName.trim(), meal_type: "drinks", drink_kind: activeCat,
              brand: drinkBrand.trim() || null,
              abv: nAbv, serving_qty: nOz, serving_unit: "oz", serving_label: `${nOz} oz`,
              calories: kcal ? parseFloat(kcal) : estKcal,
              carbs_g: cat ? +(nOz * cat.carbs_g_per_oz).toFixed(1) : null,
              protein_g: cat ? +(nOz * cat.protein_g_per_oz).toFixed(1) : null,
              source: kcal ? "confirmed" : "estimate",
            })}
            className="flex-none rounded-lg bg-surface px-3 py-1.5 text-[12px] text-ink disabled:opacity-40">
            Log + save
          </button>
        </div>
        {saveDrink.isError && (
          <p className="mt-1 text-[10.5px] text-rose-400">{apiErrorText(saveDrink.error)}</p>
        )}
      </div>

      <p className="mt-3 text-[11px] text-ink-2">
        {date === localToday() ? "Today" : date}: <b>{(day?.total ?? 0).toFixed(2)}</b> SDU
      </p>
      <EntryList entries={day?.entries} unit="SDU" onDelete={(id) => del.mutate(id)}
                 onPatch={(id, { at_time, amount }) =>
                   patch.mutate({ id, body: { at_time, sdu: amount } })}
                 fmt={(v) => v.toFixed(2)} amountLabel="SDU" />
      {editPresets && (
        <AlcoholPresetsModal presets={presets} onClose={() => setEditPresets(false)} />
      )}
      <Foot>
        One standard drink = 0.6 fl oz of pure ethanol, the unit Apple Health records
        alcohol in. Anything with calories also lands in Nutrition as a
        "drinks" entry, so it only gets logged once.
      </Foot>
    </>
  );
}

function WaterSheet({ onClose, date }) {
  const [time, setTime] = useState(nowHHMM());
  return (
    <Sheet title="Log water" onClose={onClose}>
      <WhenRow date={date} time={time} setTime={setTime} />
      <WaterPanel date={date} time={time} />
    </Sheet>
  );
}

function WaterPanel({ date, time }) {
  const qc = useQueryClient();
  const [oz, setOz] = useState("");
  const [editPresets, setEditPresets] = useState(false);

  const { data: day } = useQuery({ queryKey: ["water-day", date], queryFn: () => api.getWaterDay(date) });
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["water-day"] });
    qc.invalidateQueries({ queryKey: ["water-trend"] });
    qc.invalidateQueries({ queryKey: ["today"] });
  };
  const log = useMutation({ mutationFn: api.logWater, onSuccess: invalidate });
  const del = useMutation({ mutationFn: api.deleteWater, onSuccess: invalidate });
  const patch = useMutation({
    mutationFn: ({ id, body }) => api.patchWater(id, body), onSuccess: invalidate });

  // Presets ride along on the day payload so the sheet needs no second request.
  const presets = day?.presets ?? [];

  return (
    <>
      <div className="mb-1.5 flex justify-end">
        <button onClick={() => setEditPresets(true)} className="text-[10px] text-ink-3 hover:text-ink">
          ⚙ edit presets
        </button>
      </div>
      <div className="grid grid-cols-3 gap-2">
        {presets.map((p) => (
          <button key={p.id} disabled={log.isPending}
                  onClick={() => log.mutate({ date, at_time: time, kind: p.id })}
                  className="rounded-lg bg-surface px-2 py-2.5 text-center disabled:opacity-50">
            <div className="text-[15px]">{p.emoji || "💧"}</div>
            <div className="mt-0.5 text-[10.5px] text-ink">{p.label}</div>
            <div className="text-[9px] text-ink-3">{p.oz} oz</div>
          </button>
        ))}
      </div>

      <div className="mt-3 border-t border-rule pt-3">
        <p className="text-[9px] tracking-micro text-ink-3">CUSTOM</p>
        <div className="mt-1.5 flex items-center gap-2">
          <input inputMode="decimal" value={oz} onChange={(e) => setOz(e.target.value)} placeholder="oz"
                 className="w-20 rounded-lg bg-surface px-2 py-1.5 text-[13px] text-ink outline-none" />
          <button disabled={!oz || log.isPending}
                  onClick={() => { log.mutate({ date, at_time: time, oz: parseFloat(oz) }); setOz(""); }}
                  className="rounded-lg bg-surface px-3 py-1.5 text-[12px] text-ink disabled:opacity-40">Log</button>
        </div>
      </div>

      <p className="mt-3 text-[11px] text-ink-2">
        {date === localToday() ? "Today" : date}: <b>{Math.round(day?.total ?? 0)}</b> oz water
      </p>
      <FluidsLine f={day?.fluids} />
      <EntryList entries={day?.entries} unit="oz" onDelete={(id) => del.mutate(id)}
                 onPatch={(id, { at_time, amount }) => patch.mutate({ id, body: { at_time, oz: amount } })}
                 fmt={(v) => Math.round(v)} amountLabel="oz" />

      {editPresets && (
        <WaterPresetsModal presets={presets} onClose={() => setEditPresets(false)} />
      )}
      <Foot>
        A refill records when the bottle was FILLED, not drunk — good for daily totals,
        not for anything time-sensitive like fluid before bed.
      </Foot>
    </>
  );
}

/**
 * Total fluids from every drink against the day's target (note 78). Water alone is
 * the wrong quantity — coffee hydrates about as well as water — so the target is for
 * ALL drinks: water + caffeine presets' volume + liquid food-log drinks. Alcohol is
 * left out (net-neutral to net-negative). See services/fluids for the target rule.
 */
function FluidsLine({ f }) {
  if (!f) return null;
  const left = Math.max(0, f.target - f.total);
  return (
    <div className="mt-1.5 rounded-lg bg-surface px-3 py-2 text-[11px] text-ink-2">
      <div>
        Fluids <b className="text-ink">{Math.round(f.total)}</b> of <b className="text-ink">{f.target}</b> oz
        {left > 0 ? <span className="text-ink-3"> · {Math.round(left)} to go</span>
                  : <span className="text-ink-3"> · target met</span>}
      </div>
      <div className="mt-0.5 text-[10px] text-ink-3">
        water {Math.round(f.water)} · coffee/caffeine {Math.round(f.caffeine)} · other {Math.round(f.other)}
        {f.unknown_n > 0 && ` · ${f.unknown_n} drink${f.unknown_n > 1 ? "s" : ""} of unknown size`}
      </div>
      <div className="mt-0.5 text-[10px] text-ink-3">
        target = {f.base} oz{!f.flat && " for your weight"}
        {f.training_min > 0 && ` + ${f.target - f.base} for ${f.training_min} min training`}
        {f.alcohol_sdu > 0 && ` + ${f.target - f.base} for ${f.alcohol_sdu} SDU wine or spirits`}
        {" "}· a sanity check — thirst and urine colour beat any formula
      </div>
    </div>
  );
}

/**
 * ONE drinks sheet (2026-11-11): caffeine, alcohol and water open the same sheet,
 * with the kind you tapped expanded first, then "describe any drink" (AI parse, editable
 * macros and doses), then the other two kinds folded. One time row serves everything in it,
 * so each drink keeps its own time (a meal's single timestamp would not suit four drinks
 * over an evening). The kinds' panels are the old sheets' contents, unchanged: presets,
 * My Drinks, custom doses, the no-caffeine answer, fluids and each day's entries.
 */
const DRINK_SECTIONS = [
  { id: "caffeine", emoji: "☕", label: "CAFFEINE" },
  { id: "alcohol", emoji: "🍺", label: "ALCOHOL" },
  { id: "water", emoji: "💧", label: "WATER" },
];

function DrinksSheet({ focus, date, totals, onClose }) {
  const [time, setTime] = useState(nowHHMM());
  const [open, setOpen] = useState(() => new Set([focus]));
  const toggle = (id) => setOpen((prev) => {
    const next = new Set(prev);
    next.has(id) ? next.delete(id) : next.add(id);
    return next;
  });
  const section = (id) => {
    const k = DRINK_SECTIONS.find((x) => x.id === id);
    const isOpen = open.has(id);
    return (
      <div key={id} className="border-t border-rule py-2">
        <button onClick={() => toggle(id)} className="flex w-full items-center justify-between py-1">
          <span className="text-[10px] font-semibold tracking-micro text-ink-2">
            <span className="mr-1 text-[12px] tracking-normal">{k.emoji}</span>{k.label}
          </span>
          <span className="text-[10.5px] tabular-nums text-ink-3">{totals[id]} {isOpen ? "▾" : "▸"}</span>
        </button>
        {isOpen && (
          <div className="mt-2">
            {id === "caffeine" ? <CaffeineCard date={date} time={time} />
              : id === "alcohol" ? <AlcoholPanel date={date} time={time} />
              : <WaterPanel date={date} time={time} />}
          </div>
        )}
      </div>
    );
  };
  const rest = DRINK_SECTIONS.map((k) => k.id).filter((id) => id !== focus);
  return (
    <Sheet title="Drinks" onClose={onClose}>
      <WhenRow date={date} time={time} setTime={setTime} />
      {section(focus)}
      <div className="border-t border-rule py-3">
        <p className="mb-1.5 text-[9px] tracking-micro text-ink-3">ANY OTHER DRINK</p>
        <DrinkParse date={date} time={time} />
      </div>
      {rest.map(section)}
    </Sheet>
  );
}

export default function IntakeTrio({ date: dateProp }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(null);   // "caffeine" | "alcohol" | "water"
  const today = localToday();
  // The LOG view's single date (2026-10-21); totals and every sheet follow it.
  const day = dateProp || today;

  const { data: caf } = useQuery({ queryKey: ["caffeine-day", day], queryFn: () => api.getCaffeineDay(day) });
  const { data: alc } = useQuery({ queryKey: ["alcohol-day", day], queryFn: () => api.getAlcoholDay(day) });
  const { data: wat } = useQuery({ queryKey: ["water-day", day], queryFn: () => api.getWaterDay(day) });

  // Trailing history behind the "typical" tick on each bar.
  const refStart = daysAgoStr(REF_DAYS);
  const { data: cafHist } = useQuery({
    queryKey: ["caffeine-trend", refStart, today], queryFn: () => api.getCaffeineRange(refStart, today), staleTime: 6e5 });
  const { data: alcHist } = useQuery({
    queryKey: ["alcohol-trend", refStart, today], queryFn: () => api.rawTrend("alcohol", refStart, today, "sum"), staleTime: 6e5 });
  const { data: watHist } = useQuery({
    queryKey: ["water-trend", refStart, today], queryFn: () => api.rawTrend("water", refStart, today, "sum"), staleTime: 6e5 });

  // Compact (note 83, 2026-10-26): emoji beside the label instead of above it, less
  // padding — about 40% shorter, which is the room the one-tap chips below use.
  const Btn = ({ id, emoji, label, value, unit, raw, typical, color, refLabel }) => (
    <button onClick={() => setOpen(id)}
            className="w-full flex-1 rounded-xl bg-surface px-2 py-2 text-center transition-colors hover:bg-rule">
      <div className="text-[9px] tracking-micro text-ink-3">
        <span className="mr-1 text-[12px] tracking-normal">{emoji}</span>{label}
      </div>
      <div className="mt-0.5 text-[14px] font-semibold leading-none text-ink">
        {value}<span className="text-[9px] font-normal text-ink-3">{unit}</span>
      </div>
      <MiniBar value={raw} typical={typical} color={color} refLabel={refLabel} />
    </button>
  );

  // One-tap chips (note 81): your most frequent entry per kind over 30 days
  // (GET /api/intake/usual), replayed through the normal logger, with Undo. Today
  // only — a backfill needs a time, which the sheet asks for.
  const { data: usual } = useQuery({ queryKey: ["usual-intake"], queryFn: () => api.getUsualIntake(30),
                                     staleTime: 10 * 60 * 1000 });
  const [busy, setBusy] = useState(null);
  const [undo, setUndo] = useState(null);      // { label, kind, id } | { label, queuedKey }
  const [err, setErr] = useState(null);
  useEffect(() => {
    if (!undo) return;
    const t = setTimeout(() => setUndo(null), 6000);
    return () => clearTimeout(t);
  }, [undo]);
  const refreshIntake = () => {
    for (const k of ["caffeine-day", "caffeine-trend", "caffeine-incomplete", "alcohol-day", "alcohol-trend",
                     "water-day", "water-trend", "usual-intake", "nutrition-logs", "nutrition-daily", "today"])
      qc.invalidateQueries({ queryKey: [k] });
  };
  const QUICK = {
    caffeine: { log: (u) => api.logCaffeine({ date: day, kind: u.kind }), del: api.deleteCaffeine,
                text: (u) => u.label },
    water:    { log: (u) => api.logWater(u.kind ? { date: day, kind: u.kind } : { date: day, oz: u.oz }),
                del: api.deleteWater, text: (u) => (u.kind ? `${u.label} ${u.oz}oz` : u.label) },
    alcohol:  { log: (u) => api.logAlcohol({ date: day, kind: u.kind }), del: api.deleteAlcohol,
                text: (u) => u.label },
  };
  const quickLog = async (kind) => {
    const u = usual?.[kind];
    setBusy(kind); setErr(null);
    try {
      const r = await QUICK[kind].log(u);
      const label = QUICK[kind].text(u);
      setUndo(r?.queued ? { label: `${label} (offline — syncs later)`, kind, queuedKey: r.key }
                        : { label, kind, id: r?.id });
      refreshIntake();
    } catch (e) { setErr(apiErrorText(e)); } finally { setBusy(null); }
  };
  const doUndo = async () => {
    const u = undo; setUndo(null);
    if (u.queuedKey) { removeQueued(u.queuedKey); return; }
    if (u.id != null) { try { await QUICK[u.kind].del(u.id); } catch { /* already gone */ } }
    refreshIntake();
  };
  const isToday = day === foodToday();
  // The tapped chip BECOMES its Undo for 6 s (no separate bar): a bar appearing and
  // vanishing moved everything below it, and in the harness a tap meant for a meal
  // tab landed on a food chip as the content jumped back up.
  const Chip = ({ kind }) => {
    const u = usual?.[kind];
    if (!isToday || !u) return <div className="h-[22px]" />;
    if (undo?.kind === kind) return (
      <button type="button" onClick={doUndo} title={`Logged ${undo.label} — tap to undo`}
              className="mt-1 h-[22px] w-full truncate rounded-lg bg-surface px-1.5 text-[10px] font-semibold text-ink">
        ✓ Undo
      </button>
    );
    return (
      <button type="button" disabled={busy != null} onClick={() => quickLog(kind)}
              title={`Your most frequent (${u.times}× in ${usual.days} days) — tap to log now`}
              className="mt-1 h-[22px] w-full truncate rounded-lg border border-rule px-1.5 text-[10px] text-ink-2 hover:bg-surface disabled:opacity-50">
        {busy === kind ? "Logging…" : `+ ${QUICK[kind].text(u)}`}
      </button>
    );
  };

  return (
    <Card>
      <Eyebrow left="INTAKE" right="TAP TO LOG" />
      <div className="mt-2 flex gap-2">
        <div className="flex min-w-0 flex-1 flex-col">
        <Btn id="caffeine" emoji="☕" label="CAFFEINE" value={Math.round(caf?.total_mg ?? 0)} unit="mg"
             raw={caf?.total_mg ?? 0} typical={typicalOf(cafHist)} color="var(--breakfast, #f59e0b)" />
        <Chip kind="caffeine" />
        </div>
        <div className="flex min-w-0 flex-1 flex-col">
        <Btn id="alcohol"  emoji="🍺" label="ALCOHOL"  value={(alc?.total ?? 0).toFixed(1)} unit=" SDU"
             raw={alc?.total ?? 0} typical={typicalOf(alcHist)} color="var(--over)" />
        <Chip kind="alcohol" />
        </div>
        <div className="flex min-w-0 flex-1 flex-col">
        {/* FLUIDS, not water (note 78): every drink counts, and the tick is today's
            training-adjusted target rather than a typical day. */}
        <Btn id="water"    emoji="💧" label="FLUIDS"
             value={Math.round(wat?.fluids?.total ?? wat?.total ?? 0)} unit="oz"
             raw={wat?.fluids?.total ?? wat?.total ?? 0}
             typical={wat?.fluids?.target ?? typicalOf(watHist)}
             refLabel={wat?.fluids ? "target" : "typical"} color="var(--hrv)" />
        <Chip kind="water" />
        </div>
      </div>
      {err && <p className="mt-1 text-[11px]" style={{ color: "var(--over)" }}>{err}</p>}

      {open && (
        <DrinksSheet focus={open} date={day} onClose={() => setOpen(null)}
          totals={{ caffeine: `${Math.round(caf?.total_mg ?? 0)} mg`,
                    alcohol: `${(alc?.total ?? 0).toFixed(1)} SDU`,
                    water: `${Math.round(wat?.fluids?.total ?? wat?.total ?? 0)} oz fluids` }} />
      )}
    </Card>
  );
}
