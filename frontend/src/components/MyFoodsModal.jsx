import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, apiErrorText } from "../api.js";
import { SegmentedTabs, BrandTag } from "./ui.jsx";
import { DRINK_KINDS, DRINK_KIND_BY_ID, guessDrink, isAlcoholic, isDrink, sduOf } from "../drinks.js";
import { shrinkImage } from "../imageUtil.js";

// My Foods — seeding review + library (docs/MY_FOODS.md, phase 3, 2026-09-13).
//
// REVIEW: repeat foods from the log, grouped by the meal they're usually logged in
// (user: organised by meal type, top 15 per group). Each can be CREATED as a My Food
// (numbers checked once), SPLIT into base + topping (a combo that logs as one row per
// part), or SKIPPED (stays gone). After creating, the matching past entries can be
// rewritten to the new numbers — previewed first, and undoable.

const MACROS = [
  ["calories", "kcal"], ["protein_g", "protein"], ["carbs_g", "carbs"], ["fat_g", "fat"],
  ["fiber_g", "fiber"], ["sugar_g", "sugar"], ["sodium_mg", "Na mg"],
];
const MEALS = ["breakfast", "lunch", "dinner", "dessert", "snack", "drinks"];
// Typical label values for common spreads — only a starting point for a split, shown
// as such. Check your own jar.
const TOPPING_HINTS = {
  "cream cheese":  { serving_label: "2 tbsp", serving_qty: 2, serving_unit: "tbsp", calories: 100, protein_g: 2, carbs_g: 1, fat_g: 10, fiber_g: 0, sugar_g: 1, sodium_mg: 90 },
  "peanut butter": { serving_label: "2 tbsp", serving_qty: 2, serving_unit: "tbsp", calories: 190, protein_g: 7, carbs_g: 8, fat_g: 16, fiber_g: 2, sugar_g: 3, sodium_mg: 140 },
  "butter":        { serving_label: "1 tbsp", serving_qty: 1, serving_unit: "tbsp", calories: 100, protein_g: 0, carbs_g: 0, fat_g: 11, fiber_g: 0, sugar_g: 0, sodium_mg: 90 },
};
const num = (v) => (v === "" || v == null || Number.isNaN(parseFloat(v)) ? null : parseFloat(v));
const s = (v) => (v == null ? "" : String(v));
const inputCls = "w-full rounded-md border border-rule bg-surface px-2 py-1 text-[13px] text-ink outline-none focus:border-[color:var(--hrv)]";
const btn = "rounded-md px-3 py-1.5 text-[11px] font-semibold tracking-micro disabled:opacity-40";
// apiFetch throws the raw response body — apiErrorText (api.js) shows the server's sentence.
const errText = apiErrorText;

function blankForm(over = {}) {
  return { name: "", brand: "", role: "food", meal_type: null, serving_qty: "1", serving_unit: "serving",
           serving_label: "", units: [], aliases: [], drink_kind: null, abv: "",
           ...Object.fromEntries(MACROS.map(([k]) => [k, ""])), ...over };
}

function formFromCandidate(c, meal) {
  // A drink candidate starts from a GUESS of its kind, pour and ABV (drinks.js) — the
  // history knows "IPA beer ×10" but never recorded the ABV. Calories stay the logged
  // median; the drink fields offer the oz x ABV estimate beside them.
  const g = meal === "drinks" ? guessDrink(c.name) : null;
  const alc = g && g.kind !== "non_alcoholic";
  return blankForm({
    name: c.name, brand: c.brand ?? "", meal_type: meal,
    serving_qty: alc ? s(g.oz) : s(c.serving_qty), serving_unit: alc ? "oz" : c.serving_unit,
    serving_label: alc ? `${g.oz} oz` : `${c.serving_qty} ${c.serving_unit}`,
    ...(g ? { drink_kind: g.kind, abv: alc ? s(g.abv) : "" } : {}),
    ...Object.fromEntries(MACROS.map(([k]) => [k, s(c.suggested?.[k])])),
  });
}

function formFromFood(f) {
  return blankForm({
    name: f.name, brand: f.brand ?? "", role: f.role === "combo" ? "food" : f.role, meal_type: f.meal_type,
    serving_qty: s(f.serving_qty), serving_unit: f.serving_unit, serving_label: f.serving_label ?? "",
    units: (f.units || []).map((u) => ({ label: u.label, factor: s(u.factor) })),
    drink_kind: f.drink_kind ?? null, abv: s(f.abv),
    ...Object.fromEntries(MACROS.map(([k]) => [k, s(f[k])])),
  });
}

// A form filled from a scanned label carries source "label" (note 40); otherwise the
// caller's source (default "confirmed") applies.
function payload(form, source = form.source || "confirmed") {
  return {
    name: form.name.trim(), role: form.role, meal_type: form.meal_type || null,
    brand: (form.brand || "").trim() || null,
    serving_qty: num(form.serving_qty) ?? 1, serving_unit: form.serving_unit.trim() || "serving",
    serving_label: form.serving_label.trim() || null,
    units: form.units.filter((u) => u.label.trim() && num(u.factor) > 0)
      .map((u) => ({ label: u.label.trim(), factor: num(u.factor) })),
    aliases: form.aliases,
    ...Object.fromEntries(MACROS.map(([k]) => [k, num(form[k])])),
    drink_kind: form.drink_kind || null,
    abv: form.drink_kind && form.drink_kind !== "non_alcoholic" ? num(form.abv) : null,
    source,
  };
}

