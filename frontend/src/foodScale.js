// Portion scaling for LogFoodModal items. Pure functions, so they can be checked
// outside React (moved out of the modal 2026-09-13; behaviour unchanged for scaleItem).

// Always scale from that item's own base values, never incrementally, so clearing
// and retyping a quantity works and no item can move another's numbers.
export const scaleItem = (it, newQtyStr) => {
  const base = it.base;
  const newQty = parseFloat(newQtyStr);
  const baseQty = base ? parseFloat(base.portionQty) : NaN;
  if (!base || isNaN(newQty) || newQty <= 0 || isNaN(baseQty) || baseQty <= 0) {
    return { ...it, portionQty: newQtyStr };
  }
  const r = newQty / baseQty;
  const sc = (v, dec) => v == null ? "" : dec ? String(+(v * r).toFixed(dec)) : String(Math.round(v * r));
  return { ...it, portionQty: newQtyStr,
    calories: sc(base.calories, 0), protein: sc(base.protein, 1),
    carbs: sc(base.carbs, 1), fat: sc(base.fat, 1), fiber: sc(base.fiber, 1),
    sugar: sc(base.sugar, 1), sodium: sc(base.sodium, 0) };
};

// TOPPINGS FOLLOW THEIR BASE (2026-09-13): half a bagel means half the cream
// cheese by default. Items expanded from a My Foods combo share a `comboGroup`; when
// the BASE's amount changes, every topping in the group is rescaled by the same ratio,
// measured from both items' ORIGINAL amounts (their `base` snapshots) — so typing
// through an empty or zero field mid-edit can neither compound nor lose the ratio.
// Editing a topping yourself detaches it (`followsBase: false`): a deliberate "extra
// cream cheese" is not overwritten by the next change to the bagel.
export function withToppingsFollowing(items, idx, newQtyStr) {
  const it = items[idx];
  const next = items.map((x, i) => (i === idx ? scaleItem(x, newQtyStr) : x));
  if (!it?.comboGroup) return next;
  if (it.comboPart === "topping") {
    return next.map((x, i) => (i === idx ? { ...x, followsBase: false } : x));
  }
  const newQ = parseFloat(newQtyStr);
  const q0 = parseFloat(it.base?.portionQty);
  if (!(newQ > 0) || !(q0 > 0)) return next;
  const r = newQ / q0;
  return next.map((x) => {
    if (x.comboGroup !== it.comboGroup || x.comboPart !== "topping" || x.followsBase === false) return x;
    const t0 = parseFloat(x.base?.portionQty);
    return t0 > 0 ? scaleItem(x, String(+(t0 * r).toFixed(3))) : x;
  });
}

// ── My Food cards (Phase 2, 2026-09-13) ──────────────────────────────────────
// A My Food item carries `per` (its numbers for ONE serving), `servingQty` /
// `servingUnit` (what one serving is) and `units` (alternates as servings-per-unit).
// Its amount lives in `servings`; the portion field shows it in the chosen unit.
const MF_MACROS = [["calories", "calories", 0], ["protein", "protein_g", 1], ["carbs", "carbs_g", 1],
                   ["fat", "fat_g", 1], ["fiber", "fiber_g", 1], ["sugar", "sugar_g", 1], ["sodium", "sodium_mg", 0]];

export const foodUnitOptions = (item) =>
  [item.servingUnit, ...(item.units || []).map((u) => u.label)].filter((v, i, a) => v && a.indexOf(v) === i);

export function qtyToServings(item, qty, unit) {
  const q = parseFloat(qty);
  if (!(q > 0)) return null;
  if (!unit || unit === item.servingUnit) return q / (item.servingQty || 1);
  const alt = (item.units || []).find((u) => u.label === unit);
  return alt ? q * alt.factor : null;
}

export function servingsToQty(item, servings, unit) {
  const alt = unit && unit !== item.servingUnit ? (item.units || []).find((u) => u.label === unit) : null;
  return alt ? servings / alt.factor : servings * (item.servingQty || 1);
}

export function withServings(item, servings, unit = item.portionUnit, qtyStr = null) {
  const out = { ...item, servings, portionUnit: unit,
    portionQty: qtyStr ?? String(+servingsToQty(item, servings, unit).toFixed(3)) };
  for (const [k, src, d] of MF_MACROS) {
    const v = item.per?.[src];
    out[k] = v == null ? "" : d ? String(+(v * servings).toFixed(d)) : String(Math.round(v * servings));
  }
  return out;
}

// Set a My Food item's servings within the meal. A BASE's toppings follow it at their
// per-base ratio (`perBase`: topping servings per ONE base serving); setting a topping
// yourself detaches it — the same rule as withToppingsFollowing, in servings.
export function setServingsInMeal(items, idx, servings, unit, qtyStr = null) {
  const it = items[idx];
  const next = items.map((x, i) => (i === idx ? withServings(x, servings, unit ?? x.portionUnit, qtyStr) : x));
  if (!it?.comboGroup) return next;
  if (it.comboPart === "topping") return next.map((x, i) => (i === idx ? { ...x, followsBase: false } : x));
  return next.map((x) => (x.comboGroup === it.comboGroup && x.comboPart === "topping"
    && x.followsBase !== false && x.perBase > 0 && x.per)
    ? withServings(x, +(x.perBase * servings).toFixed(3)) : x);
}

// Give each topping in a parsed combo its per-base ratio, from the servings it arrived with.
export function linkToppings(items) {
  const bases = {};
  for (const x of items) if (x.comboGroup && x.comboPart === "base") bases[x.comboGroup] = x;
  return items.map((x) => (x.comboPart === "topping" && x.comboGroup && x.perBase == null
    && bases[x.comboGroup]?.servings > 0 && x.servings > 0)
    ? { ...x, perBase: x.servings / bases[x.comboGroup].servings } : x);
}