// Kind + ABV for a drink (note 34). Standard drinks are exact (oz x ABV); the calorie
// figure is the alcohol logger's estimate for that kind, offered, never auto-applied —
// a logged median or a label beats it.
function DrinkFields({ form, set }) {
  const { data: cat } = useQuery({
    queryKey: ["alcohol-categories"], queryFn: api.getAlcoholCategories, staleTime: 1000 * 60 * 60,
  });
  const kind = form.drink_kind;
  const alcoholic = Boolean(kind) && kind !== "non_alcoholic";
  const oz = num(form.serving_qty);
  const abv = num(form.abv);
  const ok = alcoholic && oz > 0 && abv > 0 && (form.serving_unit || "").trim().toLowerCase() === "oz";
  const c = cat?.categories?.find((x) => x.id === kind);
  const est = ok && cat && c ? {
    kcal: Math.round(cat.kcal_per_g_ethanol * oz * abv * cat.ethanol_g_per_oz_pct
                     + 4 * oz * c.carbs_g_per_oz + 4 * oz * c.protein_g_per_oz),
    carbs: +(oz * c.carbs_g_per_oz).toFixed(1),
    protein: +(oz * c.protein_g_per_oz).toFixed(1),
  } : null;
  return (
    <div className="space-y-1.5 rounded-lg border border-rule p-2">
      <p className="text-[9px] tracking-micro text-ink-2">DRINK</p>
      <div className="flex flex-wrap gap-1">
        {DRINK_KINDS.map((k) => (
          <button key={k.id} type="button"
            onClick={() => set({ ...form, drink_kind: k.id,
              ...(k.id === "non_alcoholic" ? { abv: "" } : { serving_unit: "oz" }) })}
            className={`rounded-full px-2 py-0.5 text-[11px] ${kind === k.id ? "border border-[color:var(--hrv)] text-ink" : "border border-rule text-ink-2"}`}>
            {k.emoji} {k.label}
          </button>
        ))}
      </div>
      {alcoholic && (
        <div className="flex items-end gap-2">
          <label className="block w-20 flex-none">
            <span className="text-[9px] tracking-micro text-ink-2">ABV %</span>
            <input type="number" inputMode="decimal" value={form.abv}
              onChange={(e) => set({ ...form, abv: e.target.value })} className={inputCls} />
          </label>
          <p className="min-w-0 flex-1 pb-1 text-[10px] leading-snug text-ink-2">
            {ok
              ? <>= {sduOf(oz, abv, cat?.ethanol_oz_per_sdu).toFixed(2)} standard drinks per serving
                  {est && <> · ≈{est.kcal} kcal estimated</>}</>
              : "Serving in oz (above) × ABV → standard drinks."}
          </p>
          {est && (
            <button type="button"
              onClick={() => set({ ...form, calories: String(est.kcal), carbs_g: String(est.carbs),
                                   protein_g: String(est.protein) })}
              className="flex-none pb-1 text-[11px] text-[color:var(--hrv)]">use estimate</button>
          )}
        </div>
      )}
    </div>
  );
}

function MacroGrid({ form, set }) {
  return (
    <div className="grid grid-cols-4 gap-1.5">
      {MACROS.map(([k, label]) => (
        <label key={k} className="block">
          <span className="text-[9px] tracking-micro text-ink-2">{label.toUpperCase()}</span>
          <input type="number" inputMode="decimal" value={form[k]}
            onChange={(e) => set({ ...form, [k]: e.target.value })} className={inputCls} />
        </label>
      ))}
    </div>
  );
}

// Fill a My Food form from a photo of its nutrition label (note 40) — the same parser
// Log Food uses. Label values are per serving, so one serving = what the label calls a
// serving; "servings per container" becomes a "package" unit when the label gives it.
function ScanLabel({ form, set }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const onFile = async (file) => {
    if (!file) return;
    setErr(null);
    setBusy(true);
    try {
      const d = await api.parseNutritionLabel(await shrinkImage(file));
      const spc = num(d.servings_per_container);
      set({
        ...form,
        name: form.name.trim() ? form.name : (d.name || ""),
        serving_qty: "1", serving_unit: "serving",
        serving_label: d.serving_size || form.serving_label,
        units: spc && spc > 0 && !form.units.some((u) => u.label === "package")
          ? [...form.units, { label: "package", factor: String(spc) }] : form.units,
        ...Object.fromEntries(MACROS.map(([k]) => [k, d[k] == null ? form[k] : s(d[k])])),
        source: "label",
      });
    } catch (e) {
      setErr(e.message || "Could not read that label");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div>
      <label className={`${btn} flex w-full cursor-pointer items-center justify-center gap-1.5 border border-rule text-ink-2 ${busy ? "opacity-50" : ""}`}>
        <span>📷</span><span>{busy ? "READING LABEL…" : form.source === "label" ? "SCANNED ✓ — SCAN AGAIN" : "SCAN A NUTRITION LABEL"}</span>
        <input type="file" accept="image/*" capture="environment" className="hidden" disabled={busy}
          onChange={(e) => { onFile(e.target.files?.[0]); e.target.value = ""; }} />
      </label>
      {err && <p className="mt-1 text-[11px] text-over">{err}</p>}
      {form.source === "label" && !err && (
        <p className="mt-1 text-[10px] text-ink-2">Filled from the label — check the name and serving before saving.</p>
      )}
    </div>
  );
}

function FoodForm({ form, set, similar = [] }) {
  const setUnit = (i, patch) =>
    set({ ...form, units: form.units.map((u, j) => (j === i ? { ...u, ...patch } : u)) });
  const { data: brandOptions = [] } = useQuery({ queryKey: ["food-brands"], queryFn: () => api.getBrands() });
  return (
    <div className="space-y-2">
      <ScanLabel form={form} set={set} />
      <div className="flex gap-2">
        <input value={form.name} onChange={(e) => set({ ...form, name: e.target.value })}
          placeholder="Name" className={inputCls} />
        <select value={form.role} onChange={(e) => set({ ...form, role: e.target.value })}
          className="rounded-md bg-surface px-2 text-[12px] text-ink">
          <option value="food">food</option>
          <option value="topping">topping</option>
        </select>
      </div>
      {/* Brand / restaurant (note 60): part of the food's identity — "Cream cheese" +
          Kirkland is a different food from generic cream cheese — but kept out of the
          name so the name stays short. Every entry logged from this food carries it. */}
      <input value={form.brand ?? ""} onChange={(e) => set({ ...form, brand: e.target.value })}
        list="myfood-brand-options" placeholder="Brand (optional)" autoCapitalize="words"
        className={inputCls} />
      <datalist id="myfood-brand-options">
        {brandOptions.map((b) => <option key={b} value={b} />)}
      </datalist>
      <div className="grid grid-cols-3 gap-1.5">
        <label className="block"><span className="text-[9px] tracking-micro text-ink-2">SERVING QTY</span>
          <input type="number" inputMode="decimal" value={form.serving_qty}
            onChange={(e) => set({ ...form, serving_qty: e.target.value })} className={inputCls} /></label>
        <label className="block"><span className="text-[9px] tracking-micro text-ink-2">UNIT</span>
          <input value={form.serving_unit} onChange={(e) => set({ ...form, serving_unit: e.target.value })}
            className={inputCls} /></label>
        <label className="block"><span className="text-[9px] tracking-micro text-ink-2">LABEL</span>
          <input value={form.serving_label} placeholder="1 bar"
            onChange={(e) => set({ ...form, serving_label: e.target.value })} className={inputCls} /></label>
      </div>
      {(form.meal_type === "drinks" || form.drink_kind) && <DrinkFields form={form} set={set} />}
      <p className="text-[10px] text-ink-2">Numbers below are for ONE serving as defined above.</p>
      <MacroGrid form={form} set={set} />
      <div>
        <p className="text-[9px] tracking-micro text-ink-2">OTHER UNITS (servings per one unit — a slice of a pie is 0.125)</p>
        {form.units.map((u, i) => (
          <div key={i} className="mt-1 flex gap-1.5">
            <input value={u.label} placeholder="slice" onChange={(e) => setUnit(i, { label: e.target.value })} className={inputCls} />
            <input type="number" inputMode="decimal" value={u.factor} placeholder="0.125"
              onChange={(e) => setUnit(i, { factor: e.target.value })} className={inputCls} />
            <button onClick={() => set({ ...form, units: form.units.filter((_, j) => j !== i) })}
              className="px-2 text-ink-2">×</button>
          </div>
        ))}
        <button onClick={() => set({ ...form, units: [...form.units, { label: "", factor: "" }] })}
          className="mt-1 text-[11px] text-[color:var(--hrv)]">+ add a unit</button>
      </div>
      {similar.length > 0 && (
        <div>
          <p className="text-[9px] tracking-micro text-ink-2">SAME FOOD UNDER ANOTHER NAME?</p>
          {similar.map((o) => (
            <label key={o.item_key} className="mt-1 flex items-center gap-2 text-[12px] text-ink-2">
              <input type="checkbox" checked={form.aliases.includes(o.item_key)}
                onChange={(e) => set({ ...form, aliases: e.target.checked
                  ? [...form.aliases, o.item_key] : form.aliases.filter((a) => a !== o.item_key) })} />
              {o.name} <span className="text-ink-3">×{o.uses}</span>
            </label>
          ))}
        </div>
      )}
    </div>
  );
}

// After a food exists: what apply-to-past would do, then do it (undoable).
function PastEntries({ food }) {
  const qc = useQueryClient();
  const { data: prev, refetch } = useQuery({
    queryKey: ["my-food-past", food.id], queryFn: () => api.myFoodPastEntries(food.id) });
  const [done, setDone] = useState(null);
  const apply = useMutation({
    mutationFn: () => api.applyMyFoodToPast(food.id),
    onSuccess: (r) => { setDone(r); refetch(); qc.invalidateQueries(); },
  });
  const undo = useMutation({
    mutationFn: () => api.undoMyFoodBackfill(done.backfill_id),
    onSuccess: () => { setDone(null); refetch(); qc.invalidateQueries(); },
  });
  if (!prev) return <p className="text-[11px] text-ink-2">Checking past entries…</p>;
  if (done) {
    return (
      <p className="text-[11px] text-[color:var(--rhr)]">
        Updated {done.updated} past entries.{" "}
        <button onClick={() => undo.mutate()} className="text-[color:var(--hrv)] underline">Undo</button>
      </p>
    );
  }
  const mism = prev.unit_mismatch.length;
  return (
    <div className="text-[11px] text-ink-2">
      {prev.matching} past entries match; {prev.would_change} would change
      {mism > 0 && <> · {mism} in a unit this food doesn't define (left alone — add the unit to include them)</>}.
      {prev.would_change > 0 && (
        <button onClick={() => apply.mutate()} disabled={apply.isPending}
          className={`${btn} ml-2 bg-ink text-canvas`}>
          {apply.isPending ? "APPLYING…" : `APPLY TO ${prev.would_change}`}
        </button>
      )}
    </div>
  );
}

function toppingForm(name) {
  const h = TOPPING_HINTS[(name || "").toLowerCase()] || {};
  return blankForm({ name: name || "", role: "topping", hinted: h.calories != null,
    serving_qty: s(h.serving_qty ?? 1), serving_unit: h.serving_unit ?? "serving",
    serving_label: h.serving_label ?? "",
    ...Object.fromEntries(MACROS.map(([k]) => [k, s(h[k])])) });
}

function SplitForm({ c, meal, onCreated }) {
  const hint = c.split || { base: c.name, topping: "" };
  const [base, setBase] = useState(formFromCandidate({ ...c, name: hint.base }, meal));
  // Any number of toppings (a pizza is sauce AND cheese).
  const [tops, setTops] = useState([toppingForm(hint.topping)]);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const setTop = (i, f) => setTops((ts) => ts.map((t, j) => (j === i ? f : t)));
  // Base = the entry's usual total minus ALL the toppings, as a starting point.
  const fillBase = () => setBase({ ...base, ...Object.fromEntries(MACROS.map(([k]) => {
    const t = num(c.suggested?.[k]);
    const p = tops.reduce((acc, x) => acc + (num(x[k]) ?? 0), 0);
    return [k, t == null ? base[k] : s(Math.max(0, +((t - p).toFixed(1))))];
  })) });
  const create = async () => {
    setBusy(true); setErr(null);
    try {
      // ONE request: base, toppings and the combination are created together or not at
      // all. A name already in the library is reused (numbers untouched), and the
      // "same food under another name" choice goes to the COMBINATION — those are
      // names the whole dish was logged under, not the base's.
      const r = await api.createMyFoodCombo({
        name: c.name, meal_type: meal, serving_qty: 1, serving_unit: c.serving_unit,
        serving_label: `1 ${c.serving_unit}`, aliases: base.aliases, source: "confirmed",
        parts: [
          { food: { ...payload(base), aliases: [] }, servings: 1 },
          ...tops.map((t) => ({ food: { ...payload(t), role: "topping", aliases: [] }, servings: 1 })),
        ],
      });
      const reused = r.parts.filter((p) => p.reused).map((p) => p.name);
      onCreated(r.combo, reused.length
        ? `Used your existing ${reused.join(", ")} — numbers unchanged; edit them in the Library.`
        : null);
    } catch (e) { setErr(errText(e)); } finally { setBusy(false); }
  };
  const incomplete = !base.name.trim() || num(base.calories) == null
    || tops.some((t) => !t.name.trim() || num(t.calories) == null);
  return (
    <div className="space-y-3">
      <p className="text-[11px] text-ink-2">
        Logs as {1 + tops.length} rows from now on. Usual total: {c.suggested?.calories ?? "—"} kcal.
        A name already in your library is reused as it is.
      </p>
      {/* Base first, toppings below — the order the rows are logged in. */}
      <div className="rounded-lg border border-rule p-2">
        <div className="mb-1 flex items-baseline justify-between">
          <p className="text-[9px] tracking-micro text-ink-2">BASE</p>
          <button onClick={fillBase} className="text-[10px] text-[color:var(--hrv)]">= total − toppings</button>
        </div>
        <FoodForm form={base} set={setBase} similar={c.similar} />
      </div>
      {tops.map((t, i) => (
        <div key={i} className="rounded-lg border border-rule p-2">
          <div className="mb-1 flex items-baseline justify-between">
            <p className="text-[9px] tracking-micro text-ink-2">
              TOPPING{tops.length > 1 ? ` ${i + 1}` : ""}{t.hinted ? " · typical label values — check yours" : ""}
            </p>
            {tops.length > 1 && (
              <button onClick={() => setTops((ts) => ts.filter((_, j) => j !== i))}
                className="px-1 text-ink-2" title="Remove this topping">×</button>
            )}
          </div>
          <FoodForm form={t} set={(f) => setTop(i, f)} />
        </div>
      ))}
      <button onClick={() => setTops((ts) => [...ts, toppingForm("")])}
        className={`${btn} w-full border border-dashed border-rule text-ink-2`}>
        + ADD ANOTHER TOPPING
      </button>
      {err && <p className="text-[11px] text-over">{err}</p>}
      <button onClick={create} disabled={busy || incomplete}
        className={`${btn} w-full bg-ink text-canvas`}>
        {busy ? "CREATING…" : `CREATE BASE + ${tops.length} TOPPING${tops.length > 1 ? "S" : ""}`}
      </button>
    </div>
  );
}

function CandidateCard({ c, meal, library, onChanged, onSkipped }) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState("form");            // form | split
  const [form, setForm] = useState(() => formFromCandidate(c, meal));
  const [created, setCreated] = useState(null);
  const [createdNote, setCreatedNote] = useState(null);
  const [err, setErr] = useState(null);
  const create = useMutation({
    mutationFn: () => api.createMyFood(payload(form)),
    onSuccess: (f) => { setCreated(f); onChanged(); },
    onError: (e) => setErr(errText(e)),
  });
  // Skipping must drop the card from the list — refetch the candidates, not the library.
  const skip = useMutation({ mutationFn: () => api.dismissMyFoodCandidate(c.item_key), onSuccess: onSkipped });
  const flags = [c.unit_mixed && "MIXED UNITS", c.combo && "COMBO", c.distinct_kcal > 1 && `${c.distinct_kcal} KCAL VALUES`]
    .filter(Boolean);
  return (
    <div className="rounded-xl bg-surface p-3">
      <button onClick={() => setOpen((v) => !v)} className="flex w-full items-baseline gap-2 text-left">
        <span className="min-w-0 flex-1 text-[13px] text-ink">{created ? `✓ ${created.name}` : c.name}</span>
        <span className="flex-none text-[10px] tabular-nums text-ink-2">×{c.uses}</span>
      </button>
      {!open && !created && (
        <p className="mt-0.5 text-[10px] text-ink-2">
          ~{c.suggested?.calories ?? "—"} kcal per {c.serving_qty} {c.serving_unit}
          {flags.length > 0 && <span className="text-[color:var(--breakfast)]"> · {flags.join(" · ")}</span>}
        </p>
      )}
      {open && created && (
        <div className="mt-2 space-y-1">
          {createdNote && <p className="text-[11px] text-ink-2">{createdNote}</p>}
          <PastEntries food={created} />
        </div>
      )}
      {open && !created && (
        <div className="mt-2 space-y-2">
          {c.unit_mixed && (
            <p className="text-[10px] text-[color:var(--breakfast)]">
              Logged in {Object.entries(c.units_seen).map(([u, n]) => `${u} ×${n}`).join(", ")} —
              set one serving and add the others as units.
            </p>
          )}
          <SegmentedTabs value={mode} onChange={setMode}
            options={[{ value: "form", label: "ONE FOOD" }, { value: "split", label: "BASE + TOPPING" }]} />
          {mode === "form" && (
            <>
              <FoodForm form={form} set={setForm} similar={c.similar} />
              {err && <p className="text-[11px] text-over">{err}</p>}
              <button onClick={() => { setErr(null); create.mutate(); }}
                disabled={create.isPending || !form.name.trim() || num(form.calories) == null}
                className={`${btn} w-full bg-ink text-canvas`}>
                {create.isPending ? "CREATING…" : "CREATE MY FOOD"}
              </button>
            </>
          )}
          {mode === "split" && (
            <SplitForm c={c} meal={meal}
              onCreated={(f, note) => { setCreated(f); setCreatedNote(note); onChanged(); }} />
          )}
          <button onClick={() => skip.mutate()} disabled={skip.isPending}
            className={`${btn} w-full border border-rule text-ink-2`}>SKIP — NOT WORTH A MY FOOD</button>
        </div>
      )}
    </div>
  );
}

function Review({ library }) {
  const qc = useQueryClient();
  const [perGroup, setPerGroup] = useState(15);
  const { data, isLoading } = useQuery({
    queryKey: ["my-food-candidates", perGroup], queryFn: () => api.myFoodCandidates(perGroup) });
  const groups = data?.groups ?? [];
  const [meal, setMeal] = useState(null);
  const active = groups.find((g) => g.meal_type === meal) ?? groups[0];
  const changed = () => { qc.invalidateQueries({ queryKey: ["my-foods"] }); qc.invalidateQueries({ queryKey: ["food-brands"] }); };
  const skipped = () => { qc.invalidateQueries({ queryKey: ["my-food-candidates"] }); };
  if (isLoading) return <p className="text-[12px] text-ink-2">Loading candidates…</p>;
  if (!groups.length) return <p className="text-[12px] text-ink-2">Nothing left to review.</p>;
  return (
    <div className="space-y-3">
      <SegmentedTabs value={active.meal_type} onChange={setMeal}
        options={MEALS.filter((m) => groups.some((g) => g.meal_type === m))
          .map((m) => ({ value: m, label: `${m.toUpperCase()} ${groups.find((g) => g.meal_type === m).total}` }))} />
      <p className="text-[11px] text-ink-2">
        Ranked by how often you log them. Suggested numbers are the median of your entries —
        check them against the label before creating.
      </p>
      {active.candidates.map((c) => (
        <CandidateCard key={c.item_key} c={c} meal={active.meal_type} library={library}
          onChanged={changed} onSkipped={skipped} />
      ))}
      {active.total > active.candidates.length && (
        <button onClick={() => setPerGroup((n) => n + 15)} className={`${btn} w-full border border-rule text-ink-2`}>
          SHOW MORE ({active.total - active.candidates.length})
        </button>
      )}
    </div>
  );
}

// Build or edit a combination from foods already in the library (2026-09-13: the
// review was the only way to make one, and a combo whose parts already existed —
// or a new topping variant of the same crust — had no way in).
function ComboBuilder({ foods, initial = null, onSaved, onClose }) {
  const singles = foods.filter((f) => f.role !== "combo");
  const byId = Object.fromEntries(singles.map((f) => [String(f.id), f]));
  const [name, setName] = useState(initial?.name ?? "");
  const [unit, setUnit] = useState(initial?.serving_unit ?? "serving");
  const [alias, setAlias] = useState("");
  const [parts, setParts] = useState(initial
    ? initial.components.map((c) => ({ id: String(c.my_food_id), servings: s(c.servings) }))
    : [{ id: "", servings: "1" }, { id: "", servings: "1" }]);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(null);
  const [note, setNote] = useState(null);
  const setPart = (i, patch) => setParts((ps) => ps.map((p, j) => (j === i ? { ...p, ...patch } : p)));
  const total = parts.reduce((acc, p) => acc + (byId[p.id]?.calories ?? 0) * (num(p.servings) ?? 0), 0);
  const valid = name.trim() && parts.length >= 2 && parts.every((p) => byId[p.id] && num(p.servings) > 0);
  const save = async () => {
    setBusy(true); setErr(null);
    try {
      const comps = parts.map((p) => ({ my_food_id: Number(p.id), servings: num(p.servings) }));
      if (initial) {
        setSaved(await api.updateMyFood(initial.id, {
          name: name.trim(), serving_unit: unit.trim() || "serving", components: comps }));
      } else {
        const r = await api.createMyFoodCombo({
          name: name.trim(), serving_qty: 1, serving_unit: unit.trim() || "serving",
          aliases: alias.split(",").map((x) => x.trim()).filter(Boolean),
          parts: comps, source: "confirmed" });
        setSaved(r.combo);
        setNote(r.moved_aliases?.length
          ? `Moved ${r.moved_aliases.map((x) => `"${x.alias}"`).join(", ")} from ${r.moved_aliases[0].from} to this combination.`
          : null);
      }
      onSaved();
    } catch (e) { setErr(errText(e)); } finally { setBusy(false); }
  };
  if (saved) {
    return (
      <div className="space-y-2 rounded-xl border border-rule p-3">
        <p className="text-[12px] text-ink">✓ {saved.name} — {saved.calories} kcal per {saved.serving_label || saved.serving_unit}</p>
        {note && <p className="text-[11px] text-ink-2">{note}</p>}
        <PastEntries food={saved} />
        <button onClick={onClose} className={`${btn} w-full border border-rule text-ink-2`}>DONE</button>
      </div>
    );
  }
  return (
    <div className="space-y-2 rounded-xl border border-rule p-3">
      <p className="text-[9px] tracking-micro text-ink-2">{initial ? "EDIT COMBINATION" : "NEW COMBINATION"}</p>
      <input value={name} onChange={(e) => setName(e.target.value)}
        placeholder="Name, e.g. Personal pizza with chickpea crust" className={inputCls} />
      {parts.map((p, i) => (
        <div key={i} className="flex items-center gap-1.5">
          <span className="w-14 flex-none text-[9px] tracking-micro text-ink-2">{i === 0 ? "BASE" : "TOPPING"}</span>
          <select value={p.id} onChange={(e) => setPart(i, { id: e.target.value })}
            className="min-w-0 flex-1 rounded-md bg-surface px-2 py-1 text-[12px] text-ink outline-none">
            <option value="">choose…</option>
            {singles.map((f) => (
              <option key={f.id} value={f.id}>
                {f.name} ({f.serving_label || `${f.serving_qty} ${f.serving_unit}`})
              </option>
            ))}
          </select>
          <input type="number" inputMode="decimal" value={p.servings} title="servings"
            onChange={(e) => setPart(i, { servings: e.target.value })}
            className="w-14 flex-none rounded-md bg-surface px-2 py-1 text-[12px] text-ink outline-none" />
          {parts.length > 2 && (
            <button onClick={() => setParts((ps) => ps.filter((_, j) => j !== i))}
              className="px-1 text-ink-2" title="Remove">×</button>
          )}
        </div>
      ))}
      <button onClick={() => setParts((ps) => [...ps, { id: "", servings: "1" }])}
        className="text-[11px] text-[color:var(--hrv)]">+ add a topping</button>
      <p className="text-[10px] text-ink-2">
        Amounts are in each food's own serving — 0.5 of a "2 tbsp" topping is 1 tbsp.
        Missing a part? Add it with + New food first.
      </p>
      <div className="grid grid-cols-2 gap-1.5">
        <label className="block"><span className="text-[9px] tracking-micro text-ink-2">ONE SERVING IS 1…</span>
          <input value={unit} onChange={(e) => setUnit(e.target.value)} placeholder="piece" className={inputCls} /></label>
        {!initial && (
          <label className="block"><span className="text-[9px] tracking-micro text-ink-2">OTHER NAMES (comma-separated)</span>
            <input value={alias} onChange={(e) => setAlias(e.target.value)}
              placeholder="Personal pizza chickpea crust" className={inputCls} /></label>
        )}
      </div>
      <p className="text-[11px] text-ink-2">Total: {Math.round(total)} kcal per serving</p>
      {err && <p className="text-[11px] text-over">{err}</p>}
      <div className="flex gap-2">
        <button onClick={save} disabled={busy || !valid} className={`${btn} flex-1 bg-ink text-canvas`}>
          {busy ? "SAVING…" : initial ? "SAVE" : "CREATE"}
        </button>
        <button onClick={onClose} className={`${btn} flex-1 border border-rule text-ink-2`}>CANCEL</button>
      </div>
    </div>
  );
}

function NewFood({ onSaved, onClose, initial = {} }) {
  const [form, setForm] = useState(() => blankForm(initial));
  const [err, setErr] = useState(null);
  const [saved, setSaved] = useState(null);
  const create = useMutation({
    mutationFn: () => api.createMyFood(payload(form)),
    onSuccess: (f) => { setSaved(f); onSaved(); },
    onError: (e) => setErr(errText(e)),
  });
  if (saved) {
    return (
      <div className="space-y-2 rounded-xl border border-rule p-3">
        <p className="text-[12px] text-ink">✓ {saved.name}</p>
        <PastEntries food={saved} />
        <button onClick={onClose} className={`${btn} w-full border border-rule text-ink-2`}>DONE</button>
      </div>
    );
  }
  return (
    <div className="space-y-2 rounded-xl border border-rule p-3">
      <p className="text-[9px] tracking-micro text-ink-2">NEW FOOD</p>
      <FoodForm form={form} set={setForm} />
      {err && <p className="text-[11px] text-over">{err}</p>}
      <div className="flex gap-2">
        <button onClick={() => { setErr(null); create.mutate(); }}
          disabled={create.isPending || !form.name.trim() || num(form.calories) == null}
          className={`${btn} flex-1 bg-ink text-canvas`}>{create.isPending ? "CREATING…" : "CREATE"}</button>
        <button onClick={onClose} className={`${btn} flex-1 border border-rule text-ink-2`}>CANCEL</button>
      </div>
    </div>
  );
}

function Library({ foods }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(null);         // food id
  const [form, setForm] = useState(null);
  const [err, setErr] = useState(null);
  const [adding, setAdding] = useState(null);           // null | "food" | "combo"
  // Two areas (2026-09-13): the dishes you log, and the ingredients they are
  // built from — with a search that works inside whichever area is open.
  const [section, setSection] = useState("combos");     // combos | ingredients | drinks
  const [q, setQ] = useState("");
  // Drinks split by kind (note 34) so a growing beer list stays searchable.
  const [drinkKind, setDrinkKind] = useState(null);
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["my-foods"] });
    qc.invalidateQueries({ queryKey: ["my-food-candidates"] });
  };
  const save = useMutation({
    mutationFn: () => api.updateMyFood(editing, { ...payload(form), aliases: undefined, units: payload(form).units }),
    onSuccess: () => { setEditing(null); refresh(); },
    onError: (e) => setErr(errText(e)),
  });
  const archive = useMutation({ mutationFn: (id) => api.archiveMyFood(id), onSuccess: refresh });
  const byId = Object.fromEntries(foods.map((f) => [f.id, f]));
  const combos = foods.filter((f) => f.role === "combo");
  // Drinks get their own area (note 34); they are not ingredients of anything.
  const drinks = foods.filter((f) => f.role !== "combo" && isDrink(f));
  const ingredients = foods.filter((f) => f.role !== "combo" && !isDrink(f));
  // Which combinations use each ingredient — worth knowing before you edit it,
  // since every one of them re-totals.
  const usedIn = {};
  for (const cb of combos) for (const c of cb.components) (usedIn[c.my_food_id] ??= []).push(cb.name);
  const needle = q.trim().toLowerCase();
  const matches = (f) => !needle
    || f.name.toLowerCase().includes(needle)
    || (f.brand || "").toLowerCase().includes(needle)
    || (f.aliases || []).some((x) => x.includes(needle))
    // Searching combinations also searches their PARTS: "cream cheese" finds every bagel.
    || (f.role === "combo" && f.components.some((c) => byId[c.my_food_id]?.name.toLowerCase().includes(needle)));
  const byName = (x, y) => x.name.localeCompare(y.name);
  const shownCombos = combos.filter(matches).sort(byName);
  const shownBases = ingredients.filter((f) => f.role !== "topping" && matches(f)).sort(byName);
  const shownToppings = ingredients.filter((f) => f.role === "topping" && matches(f)).sort(byName);
  const kindOf = (f) => f.drink_kind || "non_alcoholic";
  const shownDrinks = drinks.filter((f) => matches(f) && (!drinkKind || kindOf(f) === drinkKind)).sort(byName);
  const drinkKindsPresent = DRINK_KINDS.filter((k) => drinks.some((f) => kindOf(f) === k.id));

  const row = (f) => (
    <div key={f.id} className="rounded-xl bg-surface p-3">
      <div className="flex items-baseline gap-2">
        <span className="min-w-0 flex-1 text-[13px] text-ink">{f.name}<BrandTag brand={f.brand} /></span>
        {f.role === "topping" && <span className="flex-none text-[9px] tracking-micro text-ink-2">TOPPING</span>}
      </div>
      <p className="mt-0.5 text-[10px] text-ink-2">
        {f.calories ?? "—"} kcal · {f.protein_g ?? "—"} P per {f.serving_label || `${f.serving_qty} ${f.serving_unit}`}
        {f.role === "combo" && <> · = {f.components.map((c) => `${byId[c.my_food_id]?.name ?? "?"}${c.servings !== 1 ? ` ×${c.servings}` : ""}`).join(" + ")}</>}
        {f.uses > 0 && <> · logged ×{f.uses}</>}
        {isAlcoholic(f) && <> · {f.abv}% ABV · {sduOf(f.serving_qty, f.abv).toFixed(2)} std drinks</>}
      </p>
      {f.role !== "combo" && usedIn[f.id] && (
        <p className="mt-0.5 text-[10px] text-ink-3">in {usedIn[f.id].join(", ")}</p>
      )}
      {editing === f.id && f.role === "combo" ? (
        <div className="mt-2">
          <ComboBuilder foods={foods} initial={f} onSaved={refresh} onClose={() => setEditing(null)} />
        </div>
      ) : editing === f.id ? (
        <div className="mt-2 space-y-2">
          <FoodForm form={form} set={setForm} />
          {err && <p className="text-[11px] text-over">{err}</p>}
          <div className="flex gap-2">
            <button onClick={() => { setErr(null); save.mutate(); }} className={`${btn} flex-1 bg-ink text-canvas`}>SAVE</button>
            <button onClick={() => setEditing(null)} className={`${btn} flex-1 border border-rule text-ink-2`}>CANCEL</button>
          </div>
          <p className="text-[10px] text-ink-2">
            Saving changes the library only{usedIn[f.id] ? ` — ${usedIn[f.id].length} combination${usedIn[f.id].length > 1 ? "s" : ""} re-total` : ""}.
            Past entries change only if you apply below.
          </p>
          <PastEntries food={f} />
        </div>
      ) : (
        <div className="mt-1.5 flex gap-3 text-[11px]">
          <button onClick={() => { setEditing(f.id); setForm(f.role === "combo" ? null : formFromFood(f)); setErr(null); }}
            className="text-[color:var(--hrv)]">Edit</button>
          <button onClick={() => archive.mutate(f.id)} className="text-ink-2">Archive</button>
        </div>
      )}
    </div>
  );
  const heading = (t, n) => (
    <p className="pt-1 text-[9px] tracking-micro text-ink-2">{t} · {n}</p>
  );

  return (
    <div className="space-y-2">
      <SegmentedTabs value={section} onChange={(v) => { setSection(v); setAdding(null); }}
        options={[
          { value: "combos", label: `COMBINATIONS ${combos.length}` },
          { value: "ingredients", label: `INGREDIENTS ${ingredients.length}` },
          { value: "drinks", label: `DRINKS ${drinks.length}` },
        ]} />
      <input value={q} onChange={(e) => setQ(e.target.value)}
        placeholder={section === "combos" ? "Search dishes or what's in them…"
          : section === "drinks" ? "Search drinks…" : "Search ingredients…"}
        className="w-full rounded-lg border border-rule bg-surface px-3 py-1.5 text-sm text-ink placeholder:text-ink-3 outline-none focus:border-[color:var(--hrv)]" />
      {adding === null && (
        <button onClick={() => setAdding(section === "combos" ? "combo" : section === "drinks" ? "drink" : "food")}
          className={`${btn} w-full border border-dashed border-rule text-ink-2`}>
          {section === "combos" ? "+ NEW COMBINATION" : section === "drinks" ? "+ NEW DRINK" : "+ NEW FOOD"}
        </button>
      )}
      {adding === "food" && <NewFood onSaved={refresh} onClose={() => setAdding(null)} />}
      {adding === "drink" && (
        <NewFood onSaved={refresh} onClose={() => setAdding(null)}
          initial={{ meal_type: "drinks", drink_kind: drinkKind || "beer", serving_qty: "12",
                     serving_unit: "oz", serving_label: "12 oz", abv: "5" }} />
      )}
      {adding === "combo" && <ComboBuilder foods={foods} onSaved={refresh} onClose={() => setAdding(null)} />}

      {section === "combos" && (
        shownCombos.length ? shownCombos.map(row) : (
          <p className="text-[12px] text-ink-2">
            {needle ? "No combination matches." : "No combinations yet — build one above, or split a candidate in Review."}
          </p>
        )
      )}
      {section === "ingredients" && (
        <>
          {shownBases.length > 0 && heading("FOODS", shownBases.length)}
          {shownBases.map(row)}
          {shownToppings.length > 0 && heading("TOPPINGS", shownToppings.length)}
          {shownToppings.map(row)}
          {!shownBases.length && !shownToppings.length && (
            <p className="text-[12px] text-ink-2">
              {needle ? "No ingredient matches." : "No ingredients yet — start in Review, or add one above."}
            </p>
          )}
        </>
      )}
      {section === "drinks" && (
        <>
          {drinkKindsPresent.length > 1 && (
            <div className="flex flex-wrap gap-1">
              {[{ id: null, label: "All", emoji: "" }, ...drinkKindsPresent].map((k) => (
                <button key={k.id ?? "all"} onClick={() => setDrinkKind(k.id)}
                  className={`rounded-full px-2 py-0.5 text-[11px] ${drinkKind === k.id ? "border border-[color:var(--hrv)] text-ink" : "border border-rule text-ink-2"}`}>
                  {k.emoji} {k.label}
                </button>
              ))}
            </div>
          )}
          {DRINK_KINDS.map((k) => {
            const inKind = shownDrinks.filter((f) => kindOf(f) === k.id);
            if (!inKind.length) return null;
            return (
              <div key={k.id} className="space-y-2">
                {heading(`${k.emoji} ${k.label.toUpperCase()}`, inKind.length)}
                {inKind.map(row)}
              </div>
            );
          })}
          {!shownDrinks.length && (
            <p className="text-[12px] text-ink-2">
              {needle || drinkKind ? "No drink matches." : "No drinks yet — add one above, save one from the alcohol logger, or start in Review (Drinks)."}
            </p>
          )}
        </>
      )}
    </div>
  );
}

// MY RECIPES (note 37) — the household app's recipes, read-only, with per-serving
// nutrition estimated once here. Deliberately its own tab: the user keeps My Foods for the
// things he eats often, and 41 recipes would bury them.
function Recipes() {
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [cat, setCat] = useState(null);
  const [editing, setEditing] = useState(null);      // recipe_id
  const [form, setForm] = useState({});
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(null);
  const { data, isLoading } = useQuery({ queryKey: ["recipes"], queryFn: () => api.listRecipes() });
  const refresh = () => qc.invalidateQueries({ queryKey: ["recipes"] });
  const run = async (fn) => {
    setErr(null);
    try { await fn(); refresh(); } catch (e) { setErr(errText(e)); } finally { setBusy(null); }
  };

  if (isLoading) return <p className="text-[12px] text-ink-2">Loading recipes…</p>;
  if (data && data.available === false) {
    return (
      <p className="text-[12px] leading-snug text-ink-2">
        The household app's recipe database isn't attached yet, so there's nothing to show.
        <span className="block text-ink-3">{data.detail}</span>
      </p>
    );
  }
  const all = data?.recipes ?? [];
  const cats = [...new Set(all.map((r) => r.category || "other"))].sort();
  const needle = q.trim().toLowerCase();
  const shown = all.filter((r) =>
    (!cat || (r.category || "other") === cat)
    && (!needle || r.title.toLowerCase().includes(needle)
        || (r.ingredient_text || "").toLowerCase().includes(needle)));

  const status = (r) => {
    const n = r.nutrition;
    if (!n || n.calories == null) return { label: "NOT ESTIMATED", color: "text-ink-3" };
    if (n.stale) return { label: "RECIPE CHANGED", color: "text-[color:var(--breakfast)]" };
    if (n.source === "confirmed") return { label: "CONFIRMED", color: "text-[color:var(--rhr)]" };
    return { label: "ESTIMATE", color: "text-ink-2" };
  };

  return (
    <div className="space-y-2">
      <p className="text-[10px] leading-snug text-ink-2">
        Recipes live in the household app — read-only here. Nutrition is estimated once per
        recipe from its ingredients ÷ servings; check the numbers, then it logs like any food.
      </p>
      <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search recipes or ingredients…"
        className="w-full rounded-lg border border-rule bg-surface px-3 py-1.5 text-sm text-ink placeholder:text-ink-3 outline-none focus:border-[color:var(--hrv)]" />
      {cats.length > 1 && (
        <div className="flex flex-wrap gap-1">
          {[{ id: null, label: "All" }, ...cats.map((c) => ({ id: c, label: c }))].map((c) => (
            <button key={c.id ?? "all"} onClick={() => setCat(c.id)}
              className={`rounded-full px-2 py-0.5 text-[11px] ${cat === c.id ? "border border-[color:var(--hrv)] text-ink" : "border border-rule text-ink-2"}`}>
              {c.label}
            </button>
          ))}
        </div>
      )}
      {err && <p className="text-[11px] text-over">{err}</p>}
      {shown.map((r) => {
        const n = r.nutrition;
        const st = status(r);
        const isEditing = editing === r.recipe_id;
        return (
          <div key={r.recipe_id} className="rounded-xl bg-surface p-3">
            <div className="flex items-baseline gap-2">
              <span className="min-w-0 flex-1 text-[13px] text-ink">{r.title}</span>
              <span className={`flex-none text-[9px] tracking-micro ${st.color}`}>{st.label}</span>
            </div>
            <p className="mt-0.5 text-[10px] text-ink-2">
              {n?.calories != null
                ? <>{Math.round(n.calories)} kcal · {n.protein_g ?? "—"} g protein per serving · makes{" "}
                    {n.servings ?? r.servings ?? "?"}
                    {n.servings_basis === "estimated" ? " (estimated yield)" : ""}
                    {n.serving_description ? ` · ${n.serving_description}` : ""}</>
                : <>makes {r.servings ?? "?"} · {r.n_ingredients} ingredients</>}
              {r.uses > 0 && <> · logged ×{r.uses}</>}
            </p>
            {/* The estimate is only as good as what it assumed — show it (note 37). */}
            {n?.assumptions && (
              <p className="mt-0.5 text-[10px] leading-snug text-ink-3">{n.assumptions}</p>
            )}
            {r.category === "drink" && (
              <p className="mt-0.5 text-[10px] text-[color:var(--breakfast)]">
                Calories only — log the standard drinks in the alcohol logger.
              </p>
            )}
            {n?.stale && (
              <p className="mt-0.5 text-[10px] text-[color:var(--breakfast)]">
                The recipe changed in the household app since this estimate — re-estimate it.
              </p>
            )}
            {isEditing ? (
              <div className="mt-2 space-y-2">
                <div className="grid grid-cols-4 gap-1.5">
                  <label className="block"><span className="text-[9px] tracking-micro text-ink-2">SERVINGS</span>
                    <input type="number" inputMode="decimal" value={form.servings ?? ""}
                      onChange={(e) => setForm({ ...form, servings: e.target.value })} className={inputCls} /></label>
                  {MACROS.map(([k, label]) => (
                    <label key={k} className="block">
                      <span className="text-[9px] tracking-micro text-ink-2">{label.toUpperCase()}</span>
                      <input type="number" inputMode="decimal" value={form[k] ?? ""}
                        onChange={(e) => setForm({ ...form, [k]: e.target.value })} className={inputCls} />
                    </label>
                  ))}
                </div>
                <p className="text-[10px] text-ink-2">Numbers are per serving. Saving marks it confirmed.</p>
                <div className="flex gap-2">
                  <button className={`${btn} flex-1 bg-ink text-canvas`}
                    onClick={() => run(async () => {
                      const body = { servings: num(form.servings) ?? undefined };
                      for (const [k] of MACROS) body[k] = num(form[k]) ?? undefined;
                      await api.updateRecipe(r.recipe_id, body);
                      setEditing(null);
                    })}>SAVE</button>
                  <button className={`${btn} flex-1 border border-rule text-ink-2`}
                    onClick={() => setEditing(null)}>CANCEL</button>
                </div>
              </div>
            ) : (
              <div className="mt-1.5 flex flex-wrap gap-3 text-[11px]">
                <button className="text-[color:var(--hrv)] disabled:opacity-40" disabled={busy === r.recipe_id}
                  onClick={() => { setBusy(r.recipe_id); run(() => api.estimateRecipe(r.recipe_id)); }}>
                  {busy === r.recipe_id ? "Estimating…" : n ? "Re-estimate" : "Estimate"}
                </button>
                <button className="text-[color:var(--hrv)]"
                  onClick={() => {
                    setEditing(r.recipe_id);
                    setForm({ servings: s(n?.servings ?? r.servings),
                              ...Object.fromEntries(MACROS.map(([k]) => [k, s(n?.[k])])) });
                    setErr(null);
                  }}>{n ? "Edit / confirm" : "Set servings"}</button>
                <button className="text-ink-2"
                  onClick={() => run(() => api.updateRecipe(r.recipe_id, { hidden: true }))}>Hide</button>
              </div>
            )}
          </div>
        );
      })}
      {!shown.length && (
        <p className="text-[12px] text-ink-2">
          {needle || cat ? "No recipe matches." : "No recipes in the household app yet."}
        </p>
      )}
    </div>
  );
}

export default function MyFoodsModal({ onClose }) {
  const [tab, setTab] = useState("review");
  const { data: foods = [] } = useQuery({ queryKey: ["my-foods"], queryFn: () => api.listMyFoods() });
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-3"
      onClick={onClose}>
      <div className="my-4 w-full max-w-md space-y-3 rounded-2xl border border-rule bg-card p-4" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between">
          <h2 className="text-base font-semibold text-ink">My Foods</h2>
          <button onClick={onClose} className="px-2 text-lg leading-none text-ink-2">×</button>
        </div>
        <SegmentedTabs value={tab} onChange={setTab}
          options={[{ value: "review", label: "REVIEW" },
                    { value: "library", label: `LIBRARY ${foods.length}` },
                    { value: "recipes", label: "RECIPES" }]} />
        {tab === "review" ? <Review library={foods} />
          : tab === "library" ? <Library foods={foods} />
          : <Recipes />}
      </div>
    </div>
  );
}
