import { useState, useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, apiErrorText } from "../api.js";
import {
  scaleItem, withToppingsFollowing, foodUnitOptions, qtyToServings, withServings,
  setServingsInMeal, linkToppings,
} from "../foodScale.js";
import { DRINK_KINDS, isAlcoholic, isDrink } from "../drinks.js";
import { shrinkImage } from "../imageUtil.js";
import { BrandTag } from "./ui.jsx";

// Mirrors backend food_log._MEAL_SLOT — the default when no time is entered.
const MEAL_DEFAULT_TIME = { breakfast: "8:00 AM", lunch: "12:00 PM", dinner: "7:00 PM", dessert: "8:00 PM" };
const localTodayStr = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};

const numOr = (v) => (v === "" || v == null || Number.isNaN(parseFloat(v)) ? null : parseFloat(v));

// "Make this a My Food" on an existing entry (Phase 2): one serving = this entry's
// portion, at its numbers, and the entry is linked to the new food.
function MakeMyFood({ logId }) {
  const qc = useQueryClient();
  const [state, setState] = useState({});
  const go = async () => {
    setState({ busy: true });
    try {
      const f = await api.myFoodFromLog(logId);
      qc.invalidateQueries({ queryKey: ["my-foods"] });
      setState({ ok: f.name });
    } catch (e) { setState({ err: apiErrorText(e) }); }
  };
  return (
    <div className="rounded-xl border border-rule bg-surface px-3 py-2">
      <div className="flex items-center justify-between">
        <span className="text-[11px] text-ink-3">My Foods</span>
        {state.ok
          ? <span className="text-[11px] text-ink-2">✓ Added as “{state.ok}”</span>
          : <button onClick={go} disabled={state.busy}
              className="rounded-lg border border-rule px-3 py-1 text-[11px] text-ink-2 disabled:opacity-40">
              ☆ Make this a My Food
            </button>}
      </div>
      {state.err && <p className="mt-1 text-[11px]" style={{ color: "var(--over)" }}>{state.err}</p>}
    </div>
  );
}

const MEALS = ["breakfast", "lunch", "dinner", "dessert", "snack", "drinks"];
const MEAL_LABELS = { breakfast: "Breakfast", lunch: "Lunch", dinner: "Dinner", dessert: "Dessert", snack: "Snack", drinks: "Drinks" };

// ── Protein powder add-on ─────────────────────────────────────────────────────
//
// Per-scoop macros for the powder in use (31 g scoop). Kept as a constant rather
// than parsed from text: the label is exact, so sending it through the LLM parser
// would replace a known number with an estimate and add drift for nothing.
//
// FOLDED INTO THE MEAL when there is one; logged on its own when there is not.
// A smoothie with a scoop in it is one food you ate, and splitting it made the
// day's log read as two things.
//
// The two hazards of folding in are handled rather than avoided:
//   • Portion scaling — the macros are added at SAVE, not into the visible
//     fields. Those scale against baseMacros, so folding in earlier would make
//     a "2 servings" meal silently double the powder along with the food.
//   • Tagging — food_tags reads the item text, so the scoop is appended to the
//     description. Macros with no textual trace is how a tag ends up describing
//     the logging channel instead of the food.
const POWDER = { name: "Protein powder", grams: 31,
                 calories: 120, protein_g: 24, carbs_g: 3, fat_g: 1 };
const SCOOP_OPTIONS = [0, 0.5, 1, 1.5, 2];

export default function LogFoodModal({
  onClose,
  defaultDate,
  defaultMeal = "breakfast",
  onSaved,
  existingLog = null,   // pass to open in edit mode
  job = null,           // a finished background parse to review (2026-10-21)
}) {
  const isEdit = existingLog != null;

  const [logDate,     setLogDate]     = useState(defaultDate);
  // Meal TIME. Empty means "don't know": the backend uses the meal's default slot
  // (breakfast 8, lunch 12, dinner 7 pm, dessert 8 pm) or, for a snack/drink, now today
  // and 3 pm on a past day — always flagged as estimated (see MEAL_DEFAULT_TIME).
  const [logTime,     setLogTime]     = useState(
    existingLog?.timestamp ? String(existingLog.timestamp).slice(11, 16) : "");
  const [mealType,    setMealType]    = useState(defaultMeal);
  const [description, setDescription] = useState("");
  const [parsing,     setParsing]     = useState(false);
  const [parseError,  setParseError]  = useState(null);
  // Set when /parse answered from your own log instead of the model (instant repeat).
  const [cachedFrom,  setCachedFrom]  = useState(null);
  const [refinement,  setRefinement]  = useState("");
  // The most recent parse: its id and the text it interpreted. "Add detail" and
  // "Re-estimate" act on that parse's items only (v2, 2026-10-21: the box adds to the
  // tray instead of being the interpretation of the whole meal).
  const [lastParse,   setLastParse]   = useState(null);
  const parseGid = useRef(0);

  // ONE ENTRY PER FOOD. A parse now returns an array — "a sub and a bag of chips"
  // is two items, not one summed row — so the whole result section is a list.
  // A single food is a list of one, which is why there is no separate scalar path.
  const [items, setItems] = useState([]);
  // {key, at} of the item a repeat pick just bumped — drives a brief highlight so
  // the tap is visibly acknowledged. Cleared on a timer; see applyRecent.
  const [bumped, setBumped] = useState(null);
  const hasParsed = items.length > 0;
  const [openFields, setOpenFields] = useState(null);   // index whose raw fields are open
  const [scoops, setScoops] = useState(0);   // protein powder add-on
  const [saving,      setSaving]      = useState(false);
  const [saveError,   setSaveError]   = useState(null);
  const [recentMeals, setRecentMeals] = useState([]);
  // OPEN BY DEFAULT. 62% of entries are an exact repeat of something already
  // logged (643 entries over Jun-Aug, 247 distinct descriptions), so re-picking
  // is the majority path and typing is the exception — but the picker used to
  // start collapsed, putting the common case behind an extra tap and the rare
  // one in front. Costs nothing to open: the fetch below runs on mount either
  // way, so this changes what you see, not what is requested.
  // Collapsed on open (2026-09-13), so the description box is visible first.
  const [showRecents, setShowRecents] = useState(false);
  // Recent list order (2026-10-21): newest first by default, or most frequent.
  // Remembered per device; localStorage can throw in private mode, hence the guards.
  const [recentSort, setRecentSortState] = useState(() => {
    try { return localStorage.getItem("logfood_recent_sort") || "recent"; } catch { return "recent"; }
  });
  const setRecentSort = (v) => {
    setRecentSortState(v);
    try { localStorage.setItem("logfood_recent_sort", v); } catch { /* private mode */ }
  };
  const [drinkKind, setDrinkKind] = useState(null);   // Drinks meal: My Foods by kind (note 34)
  // My Recipes (note 37) — separate from My Foods on purpose; starts collapsed like it.
  const [showRecipes, setShowRecipes] = useState(false);
  const [recipeQuery, setRecipeQuery] = useState("");
  const [showAllRecipes, setShowAllRecipes] = useState(false);
  const [estimatingId, setEstimatingId] = useState(null);
  const [recipeMsg, setRecipeMsg] = useState(null);
  const [scanning,    setScanning]    = useState(false);
  const [labelError,  setLabelError]  = useState(null);

  // Stable React keys. Items have no id until they are saved, and index keys would
  // re-associate every row's inputs when one is removed from the middle.
  const nextKey = useRef(0);

  // ── My Foods (Phase 2) ──────────────────────────────────────────────────────
  const qc = useQueryClient();
  const { data: library = [] } = useQuery({ queryKey: ["my-foods"], queryFn: () => api.listMyFoods() });
  const { data: brandOptions = [] } = useQuery({ queryKey: ["food-brands"], queryFn: () => api.getBrands() });
  const libById = Object.fromEntries(library.map((f) => [f.id, f]));
  // Household-app recipes with a per-serving estimate (note 37). Only ones that HAVE
  // numbers can be logged; the rest are waiting to be estimated in My Foods → RECIPES.
  const { data: recipeData, error: recipeLoadError } = useQuery({
    queryKey: ["recipes"], queryFn: () => api.listRecipes(), enabled: !isEdit,
  });
  // ALL recipes, estimated or not (fixed 2026-09-16). Filtering to estimated ones hid the
  // whole section until something had been estimated — and the only place to estimate was
  // the My Foods modal, so the entry point never appeared at all. Un-estimated rows say so
  // and estimate on tap.
  const recipes = recipeData?.recipes ?? [];
  // Why the list is empty when it is not supposed to be. The button stays visible with this
  // in its panel (2026-10-29): it used to hide on `recipes.length > 0`, so when the household
  // app dropped a column and /api/recipes returned 500, the entry point vanished silently.
  const recipesDown = recipeLoadError ? `Couldn't load your recipes — ${apiErrorText(recipeLoadError)}`
    : recipeData?.available === false ? (recipeData.detail || "The household recipes aren't available.")
    : null;
  // "Same as last <meal>" (#22): the most recent earlier day's entries for this meal.
  const { data: lastMeal } = useQuery({
    queryKey: ["last-meal", mealType, logDate],
    queryFn: () => api.getLastMeal(mealType, logDate),
    enabled: !isEdit && !!logDate, staleTime: 5 * 60 * 1000,
  });
  const [lastMealAdded, setLastMealAdded] = useState(null);   // "meal|date" already added
  const recipeServings = (r) => r.nutrition?.servings ?? r.servings ?? null;
  const [showAllFoods, setShowAllFoods] = useState(false);
  const [myFoodMsg, setMyFoodMsg] = useState({});        // item key -> message
  // "Save these as one My Food" (2026-11-12): null = closed, else {name, off: Set of keys}.
  const [comboDraft, setComboDraft] = useState(null);
  const [comboMsg, setComboMsg] = useState(null);
  // A card built from the library's own numbers: servings stepper, unit switch.
  const itemFromFood = (food, servings = 1, extras = {}) => withServings({
    key: nextKey.current++, name: food.name, brand: food.brand ?? "",
    description: food.brand ? `${food.brand} ${food.name}` : food.name,
    portionDesc: food.serving_label ?? "", portionQty: "", portionUnit: food.serving_unit,
    aiNotes: `From My Foods · 1 serving = ${food.serving_label || `${food.serving_qty} ${food.serving_unit}`}`,
    confidence: "high", omitted: null, options: null, base: null,
    myFoodId: food.id, servingQty: food.serving_qty, servingUnit: food.serving_unit, units: food.units || [],
    per: { calories: food.calories, protein_g: food.protein_g, carbs_g: food.carbs_g, fat_g: food.fat_g,
           fiber_g: food.fiber_g, sugar_g: food.sugar_g, sodium_mg: food.sodium_mg },
    ...extras,
  }, servings, food.serving_unit);

  // Each item keeps its OWN `base` snapshot. Portion scaling always computes ratios
  // against that, never against the previous input value, so clearing and retyping a
  // quantity cannot drift — and one item's portion can no longer move another's.
  // A row that IS a My Food (a parsed / cached match, or an entry being edited) becomes
  // a My Food card built from the library, so its stepper and toppings work.
  const toItem = (src) => {
    const food = src.my_food_id != null ? libById[src.my_food_id] : null;
    if (food && food.role !== "combo") {
      return itemFromFood(food, src.servings ?? 1, {
        name: src.name || food.name, brand: src.brand ?? food.brand ?? "",
        comboGroup: src.combo_group ?? null, comboPart: src.combo_part ?? null,
        comboName: src.combo_name ?? null });
    }
    return plainItem(src);
  };
  const plainItem = (src) => ({
    key:         nextKey.current++,
    name:        src.name ?? "",
    brand:       src.brand ?? "",
    description: src.description ?? "",
    portionDesc: src.portion_description ?? "",
    portionQty:  src.portion_quantity == null ? "" : String(src.portion_quantity),
    portionUnit: src.portion_unit ?? "",
    aiNotes:     src.notes ?? src.ai_notes ?? "",
    // My Foods combo parts (bagel + cream cheese): toppings follow their base.
    comboGroup:  src.combo_group ?? null,
    comboPart:   src.combo_part ?? null,
    comboName:   src.combo_name ?? null,
    recipeId:    src.recipe_id ?? null,
    calories:    src.calories   == null ? "" : String(src.calories),
    protein:     src.protein_g  == null ? "" : String(src.protein_g),
    carbs:       src.carbs_g    == null ? "" : String(src.carbs_g),
    fat:         src.fat_g      == null ? "" : String(src.fat_g),
    fiber:       src.fiber_g    == null ? "" : String(src.fiber_g),
    sugar:       src.sugar_g    == null ? "" : String(src.sugar_g),
    sodium:      src.sodium_mg  == null ? "" : String(src.sodium_mg),
    // Parser self-reporting. `options` is null for anything pinned, so the size
    // chooser simply does not render for the majority of items.
    confidence:  src.confidence ?? null,
    omitted:     src.omitted_kcal ?? null,
    options:     src.portion_options ?? null,
    // Doses the parser found in a drink (note 100). Saved as the caffeine / alcohol
    // entry this row links to; scaled with the amount (doseFactor); ✕ drops one.
    caffeineMg:  src.caffeine_mg ?? null,
    alcAbv:      src.alcohol_abv ?? null,
    alcOz:       src.alcohol_oz ?? null,
    // Note 84: bought prepared (restaurant, cafe, bar, food truck). The parser's guess or
    // the recent's own answer; null = unknown, and the server then reuses the last answer
    // for the same food.
    restaurant:  typeof src.restaurant === "boolean" ? src.restaurant : null,
    base: {
      portionQty: src.portion_quantity ?? null,
      calories:   src.calories   ?? null,
      protein:    src.protein_g  ?? null,
      carbs:      src.carbs_g    ?? null,
      fat:        src.fat_g      ?? null,
      fiber:      src.fiber_g    ?? null,
      sugar:      src.sugar_g    ?? null,
      sodium:     src.sodium_mg  ?? null,
    },
  });

  // Choosing a size REPLACES the item's base snapshot rather than nudging its
  // visible numbers. Every later portion edit scales from whatever size you picked,
  // which is the only version that stays correct if you then type "1.5" — nudging
  // the fields alone would leave `base` describing a size you rejected.
  const applyOption = (idx, opt) =>
    setItems(prev => prev.map((it, i) => i === idx ? {
      ...it,
      portionQty:  opt.portion_quantity == null ? it.portionQty : String(opt.portion_quantity),
      calories:    opt.calories   == null ? "" : String(Math.round(opt.calories)),
      protein:     opt.protein_g  == null ? "" : String(opt.protein_g),
      carbs:       opt.carbs_g    == null ? "" : String(opt.carbs_g),
      fat:         opt.fat_g      == null ? "" : String(opt.fat_g),
      fiber:       opt.fiber_g    == null ? "" : String(opt.fiber_g),
      sugar:       opt.sugar_g    == null ? "" : String(opt.sugar_g),
      sodium:      opt.sodium_mg  == null ? "" : String(opt.sodium_mg),
      chosen:      opt.label,
      // The parser's confidence described its own guess at the size. You have now
      // told it the size, so that assessment no longer applies to this number.
      confidence:  "high",
      base: {
        portionQty: opt.portion_quantity ?? null,
        calories:   opt.calories   ?? null,
        protein:    opt.protein_g  ?? null,
        carbs:      opt.carbs_g    ?? null,
        fat:        opt.fat_g      ?? null,
        fiber:      opt.fiber_g    ?? null,
        sugar:      opt.sugar_g    ?? null,
        sodium:     opt.sodium_mg  ?? null,
      },
    } : it));

  const patchItem = (idx, patch) =>
    setItems(prev => prev.map((it, i) => (i === idx ? { ...it, ...patch } : it)));

  // Fetch the meal picker in create mode. Refetches on meal-type change and on
  // search, so the server's ranking (recency-decayed frequency)
  // does the work rather than filtering a stale client-side list. Debounced —
  // this fires on every keystroke otherwise.
  // v2: the ONE box is also the search, so its text drives this query.
  const boxQuery = isEdit ? "" : description;
  useEffect(() => {
    if (isEdit) return;
    const t = setTimeout(() => {
      const query = boxQuery.trim();
      api.getRecentMeals({
        limit: 40,
        q: query || null,
        // A search spans every meal type; browsing scopes to the meal you're logging.
        meal_type: query ? null : mealType,
        sort: recentSort,
      }).then(setRecentMeals).catch(() => {});
    }, boxQuery ? 250 : 0);
    return () => clearTimeout(t);
  }, [isEdit, mealType, boxQuery, recentSort]);

  // Pre-populate when editing an existing entry. Edit mode is ALWAYS exactly one
  // item: it edits one existing row, and splitting there would silently turn one
  // stored entry into several.
  useEffect(() => {
    if (!isEdit) return;
    setDescription(existingLog.description || "");
    setMealType(existingLog.meal_type || "snack");
    setItems([toItem(existingLog)]);
    setOpenFields(0);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  // Identity for the double-tap guard. The description is what a picked row copies
  // verbatim, so two taps of the same row always match on it; name is the fallback
  // for a row that somehow carries no description.
  const itemIdentity = (src) =>
    (src.description || "").trim().toLowerCase() || (src.name || "").trim().toLowerCase();

  // A picked recent APPENDS rather than replaces, so a multi-item meal can be
  // assembled by tapping several staples in a row — which is the whole point of
  // one row per food. Costs no API call.
  //
  // Tapping the SAME row twice bumps that item's quantity instead of adding a
  // second card. Two protein bars is one food at quantity 2, which is the parser's
  // own splitting rule ("two bagels is one item, quantity 2") — and it closes the
  // gap this screen opened: 33 duplicate pairs were already in the log from
  // double-taps under the old replace-on-tap UI, and appending makes that reflex
  // additive rather than harmless. Nothing is taken away; the quantity is visible
  // and editable, and a third tap still gets you three.
  const applyRecent = (meal) => {
    const id = itemIdentity(meal);
    const at = items.findIndex(it => itemIdentity(it) === id);
    if (at >= 0) {
      const it = items[at];
      // Step by the item's OWN base quantity, so "1 piece" steps to 2 and "50 g"
      // steps to 100 — a fixed +1 would turn a 50 g portion into 51 g.
      const step = parseFloat(it.base?.portionQty) || 1;
      const next = +(((parseFloat(it.portionQty) || 0) + step).toFixed(3));
      setItems(prev => prev.map((x, i) => (i === at ? scaleItem(x, String(next)) : x)));
      // The bumped card may be scrolled out of view, and a tap with no visible
      // effect is what produces the third tap. Flash it. Keyed by timestamp so
      // repeat taps on the same item re-trigger the highlight.
      setBumped({ key: it.key, at: Date.now() });
      return;
    }
    setItems(prev => [...prev, toItem(meal)]);
    setDescription("");                        // v2: a pick clears the box for the next one
    setMealType(meal.meal_type || mealType);
  };

  // Downscale before upload: a modern phone photo is 3-8 MB, which is slow on
  // mobile data and buys nothing — a nutrition panel is legible well under 1600px.
  // Also normalises HEIC/PNG to JPEG, which the API accepts.
  // shrinkImage moved to ../imageUtil.js (2026-09-15) so the My Food form's scanner
  // (note 40) uses the same one.

  const handleLabelPhoto = async (file) => {
    setLabelError(null);
    setScanning(true);
    try {
      const blob = await shrinkImage(file);
      const d = await api.parseNutritionLabel(blob);
      const label = d.name || "Scanned nutrition label";
      // Label values are PER SERVING. Anchoring the portion at 1 serving lets the
      // existing scaling handle "I ate two" without re-reading the label.
      // Appended, like a picked recent: a scanned bar can be one item of a meal.
      setItems(prev => [...prev, { ...toItem({
        name: d.name, description: label,
        portion_quantity: 1, portion_unit: "serving",
        portion_description: d.serving_size || "",
        calories: d.calories, protein_g: d.protein_g, carbs_g: d.carbs_g,
        fat_g: d.fat_g, fiber_g: d.fiber_g, sugar_g: d.sugar_g, sodium_mg: d.sodium_mg,
        notes: d.serving_size
          ? `From label · 1 serving = ${d.serving_size}`
            + (d.servings_per_container ? ` · ${d.servings_per_container} per container` : "")
          : "From nutrition label",
      }), fromLabel: true }]);
    } catch (e) {
      setLabelError(e.message || "Could not read that label");
    } finally {
      setScanning(false);
    }
  };

  // A parse REPLACES the list — it is the interpretation of the whole description
  // box, so merging it into whatever was there would double foods already listed.
  // (Tapping a recent appends; parsing does not.)
  // v2 (2026-10-21): a parse ADDS its items to the tray and clears the box, like a
  // pick — the box is for the next thing, not the interpretation of the whole meal.
  // `replaceGroup` swaps one earlier parse's items in place (Add detail / Re-estimate).
  // Edit mode still holds exactly one item, so there a parse replaces the list.
  const applyParsed = (data, { text = description, replaceGroup = null } = {}) => {
    const gid = replaceGroup ?? `p${parseGid.current++}`;
    // `fromParse` marks items that came from parsing the typed text, so only a
    // pure parse is remembered for that text (see source_text on save).
    const fresh = linkToppings((data.items || []).map((d) => ({ ...toItem(d), fromParse: true, parseGroup: gid })));
    if (isEdit) setItems(fresh);
    else setItems((prev) => {
      if (replaceGroup == null) return [...prev, ...fresh];
      const at = prev.findIndex((it) => it.parseGroup === replaceGroup);
      const rest = prev.filter((it) => it.parseGroup !== replaceGroup);
      const pos = at < 0 ? rest.length : Math.min(at, rest.length);
      return [...rest.slice(0, pos), ...fresh, ...rest.slice(pos)];
    });
    setCachedFrom(data.cached ?? null);
    setLastParse({ gid, text });
    setOpenFields(null);
    if (!isEdit) setDescription("");
  };

  // scaleItem and the topping-follows-base rule live in ../foodScale.js (pure, testable).

  // Toppings follow their base by default — see foodScale.withToppingsFollowing. A My
  // Food card's amount is in servings (converted through its unit), everything else
  // keeps the original portion scaling.
  const handlePortionChange = (idx, newQtyStr) =>
    setItems(prev => {
      const it = prev[idx];
      if (it?.myFoodId && it.per) {
        const sv = qtyToServings(it, newQtyStr, it.portionUnit);
        return sv == null
          ? prev.map((x, i) => (i === idx ? { ...x, portionQty: newQtyStr } : x))
          : setServingsInMeal(prev, idx, sv, it.portionUnit, newQtyStr);
      }
      return withToppingsFollowing(prev, idx, newQtyStr);
    });
  // Half-serving steps (user: ½ steps plus a tap-to-type field).
  const stepServings = (idx, delta) =>
    setItems(prev => {
      const it = prev[idx];
      const sv = Math.max(0.5, Math.round(((it.servings ?? 1) + delta) * 2) / 2);
      return setServingsInMeal(prev, idx, sv, it.portionUnit);
    });
  const changeUnit = (idx, unit) =>
    setItems(prev => setServingsInMeal(prev, idx, prev[idx].servings ?? 1, unit));

  // Add every item of the last <meal> at once, same portions and links. Combo groups
  // get fresh ids so a repeated burger does not join yesterday's stored group.
  const addLastMeal = () => {
    const rows = lastMeal?.items ?? [];
    if (!rows.length) return;
    const regroup = {};
    const made = rows.map((r) => {
      const it = toItem(r);
      if (it.comboGroup) it.comboGroup = (regroup[it.comboGroup] ??= `r${nextKey.current++}`);
      return it;
    });
    setItems((prev) => [...prev, ...linkToppings(made)]);
    setLastMealAdded(`${mealType}|${lastMeal.date}`);
    setDescription("");
  };

  // Tap a My Food in the picker. A combination adds its parts — the first non-topping
  // part is the base and every other part follows it; a food already on the list
  // (outside a combo) gets another serving instead of a second card.
  // One tap = ONE SERVING of the recipe. Portion editing on the card then scales it, so
  // "I had two" needs no second lookup — same as a label-scanned item.
  const addRecipe = async (r) => {
    setRecipeMsg(null);
    let n = r.nutrition;
    // Not estimated yet: do it here rather than sending the user to another screen. It is
    // one model call, and the tap is the consent.
    if (n?.calories == null) {
      // No servings is fine (fixed 2026-09-22): the estimator works from ingredient
      // weights and estimates the yield itself.
      try {
        setEstimatingId(r.recipe_id);
        n = (await api.estimateRecipe(r.recipe_id))?.nutrition;
        qc.invalidateQueries({ queryKey: ["recipes"] });
      } catch (e) {
        setRecipeMsg(apiErrorText(e));
        return;
      } finally {
        setEstimatingId(null);
      }
    }
    if (!n || n.calories == null) {
      setRecipeMsg(`Could not work out the nutrition for "${r.title}".`);
      return;
    }
    setItems((prev) => [...prev, {
      ...plainItem({
        name: r.title, description: r.title,
        portion_quantity: 1, portion_unit: "serving",
        calories: n.calories, protein_g: n.protein_g, carbs_g: n.carbs_g, fat_g: n.fat_g,
        fiber_g: n.fiber_g, sugar_g: n.sugar_g, sodium_mg: n.sodium_mg,
        recipe_id: r.recipe_id,
        notes: `From My Recipes: ${r.title} — per serving, ${n.source === "confirmed" ? "checked by you" : "estimated from the ingredients"}`
               + (n.servings ? ` (recipe makes ${n.servings})` : "") + ".",
      }),
      confidence: n.source === "confirmed" ? "high" : "medium",
    }]);
    if (n.meal_type && !hasParsed) setMealType(n.meal_type);
  };

  const addMyFood = (food) => {
    if (food.role === "combo") {
      const parts = (food.components || []).map((c) => ({ f: libById[c.my_food_id], s: c.servings })).filter((p) => p.f);
      if (!parts.length) return;
      const bi = Math.max(0, parts.findIndex((p) => p.f.role !== "topping"));
      const ordered = [parts[bi], ...parts.filter((_, i) => i !== bi)];
      const g = `c${food.id}-${nextKey.current}`;
      const made = ordered.map((p, i) => itemFromFood(p.f, p.s, {
        comboGroup: g, comboPart: i === 0 ? "base" : "topping", comboName: food.name,
        ...(i ? { perBase: p.s / ordered[0].s } : {}) }));
      setItems(prev => [...prev, ...made]);
    } else {
      const at = items.findIndex((it) => it.myFoodId === food.id && !it.comboGroup);
      if (at >= 0) {
        stepServings(at, 1);
        setBumped({ key: items[at].key, at: Date.now() });
        return;
      }
      setItems(prev => [...prev, itemFromFood(food, 1)]);
    }
    setDescription("");                        // v2: a pick clears the box for the next one
  };

  // A "usually with" chip: the topping joins the base's group and follows its amount
  // (one topping serving per base serving), placed right under the base's group.
  const addTopping = (idx, topFood) =>
    setItems(prev => {
      const base = prev[idx];
      const g = base.comboGroup || `u${base.key}`;
      const grouped = prev.map((x, i) => (i === idx ? { ...x, comboGroup: g, comboPart: "base" } : x));
      const t = itemFromFood(topFood, base.servings ?? 1, { comboGroup: g, comboPart: "topping", perBase: 1 });
      let at = idx + 1;
      while (at < grouped.length && grouped[at].comboGroup === g) at++;
      return [...grouped.slice(0, at), t, ...grouped.slice(at)];
    });

  // "☆ Save as My Food" on a parsed / scanned / typed card: one serving = its current
  // portion, at its current numbers. The card then becomes that My Food.
  const saveAsMyFood = async (idx) => {
    const it = items[idx];
    const qty = parseFloat(it.portionQty) || 1;
    try {
      const f = await api.createMyFood({
        name: (it.name || it.description || "").trim(), role: "food", meal_type: mealType,
        brand: (it.brand || "").trim() || null,
        serving_qty: qty, serving_unit: it.portionUnit || "serving",
        serving_label: it.portionDesc || `${qty} ${it.portionUnit || "serving"}`,
        calories: numOr(it.calories), protein_g: numOr(it.protein), carbs_g: numOr(it.carbs),
        fat_g: numOr(it.fat), fiber_g: numOr(it.fiber), sugar_g: numOr(it.sugar), sodium_mg: numOr(it.sodium),
        source: it.fromLabel ? "label" : "confirmed",
      });
      qc.invalidateQueries({ queryKey: ["my-foods"] });
      setItems(prev => prev.map((x, i) => (i === idx ? itemFromFood(f, 1, { key: x.key }) : x)));
      setMyFoodMsg(m => ({ ...m, [it.key]: "Saved to My Foods." }));
    } catch (e) {
      setMyFoodMsg(m => ({ ...m, [it.key]: apiErrorText(e) }));
    }
  };

  // The tray's pieces → one My Food combination. Same server call as Today's "+ My Food":
  // a piece that is already a My Food is reused, a name the library knows is reused at
  // servings from the calories, anything else becomes a new estimate-sourced food. The
  // tray items are then linked to their parts and grouped, so they log as the dish.
  const comboable = items.filter((x) => !x.caffeineMg && !(x.alcAbv && x.alcOz));
  const saveTrayCombo = async () => {
    const chosen = comboable.filter((x) => !comboDraft.off.has(x.key));
    const name = comboDraft.name.trim();
    try {
      const r = await api.comboFromItems({
        name, meal_type: mealType, regroup: false,
        items: chosen.map((it) => ({
          my_food_id: it.myFoodId ?? null, servings: it.myFoodId ? (it.servings ?? 1) : null,
          name: (it.name || "").trim() || null, brand: (it.brand || "").trim() || null,
          description: it.description || null,
          portion_quantity: numOr(it.portionQty), portion_unit: it.portionUnit || null,
          calories: numOr(it.calories), protein_g: numOr(it.protein), carbs_g: numOr(it.carbs),
          fat_g: numOr(it.fat), fiber_g: numOr(it.fiber), sugar_g: numOr(it.sugar), sodium_mg: numOr(it.sodium),
        })),
      });
      qc.invalidateQueries({ queryKey: ["my-foods"] });
      const byKey = Object.fromEntries(chosen.map((it, i) => [it.key, r.items[i]]));
      const g = `tray-${Date.now()}`;
      setItems((prev) => prev.map((x) => (byKey[x.key] ? {
        ...x, myFoodId: byKey[x.key].my_food_id, servings: byKey[x.key].servings,
        comboGroup: g, comboPart: byKey[x.key].part, comboName: name,
        // These pieces keep their own amounts (no library serving to scale by), so the
        // topping line must not claim they follow the base.
        followsBase: false,
      } : x)));
      setComboDraft(null);
      setComboMsg(`✓ “${r.combo.name}” saved to My Foods — these will log as it.`);
    } catch (e) {
      setComboMsg(apiErrorText(e));
    }
  };

  useEffect(() => {
    if (!bumped) return;
    const t = setTimeout(() => setBumped(null), 1300);
    return () => clearTimeout(t);
  }, [bumped]);

  // Reopened from a finished background parse: restore what was typed and the parsed
  // items, exactly as if Parse had just returned.
  useEffect(() => {
    if (!job?.result) return;
    setMealType(job.meal_type);
    setLogDate(job.date);
    setLogTime(job.time || "");
    applyParsed(job.result, { text: job.description });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.id]);

  // Hand the parse to the server and close: the result waits in the LOG view's review
  // tray, so a slow model call never holds the screen (and survives iOS killing the app).
  const [bgBusy, setBgBusy] = useState(false);
  const parseInBackground = async () => {
    setBgBusy(true);
    setParseError(null);
    try {
      await api.createParseJob(description, mealType, logDate, logTime || null);
      qc.invalidateQueries({ queryKey: ["parse-jobs"] });
      onClose();
    } catch (e) {
      setParseError(apiErrorText(e));
      setBgBusy(false);
    }
  };

  const runParse = async (desc, fresh = false, replaceGroup = null) => {
    setParsing(true);
    setParseError(null);
    try {
      const data = await api.parseFood(desc, mealType, fresh);
      applyParsed(data, { text: replaceGroup ? (lastParse?.text ?? desc) : desc, replaceGroup });
      setRefinement("");
    } catch (e) {
      // Offline (src/offline.js): parsing needs the server, but a background parse is
      // queued like any other write and runs when the connection is back.
      const offline = (typeof navigator !== "undefined" && navigator.onLine === false)
        || /offline|Failed to fetch|Load failed|NetworkError/i.test(String(e?.message ?? e));
      setParseError(offline
        ? "You're offline — tap \"Parse in background\" and it'll parse once you're back online."
        : "Parse failed — check that AI is enabled in Settings");
    } finally {
      setParsing(false);
    }
  };

  const handleRefine = () => {
    if (!refinement.trim() || !lastParse) return;
    runParse(`${lastParse.text}. Additional detail: ${refinement}`, true, lastParse.gid);
  };

  const handleSave = async () => {
    if (items.some((it) => it.fromLabel && !(it.name || "").trim())) {
      setSaveError("Name the scanned item before saving.");
      return;
    }
    setSaving(true);
    setSaveError(null);
    const num = (v) => (v !== "" && v != null ? parseFloat(v) : null);
    const scoopText = `${scoops} scoop${scoops === 1 ? "" : "s"} protein powder `
                    + `(${Math.round(POWDER.grams * scoops)} g)`;
    let powderFolded = false;
    try {
      if (isEdit) {
        const it = items[0];
        await api.updateNutritionLog(existingLog.id, {
          name:        it.name || null,
          brand:       (it.brand || "").trim(),      // "" clears it
          description: it.description || description,
          portion_quantity: num(it.portionQty),
          portion_unit: it.portionUnit || null,
          calories:    num(it.calories),
          protein_g:   num(it.protein),
          carbs_g:     num(it.carbs),
          fat_g:       num(it.fat),
          fiber_g:     num(it.fiber),
          sugar_g:     num(it.sugar),
          sodium_mg:   num(it.sodium),
          ai_notes:    it.aiNotes || null,
          at_time:     logTime || null,
          meal_type:   mealType,
          ...(it.myFoodId ? { servings: it.servings ?? null } : {}),
          ...(it.restaurant != null ? { restaurant: it.restaurant } : {}),
        });
      } else if (hasParsed) {
        // Powder FOLDED INTO THE FIRST ITEM, unchanged in spirit from when there was
        // only ever one: a scoop in a smoothie is part of that smoothie, not a food
        // of its own. First item = the thing it went into. Still applied at SAVE and
        // not into the visible fields, which is what keeps it out of portion scaling
        // — those scale against the item's base, so folding in earlier would make a
        // "2 servings" item silently double the powder along with the food.
        // An alcoholic My Food goes through the ALCOHOL logger (note 34): one call writes
        // its standard drinks and its Drinks calories row, exactly as the alcohol sheet
        // does. Saved as plain food it would carry calories and no alcohol at all — how
        // typed drinks used to go uncounted.
        const isAlcItem = (it) => it.myFoodId && isAlcoholic(libById[it.myFoodId]);
        const drinks = items.filter(isAlcItem);
        const rest = items.filter((it) => !isAlcItem(it));
        for (const it of drinks) {
          await api.logAlcohol({ date: logDate, at_time: logTime || null,
                                 my_food_id: it.myFoodId, servings: it.servings ?? 1 });
        }
        const payload = rest.map((it, i) => {
          const withPowder = scoops > 0 && i === 0;
          const add = (v, per) => {
            const base = num(v);
            if (!withPowder) return base;
            return +(((base ?? 0) + per * scoops).toFixed(1));
          };
          return {
            name:        it.name || null,
            brand:       (it.brand || "").trim() || null,
            // Appended to the description so food_tags, which reads the item text,
            // can still see the powder. Macros with no textual trace is exactly how
            // a tag ends up describing the logging channel rather than the food.
            // A named label item is described by its name, not "Scanned nutrition label".
            description: withPowder
              ? `${(it.fromLabel && (it.name || "").trim()) || it.description} + ${scoopText}`
              : ((it.fromLabel && (it.name || "").trim()) || it.description),
            portion_quantity: num(it.portionQty),
            portion_unit: it.portionUnit || null,
            calories:    withPowder
              ? Math.round((num(it.calories) ?? 0) + POWDER.calories * scoops)
              : num(it.calories),
            protein_g:   add(it.protein, POWDER.protein_g),
            carbs_g:     add(it.carbs,   POWDER.carbs_g),
            fat_g:       add(it.fat,     POWDER.fat_g),
            // Powder contributes none of these, so they pass through untouched
            // rather than being coerced to 0 and claiming a measurement.
            fiber_g:     num(it.fiber),
            sugar_g:     num(it.sugar),
            sodium_mg:   num(it.sodium),
            ai_notes:    withPowder
              ? [it.aiNotes, `Includes ${scoops} scoop(s) protein powder at label values.`]
                  .filter(Boolean).join(" ")
              : (it.aiNotes || null),
            confidence:  it.confidence,
            omitted_kcal: it.omitted,
            // Kept so a retype answered from the repeat cache still links the parts.
            combo_group: it.comboGroup ?? null,
            combo_part:  it.comboPart ?? null,
            combo_name:  it.comboName ?? null,
            // My Foods link (0063): where the row came from; its macros stay the record.
            my_food_id:  it.myFoodId ?? null,
            servings:    it.myFoodId ? (it.servings ?? null) : null,
            recipe_id:   it.recipeId ?? null,
            restaurant:  it.restaurant ?? null,
            // Parsed doses (note 100), scaled to the amount being saved.
            ...(it.myFoodId ? {} : (() => {
              const d = doseOf(it);
              return { caffeine_mg: d.mg, alcohol_abv: d.oz != null ? it.alcAbv : null, alcohol_oz: d.oz };
            })()),
          };
        });
        // One request, one transaction: a five-item dinner must not be able to
        // half-land, and the day only needs its completeness recomputed once.
        if (payload.length) {
          powderFolded = scoops > 0;
          await api.createNutritionLogBatch({
            date: logDate, at_time: logTime || null, meal_type: mealType, items: payload,
            // Only when every item came from parsing THIS text — a picked recent or a
            // scanned label appended to it would otherwise be remembered as part of it,
            // and powder is folded into the first item. A drink routed to the alcohol
            // logger is not in this batch, so the text would be remembered incomplete.
            // v2: "this text" is the last parse's text, and every item must come from it.
            source_text: (scoops === 0 && drinks.length === 0 && lastParse
                          && items.every((it) => it.fromParse && it.parseGroup === lastParse.gid))
              ? lastParse.text : null,
          });
        }
      }
      // Standalone entry ONLY when there is no meal to fold into — a scoop in water,
      // or a shake logged on its own. With items present the macros went into the
      // first one above, and writing this too would double-count them. (If every item
      // was a drink, nothing was folded, so the powder still needs its own row.)
      if (!isEdit && scoops > 0 && !powderFolded) {
        await api.createNutritionLog({
          date: logDate,
          at_time: logTime || null,
          name: POWDER.name,
          meal_type: mealType,
          description: scoopText,
          portion_quantity: scoops,
          portion_unit: "scoop",
          calories:  Math.round(POWDER.calories * scoops),
          protein_g: +(POWDER.protein_g * scoops).toFixed(1),
          carbs_g:   +(POWDER.carbs_g * scoops).toFixed(1),
          fat_g:     +(POWDER.fat_g * scoops).toFixed(1),
          ai_notes: "Label values, not an estimate.",
        });
      }
      qc.invalidateQueries({ queryKey: ["food-brands"] });   // a new brand joins the suggestions
      qc.invalidateQueries({ queryKey: ["water-day"] });     // a logged drink counts as fluid
      if (job) {
        try { await api.updateParseJob(job.id, "saved"); } catch { /* tray clears in 48 h */ }
        qc.invalidateQueries({ queryKey: ["parse-jobs"] });
      }
      onSaved();
      onClose();
    } catch {
      setSaveError("Failed to save — try again");
      setSaving(false);
    }
  };

  // ── v2 layout (2026-10-21, from the log-food-v2 mockup) ────────────────────
  // One box that both searches your foods and parses free text; whatever you pick or
  // parse lands in the tray directly under it; the usual foods for this meal fill the
  // space when the box is empty. Same data and calls as before — panels reorganised.
  const MEAL_HUE = { breakfast: "var(--breakfast)", lunch: "var(--lunch)", dinner: "var(--dinner)",
                     dessert: "var(--dessert)", snack: "var(--other)", drinks: "var(--drinks)" };
  const hue = MEAL_HUE[mealType] ?? "var(--ink-3)";
  const q = boxQuery.trim().toLowerCase();
  const typing = !isEdit && q.length > 0;
  const fmtDay = (iso) => {
    const wd = (d) => `${d.toLocaleDateString([], { weekday: "short" })} ${d.getDate()}`;
    if (iso === localTodayStr()) return { a: "Today", b: wd(new Date(`${iso}T12:00:00`)) };
    const d = new Date(`${iso}T12:00:00`);
    return { a: d.toLocaleDateString([], { weekday: "short" }), b: d.toLocaleDateString([], { month: "short", day: "numeric" }) };
  };
  const day = fmtDay(logDate || localTodayStr());
  const timeLabel = logTime
    ? new Date(`2000-01-01T${logTime}`).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : MEAL_DEFAULT_TIME[mealType] ? `~${MEAL_DEFAULT_TIME[mealType]}`
    : logDate === localTodayStr() ? "now" : "~3:00 PM";

  // My Foods. While typing: every food matching the box. Otherwise: THIS MEAL's foods
  // (2026-10-21) — tagged for it, or actually logged at it (meal_uses), most-logged
  // at this meal first. Toppings stay out of the browse list; they arrive via the
  // "usually with" chips. "All My Foods" is the way to the rest.
  // Linked entries (meal_uses) undercount: an entry typed by name and matched to a My
  // Food carries no link, so also count this meal's recents that map to the food (the
  // recents list is meal-scoped while browsing).
  const recentUses = new Map();
  if (!q) for (const m of recentMeals) if (m.my_food_id) recentUses.set(m.my_food_id, Math.max(recentUses.get(m.my_food_id) ?? 0, m.uses ?? 1));
  const mealN = (f) => Math.max(f.meal_uses?.[mealType] ?? 0, recentUses.get(f.id) ?? 0);
  const forMeal = (f) => f.role !== "topping" && (f.meal_type === mealType || mealN(f) > 0);
  const rank = (f) => (f.role === "topping" ? 2 : f.meal_type === mealType ? 0 : 1);
  const foodList = library
    .filter((f) => !q || f.name.toLowerCase().includes(q) || (f.brand || "").toLowerCase().includes(q)
                   || (f.aliases || []).some((a) => a.includes(q)))
    .filter((f) => q || showAllFoods || forMeal(f))
    .filter((f) => mealType !== "drinks" || !drinkKind || (f.drink_kind || "non_alcoholic") === drinkKind)
    .sort((a, b) => (q || showAllFoods
      ? rank(a) - rank(b) || (b.uses ?? 0) - (a.uses ?? 0)
      : mealN(b) - mealN(a) || (b.uses ?? 0) - (a.uses ?? 0)) || a.name.localeCompare(b.name));
  const foodsShown = showAllFoods || !typing ? foodList : foodList.slice(0, 4);
  const otherFoods = library.length - foodList.length;
  // Recents the server ranked; a recent that IS a My Food is shown once, under My Foods.
  const recentList = recentMeals.filter((m) => !(m.my_food_id && libById[m.my_food_id]));
  const recentsShown = showRecents ? recentList : recentList.slice(0, typing ? 4 : 5);

  const totals = items.reduce((t, it) => ({
    kcal: t.kcal + (parseFloat(it.calories) || 0), p: t.p + (parseFloat(it.protein) || 0),
    omitted: t.omitted + (it.omitted || 0) }), { kcal: 0, p: 0, omitted: 0 });
  const totKcal = Math.round(totals.kcal + (scoops > 0 ? POWDER.calories * scoops : 0));
  const totP = Math.round(totals.p + (scoops > 0 ? POWDER.protein_g * scoops : 0));
  const canSave = hasParsed || isEdit || scoops > 0;

  const chip = "rounded-full border border-rule bg-surface px-3 py-1.5 text-[11px] text-ink-2 whitespace-nowrap";
  const sect = "mb-1 mt-4 text-[8.5px] font-semibold tracking-eyebrow text-ink-3";
  const input = "w-full rounded-lg border border-rule bg-surface px-3 py-1.5 text-sm text-ink outline-none placeholder:text-ink-3 focus:border-[color:var(--hrv)]";
  const err = (t) => t && <p className="text-[11px] leading-snug" style={{ color: "var(--over)" }}>{t}</p>;

  const pickRow = (key, onClick, name, brand, meta, extra = null, disabled = false) => (
    <button key={key} onClick={onClick} disabled={disabled}
      className="flex w-full items-baseline gap-2 border-t border-rule py-2 text-left first:border-t-0 disabled:opacity-50">
      <span className="prose-ui min-w-0 flex-1 text-[13px] leading-snug text-ink line-clamp-2">
        {name}<BrandTag brand={brand} />{extra}
      </span>
      <span className="flex-none text-[10.5px] tabular-nums text-ink-3">{meta}</span>
      <span className="w-5 flex-none text-center text-[15px] text-ink-2">＋</span>
    </button>
  );
  const foodMeta = (f) => `${Math.round(f.calories ?? 0)}${f.protein_g != null ? ` · ${Math.round(f.protein_g)} g` : ""}`;

  const sourceTag = (it) => {
    const t = it.myFoodId ? "MY FOOD" : it.fromLabel ? "LABEL" : it.recipeId ? "RECIPE"
      : it.fromParse ? (cachedFrom && it.parseGroup === lastParse?.gid ? "REPEAT" : "PARSED") : null;
    return t && <span className="flex-none rounded border border-rule px-1 py-px text-[8.5px] tracking-micro text-ink-3">{t}</span>;
  };

  // Parsed doses follow the amount the same way the macros do: against the parse's own
  // portion (base), so retyping the quantity can't drift them.
  const doseFactor = (it) => {
    const b = parseFloat(it.base?.portionQty), q = parseFloat(it.portionQty);
    return b > 0 && q > 0 ? q / b : 1;
  };
  const doseOf = (it) => {
    const f = doseFactor(it);
    return {
      mg: it.caffeineMg ? Math.round(it.caffeineMg * f) : null,
      oz: it.alcAbv && it.alcOz ? +(it.alcOz * f).toFixed(1) : null,
      sdu: it.alcAbv && it.alcOz ? (it.alcOz * f * it.alcAbv / 100) / 0.6 : null,
    };
  };

  // One tray row. Compact by default; tap the name line for everything else. What a
  // correction NEEDS stays visible: the "rough" flag, the size chooser, the uncounted
  // kcal and the usually-with chips. A scanned label with no name opens itself.
  const trayRow = (it, idx) => {
    const open = isEdit || openFields === idx || (it.fromLabel && !(it.name || "").trim());
    const isFood = it.myFoodId && it.per;
    return (
      <div key={it.key} className={`border-t border-rule py-2.5 first:border-t-0 transition-shadow duration-300 ${
        bumped?.key === it.key ? "rounded-lg ring-1 ring-[color:var(--hrv)]" : ""}`}>
        <div className="flex items-baseline gap-2">
          <button onClick={() => !isEdit && setOpenFields(openFields === idx ? null : idx)}
            className="prose-ui min-w-0 flex-1 text-left text-[13px] leading-snug text-ink line-clamp-2">
            {it.name || it.description || "Unnamed item"}<BrandTag brand={it.brand} />
          </button>
          {it.confidence === "low" && !it.chosen && (
            <span className="flex-none rounded px-1.5 py-px text-[9px]" title="The parser is unsure of the amount"
              style={{ color: "var(--breakfast)", background: "color-mix(in srgb, var(--breakfast) 14%, transparent)" }}>rough</span>
          )}
          <span className="flex-none text-[12px] font-semibold tabular-nums text-ink">
            {it.calories !== "" && it.calories != null ? `${Math.round(parseFloat(it.calories) || 0)}` : "—"}
            <span className="ml-0.5 text-[10px] font-normal text-ink-3">kcal</span>
          </span>
          {!isEdit && (
            <button onClick={() => { setItems((prev) => prev.filter((_, i) => i !== idx)); setOpenFields(null); }}
              className="flex-none px-1 text-[12px] text-ink-3" title="Remove this item">✕</button>
          )}
        </div>

        {!isEdit && !it.myFoodId && (it.caffeineMg || (it.alcAbv && it.alcOz)) && (() => {
          const d = doseOf(it);
          const chip = "inline-flex items-center gap-1 rounded-full border border-rule px-2 py-0.5 text-[10.5px] text-ink-2";
          const drop = (keys) => setItems((prev) => prev.map((x) => (x.key === it.key ? { ...x, ...keys } : x)));
          return (
            <div className="mt-1 flex flex-wrap gap-1.5" title="Logged with this item as its own caffeine / alcohol entry">
              {d.mg != null && (
                <span className={chip}>☕ {d.mg} mg caffeine
                  <button onClick={() => drop({ caffeineMg: null })} className="text-ink-3" aria-label="Don't log caffeine">✕</button></span>
              )}
              {d.sdu != null && (
                <span className={chip}>🍺 {d.sdu.toFixed(1)} drinks · {d.oz} oz at {it.alcAbv}%
                  <button onClick={() => drop({ alcAbv: null, alcOz: null })} className="text-ink-3" aria-label="Don't log alcohol">✕</button></span>
              )}
            </div>
          );
        })()}

        {it.restaurant === true && (
          <div className="mt-1 flex">
            <span className="inline-flex items-center gap-1 rounded-full border border-rule px-2 py-0.5 text-[10.5px] text-ink-2"
                  title="Bought prepared: restaurant, cafe, bar or food truck">
              🍽 restaurant
              <button onClick={() => patchItem(idx, { restaurant: false })} className="text-ink-3"
                      aria-label="Not from a restaurant">✕</button>
            </span>
          </div>
        )}

        {it.comboPart === "topping" && (
          <p className="mt-0.5 text-[10px] text-ink-3">
            ↳ topping on {items.find((x) => x.comboGroup === it.comboGroup && x.comboPart === "base")?.name || "the base"}
            {it.followsBase === false ? " · amount set by you" : " · scales with it"}
          </p>
        )}

        {/* Amount + macros on one line. */}
        <div className="mt-1.5 flex items-center gap-2 text-[11px] text-ink-3">
          <div className="flex flex-none items-center overflow-hidden rounded-lg border border-rule">
            {isFood && (
              <button onClick={() => stepServings(idx, -0.5)} disabled={(it.servings ?? 1) <= 0.5}
                className="px-2 py-1 text-ink-2 disabled:opacity-30" title="½ serving less">−</button>
            )}
            <input type="number" inputMode="decimal" value={it.portionQty}
              onChange={(e) => handlePortionChange(idx, e.target.value)} aria-label="Amount"
              className={`w-12 bg-transparent py-1 text-center text-[12px] tabular-nums text-ink outline-none ${isFood ? "border-x border-rule" : ""}`} />
            {isFood && (
              <button onClick={() => stepServings(idx, 0.5)} className="px-2 py-1 text-ink-2" title="½ serving more">＋</button>
            )}
          </div>
          {isFood && foodUnitOptions(it).length > 1 ? (
            <select value={it.portionUnit} onChange={(e) => changeUnit(idx, e.target.value)}
              className="flex-none bg-transparent text-[11px] text-ink-2 outline-none">
              {foodUnitOptions(it).map((u) => <option key={u} value={u}>{u}</option>)}
            </select>
          ) : <span className="flex-none text-ink-2">{it.portionUnit}</span>}
          <span className="min-w-0 flex-1 truncate tabular-nums">
            P {it.protein || "—"} · C {it.carbs || "—"} · F {it.fat || "—"}
          </span>
          {sourceTag(it)}
        </div>

        {Array.isArray(it.options) && it.options.length > 1 && (
          <div className="mt-2 flex gap-1.5">
            {it.options.map((opt) => {
              const active = it.chosen ? it.chosen === opt.label
                : Math.abs((parseFloat(it.calories) || 0) - (opt.calories || 0)) < 1;
              return (
                <button key={opt.label} onClick={() => applyOption(idx, opt)}
                  className={`min-w-0 flex-1 rounded-lg border px-2 py-1 text-[11px] ${
                    active ? "border-[color:var(--hrv)] text-ink" : "border-rule text-ink-3"}`}>
                  <span className="truncate">{opt.label} </span>
                  <span className="tabular-nums">{Math.round(opt.calories)}</span>
                </button>
              );
            })}
          </div>
        )}

        {it.omitted > 0 && (
          <p className="mt-1 text-[11px]" style={{ color: "var(--breakfast)" }}>
            ≈{it.omitted} kcal not counted<span className="text-ink-3"> — unmentioned extras; add them if you had them.</span>
          </p>
        )}

        {it.myFoodId && it.comboPart !== "topping" && (() => {
          const f = libById[it.myFoodId];
          const inGroup = new Set(items.filter((x) => it.comboGroup && x.comboGroup === it.comboGroup).map((x) => x.myFoodId));
          const sugg = (f?.usually_with || []).map((id) => libById[id]).filter((t) => t && !inGroup.has(t.id)).slice(0, 3);
          return sugg.length > 0 && (
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <span className="text-[10px] text-ink-3">usually with</span>
              {sugg.map((t) => (
                <button key={t.id} onClick={() => addTopping(idx, t)}
                  className="rounded-full border border-rule px-2 py-0.5 text-[11px] text-ink-2">＋ {t.name}</button>
              ))}
            </div>
          );
        })()}

        {open && (
          <div className="mt-3 space-y-2.5 rounded-lg border border-rule bg-canvas/40 p-3">
            <input value={it.name} onChange={(e) => patchItem(idx, { name: e.target.value })}
              autoFocus={!!it.fromLabel && !it.name}
              placeholder={it.fromLabel ? "Name this product (required)" : "Item name"}
              className={`${input} ${it.fromLabel && !(it.name || "").trim() ? "border-[color:var(--breakfast)]" : ""}`} />
            <input value={it.brand ?? ""} onChange={(e) => patchItem(idx, { brand: e.target.value })}
              list="brand-options" placeholder="Brand (optional)" autoCapitalize="words" className={`${input} text-xs`} />
            {(isFood ? true : it.portionDesc) && (
              <p className="text-[10.5px] text-ink-3">
                {isFood ? `${+(it.servings ?? 1).toFixed(2)} serving${(it.servings ?? 1) === 1 ? "" : "s"}` : it.portionDesc}
              </p>
            )}
            {it.aiNotes && (
              <p className="prose-ui text-[11px] leading-relaxed text-ink-3">
                <span className="text-ink-2">{it.myFoodId ? "My Food: " : it.fromLabel ? "Label: " : it.recipeId ? "Recipe: " : "Parser: "}</span>
                {it.aiNotes.replace(/^From My Foods · /, "")}
              </p>
            )}
            <div className="grid grid-cols-4 gap-1.5">
              {[["Cal", "calories", "kcal"], ["Protein", "protein", "g"], ["Carbs", "carbs", "g"], ["Fat", "fat", "g"],
                ["Fiber", "fiber", "g"], ["Sugar", "sugar", "g"], ["Sodium", "sodium", "mg"]].map(([label, k, unit]) => (
                <label key={k} className="block">
                  <span className="block text-[9px] tracking-micro text-ink-3">{label.toUpperCase()} <span className="normal-case">{unit}</span></span>
                  <input type="number" inputMode="decimal" value={it[k]} onChange={(e) => patchItem(idx, { [k]: e.target.value })}
                    className="mt-0.5 w-full rounded-md border border-rule bg-surface px-1.5 py-1 text-[12px] tabular-nums text-ink outline-none focus:border-[color:var(--hrv)]" />
                </label>
              ))}
            </div>
            <div className="flex items-center gap-1.5">
              <span className="mr-0.5 text-[9px] tracking-micro text-ink-3">FROM</span>
              {/* An edit can set yes / no; "not sure" there would only leave it as it was. */}
              {[[true, "🍽 Restaurant"], [false, "Home / store"], ...(isEdit ? [] : [[null, "Not sure"]])].map(([v, label]) => (
                <button key={String(v)} onClick={() => patchItem(idx, { restaurant: v })}
                  className={`whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] ${
                    (it.restaurant ?? null) === v ? "border-[color:var(--hrv)] text-ink" : "border-rule text-ink-3"}`}>
                  {label}
                </button>
              ))}
            </div>
            <label className="block">
              <span className="block text-[9px] tracking-micro text-ink-3">DESCRIPTION (WHAT GETS TAGGED)</span>
              <input value={it.description} onChange={(e) => patchItem(idx, { description: e.target.value })}
                className={`${input} mt-0.5 text-xs`} />
            </label>
            {!isEdit && !it.myFoodId && (
              <div className="flex items-baseline gap-2">
                <button onClick={() => saveAsMyFood(idx)} className="text-[11px] text-ink-2 underline underline-offset-2">☆ Save as My Food</button>
                {myFoodMsg[it.key] && <span className="text-[11px]" style={{ color: "var(--over)" }}>{myFoodMsg[it.key]}</span>}
              </div>
            )}
            {it.myFoodId && myFoodMsg[it.key] && <p className="text-[11px] text-ink-2">{myFoodMsg[it.key]}</p>}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/60 pb-16" onClick={onClose}>
      <datalist id="brand-options">
        {brandOptions.map((b) => <option key={b} value={b} />)}
      </datalist>
      {/* Flex column with the scroll on the body, so the action bar stays pinned. */}
      <div className="flex max-h-[85vh] w-full max-w-md flex-col rounded-t-2xl border border-rule bg-card"
           onClick={(e) => e.stopPropagation()}>

        {/* ── Header: what, when, which meal ─────────────────────────────────── */}
        <div className="flex-none px-4 pb-2 pt-4">
          <div className="flex items-center justify-between">
            <span className="text-[9.5px] font-semibold tracking-eyebrow text-ink-3">{isEdit ? "EDIT ENTRY" : "LOG FOOD"}</span>
            <button onClick={onClose} className="-mr-1 px-1 text-[18px] leading-none text-ink-3" aria-label="Close">×</button>
          </div>
          {/* Date and time as chips; the native picker sits invisibly over each. In edit
              mode the DATE stays fixed (moving an entry changes two days' totals) but the
              time is editable. Empty time = "don't know": the backend's meal slot / now,
              flagged as estimated — the chip says which. */}
          <div className="mt-2.5 flex items-center gap-1.5">
            {!isEdit && (
              <label className={`${chip} relative`}>
                <b className="font-semibold text-ink">{day.a}</b> · {day.b}
                <input type="date" value={logDate} onChange={(e) => setLogDate(e.target.value)} aria-label="Date"
                  className="absolute inset-0 h-full w-full cursor-pointer opacity-0" />
              </label>
            )}
            <label className={`${chip} relative`}>
              {logTime ? <>at <b className="font-semibold text-ink">{timeLabel}</b></>
                : <>{timeLabel}{timeLabel !== "now" && <span className="text-ink-3"> est.</span>}</>}
              <input type="time" value={logTime} onChange={(e) => setLogTime(e.target.value)} aria-label="Time eaten"
                className="absolute inset-0 h-full w-full cursor-pointer opacity-0" />
            </label>
            {logTime && (
              <button onClick={() => setLogTime("")} className="px-1 text-[11px] text-ink-3" title="Clear the time">✕</button>
            )}
          </div>
          <div className="-mx-4 mt-2 flex gap-1.5 overflow-x-auto px-4 pb-0.5" style={{ scrollbarWidth: "none" }}>
            {MEALS.map((m) => {
              const on = mealType === m;
              return (
                <button key={m} onClick={() => setMealType(m)}
                  className="flex-none rounded-full border px-3 py-1.5 text-[11px] transition-colors"
                  style={on ? { borderColor: MEAL_HUE[m], color: "var(--ink)",
                                background: `color-mix(in srgb, ${MEAL_HUE[m]} 16%, transparent)` }
                            : { borderColor: "var(--rule)", color: "var(--ink-2)" }}>
                  {MEAL_LABELS[m]}
                </button>
              );
            })}
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-4">
          {isEdit && logTime === "12:00" && (
            <p className="mb-2 text-[10px] text-ink-3">
              Entries logged before 2026-08-17 all read 12:00 — that was a placeholder, not a real time.
            </p>
          )}

          {/* ── The box ───────────────────────────────────────────────────────── */}
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            onKeyDown={(e) => {
              // Return parses (create mode); in edit mode the description is prose, so
              // it takes Cmd/Ctrl+Return as before.
              if (e.key === "Enter" && !e.shiftKey && (!isEdit || e.metaKey || e.ctrlKey)
                  && description.trim() && !parsing) {
                e.preventDefault();
                runParse(description);
              }
            }}
            rows={description.length > 38 ? 3 : 1}
            enterKeyHint="go"
            placeholder={isEdit ? "Description" : (hasParsed ? "Add another…" : "Search My Foods, or describe what you ate…")}
            className="prose-ui mt-1 w-full resize-none rounded-xl border border-rule bg-surface px-3 py-3 text-[14px] text-ink outline-none placeholder:text-ink-3 focus:border-[color:var(--hrv)]"
          />

          {/* Tools: the occasional inputs, one row. Hidden while typing (it is search). */}
          {!isEdit && !typing && (
            <div className="mt-2 flex items-stretch gap-1.5">
              <label className={`flex min-w-0 flex-1 cursor-pointer items-center justify-center gap-1 rounded-lg border border-dashed border-rule py-1.5 text-[11px] text-ink-2 ${scanning ? "cursor-wait" : ""}`}>
                📷 <span className="truncate">{scanning ? "Reading…" : "Label"}</span>
                {/* No `capture` attribute: on iOS it forces the camera and hides the photo
                    library, and most labels are photographed once and logged later. */}
                <input type="file" accept="image/*" className="hidden" disabled={scanning}
                  onChange={(e) => { const f = e.target.files?.[0]; if (f) handleLabelPhoto(f); e.target.value = ""; }} />
              </label>
              {(recipes.length > 0 || recipesDown) && (
                <button onClick={() => setShowRecipes((v) => !v)} aria-expanded={showRecipes}
                  className={`flex min-w-0 flex-1 items-center justify-center gap-1 rounded-lg border border-dashed py-1.5 text-[11px] ${
                    showRecipes ? "border-[color:var(--hrv)] text-ink" : "border-rule text-ink-2"}`}>
                  📖 <span className="truncate">Recipes</span> <span className="text-[10px] text-ink-3">{recipesDown ? "!" : recipes.length}</span>
                </button>
              )}
              {/* A select: the options are ordered and exclusive, and at rest it shows the value. */}
              <select value={scoops} onChange={(e) => setScoops(parseFloat(e.target.value))} aria-label="Protein powder scoops"
                className={`min-w-0 flex-1 rounded-lg border border-dashed bg-transparent px-1 py-1.5 text-center text-[11px] outline-none ${
                  scoops > 0 ? "border-[color:var(--hrv)] text-ink" : "border-rule text-ink-2"}`}>
                {SCOOP_OPTIONS.map((n) => (
                  <option key={n} value={n}>{n === 0 ? "＋ Powder" : `${n} scoop${n === 1 ? "" : "s"}`}</option>
                ))}
              </select>
            </div>
          )}
          {err(labelError)}
          {scoops > 0 && (
            <p className="mt-1 text-[10.5px] text-ink-3">
              +{Math.round(POWDER.calories * scoops)} kcal · {+(POWDER.protein_g * scoops).toFixed(1)} g protein
              {" "}— added to this meal on save{hasParsed ? "" : ", or logged on its own"}
            </p>
          )}

          {/* My Recipes (note 37): its own list, never mixed into My Foods. */}
          {!isEdit && showRecipes && recipesDown && recipes.length === 0 && (
            <p className="mt-3 rounded-xl border border-rule px-3 py-2 text-[11px] leading-snug" style={{ color: "var(--breakfast)" }}>{recipesDown}</p>
          )}
          {!isEdit && showRecipes && recipes.length > 0 && (() => {
            const list = recipes
              .filter((r) => !q || r.title.toLowerCase().includes(q) || (r.ingredient_text || "").toLowerCase().includes(q))
              .sort((a, b) => (b.uses ?? 0) - (a.uses ?? 0) || a.title.localeCompare(b.title));
            const shown = showAllRecipes ? list : list.slice(0, 6);
            return (
              <div className="mt-3 rounded-xl border border-rule px-3 py-1">
                {recipeMsg && <p className="pt-2 text-[11px] leading-snug" style={{ color: "var(--breakfast)" }}>{recipeMsg}</p>}
                {shown.map((r) => {
                  const n = r.nutrition;
                  const known = n?.calories != null;
                  const busy = estimatingId === r.recipe_id;
                  return pickRow(r.recipe_id, () => addRecipe(r), r.title, null,
                    busy ? "estimating…" : known ? `${Math.round(n.calories)} · ${n.protein_g != null ? `${Math.round(n.protein_g)} g` : "—"}` : "estimate",
                    known && n.source !== "confirmed"
                      ? <span className="ml-1.5 text-[9px] tracking-micro text-ink-3">ESTIMATE{n.stale ? " · CHANGED" : ""}</span> : null,
                    busy);
                })}
                {!showAllRecipes && list.length > 6 && (
                  <button onClick={() => setShowAllRecipes(true)} className="w-full py-1.5 text-[11px] text-ink-3">Show all {list.length}</button>
                )}
              </div>
            );
          })()}

          {/* ── Tray: what will be logged, directly under the box ─────────────── */}
          {hasParsed && (
            <div className="mt-3 rounded-xl border border-rule bg-surface px-3 py-0.5">
              {items.map(trayRow)}
            </div>
          )}
          {!isEdit && hasParsed && comboable.length >= 2
            && !(comboable.every((x) => x.comboGroup && x.comboGroup === comboable[0].comboGroup)) && (
            comboDraft ? (
              <div className="mt-2 space-y-2 rounded-xl border border-rule bg-surface p-3">
                <span className="block text-[9px] tracking-micro text-ink-3">SAVE AS ONE MY FOOD · TAP TO LEAVE ONE OUT</span>
                <div className="flex flex-wrap gap-1.5">
                  {comboable.map((x) => {
                    const on = !comboDraft.off.has(x.key);
                    return (
                      <button key={x.key}
                        onClick={() => setComboDraft((d) => {
                          const off = new Set(d.off);
                          if (off.has(x.key)) off.delete(x.key); else off.add(x.key);
                          return { ...d, off };
                        })}
                        className={`max-w-full truncate rounded-full border px-2 py-0.5 text-[11px] ${
                          on ? "border-[color:var(--hrv)] text-ink" : "border-rule text-ink-3 line-through"}`}>
                        {x.name || x.description}
                      </button>
                    );
                  })}
                </div>
                <div className="flex gap-2">
                  <input value={comboDraft.name} onChange={(e) => setComboDraft((d) => ({ ...d, name: e.target.value }))}
                    placeholder="Name the dish, e.g. Taco salad" autoCapitalize="words"
                    className={`${input} min-w-0 flex-1 text-xs`} />
                  <button onClick={saveTrayCombo}
                    disabled={!comboDraft.name.trim() || comboable.filter((x) => !comboDraft.off.has(x.key)).length < 2}
                    className="flex-none rounded-lg border border-rule px-3 text-[12px] text-ink disabled:opacity-40">Save</button>
                  <button onClick={() => setComboDraft(null)} className="flex-none px-1 text-[12px] text-ink-3" aria-label="Cancel">✕</button>
                </div>
              </div>
            ) : (
              <button onClick={() => { setComboDraft({ name: "", off: new Set() }); setComboMsg(null); }}
                className="mt-1.5 text-[11px] text-ink-2 underline underline-offset-2">☆ Save these as one My Food</button>
            )
          )}
          {comboMsg && !comboDraft && <p className="mt-1 text-[11px] text-ink-2">{comboMsg}</p>}
          {isEdit && !existingLog?.my_food_id && <div className="mt-3"><MakeMyFood logId={existingLog.id} /></div>}

          {/* The last parse: instant-repeat note and "add detail" (re-parses that parse only). */}
          {cachedFrom && lastParse && !parsing && items.some((it) => it.parseGroup === lastParse.gid) && (
            <p className="mt-2 text-[11px] text-ink-3">
              Same text as your {cachedFrom.source === "saved" ? "last save" : "entry"}
              {cachedFrom.date ? ` on ${cachedFrom.date}` : ""} — numbers reused, no AI.{" "}
              <button onClick={() => runParse(lastParse.text, true, lastParse.gid)} className="text-ink-2 underline underline-offset-2">Re-estimate</button>
            </p>
          )}
          {!isEdit && lastParse && !typing && items.some((it) => it.parseGroup === lastParse.gid) && (
            <div className="mt-2 flex gap-2">
              <input value={refinement} onChange={(e) => setRefinement(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleRefine()}
                placeholder={`Add detail to “${lastParse.text.slice(0, 22)}${lastParse.text.length > 22 ? "…" : ""}”`}
                className={`${input} flex-1 text-xs`} />
              <button onClick={handleRefine} disabled={!refinement.trim() || parsing}
                className="rounded-lg border border-rule px-3 text-sm text-ink-2 disabled:opacity-40" title="Re-parse with the extra detail">↺</button>
            </div>
          )}

          {/* ── Parse (typing, or edit mode) ─────────────────────────────────── */}
          {(typing || isEdit) && description.trim() && (
            <div className="mt-3">
              <button onClick={() => runParse(description)} disabled={parsing}
                className="prose-ui flex w-full items-center gap-2 rounded-xl border px-3 py-2.5 text-left text-[12.5px] text-ink disabled:opacity-60"
                style={{ borderColor: "color-mix(in srgb, var(--hrv) 45%, var(--rule))" }}>
                <span>✦</span>
                <span className="min-w-0 flex-1 truncate">
                  {parsing ? "Parsing…" : isEdit ? "Re-parse this description" : `Parse “${description.trim()}”`}
                </span>
                <span className="flex-none text-[10px] text-ink-3">AI</span>
              </button>
              {!isEdit && !hasParsed && !parsing && (
                <button onClick={parseInBackground} disabled={bgBusy}
                  className="prose-ui mt-1 w-full py-1 text-left text-[10.5px] text-ink-3 disabled:opacity-40">
                  {bgBusy ? "Sending…" : "or parse in the background and keep going →"}
                </button>
              )}
            </div>
          )}
          {parseError && <div className="mt-2">{err(parseError)}</div>}

          {/* ── Pick lists: matches while typing, the usual foods otherwise ──── */}
          {!isEdit && (
            <>
              {mealType === "drinks" && library.some(isDrink) && (
                <div className="mt-4 flex flex-wrap gap-1">
                  {[{ id: null, label: "All", emoji: "" },
                    ...DRINK_KINDS.filter((k) => library.some((f) => isDrink(f) && (f.drink_kind || "non_alcoholic") === k.id))]
                    .map((k) => (
                      <button key={k.id ?? "all"} onClick={() => setDrinkKind(k.id)}
                        className={`rounded-full border px-2.5 py-0.5 text-[11px] ${drinkKind === k.id ? "border-[color:var(--drinks)] text-ink" : "border-rule text-ink-3"}`}>
                        {k.emoji} {k.label}
                      </button>
                    ))}
                </div>
              )}
              {/* Same as last <meal> (#22): one tap adds the whole meal. */}
              {!typing && lastMeal?.items?.length > 0 && lastMealAdded !== `${mealType}|${lastMeal.date}` && (() => {
                const rows = lastMeal.items;
                const kcal = Math.round(rows.reduce((a, r) => a + (r.calories || 0), 0));
                const prot = Math.round(rows.reduce((a, r) => a + (r.protein_g || 0), 0));
                const dayMs = new Date(`${logDate}T12:00:00`) - new Date(`${lastMeal.date}T12:00:00`);
                const ago = Math.round(dayMs / 86400000);
                const when = ago === 1 ? "yesterday"
                  : ago < 7 ? new Date(`${lastMeal.date}T12:00:00`).toLocaleDateString([], { weekday: "long" })
                  : new Date(`${lastMeal.date}T12:00:00`).toLocaleDateString([], { month: "short", day: "numeric" });
                return (
                  <button onClick={addLastMeal}
                    className="mt-4 flex w-full items-center gap-2 rounded-xl border px-3 py-2.5 text-left"
                    style={{ borderColor: `color-mix(in srgb, ${hue} 45%, var(--rule))`,
                             background: `color-mix(in srgb, ${hue} 7%, transparent)` }}>
                    <span className="text-[15px] text-ink-2">↻</span>
                    <span className="min-w-0 flex-1">
                      <span className="prose-ui block text-[13px] text-ink">
                        Same as {MEAL_LABELS[mealType].toLowerCase()} {when}
                      </span>
                      <span className="prose-ui block truncate text-[10.5px] text-ink-3">
                        {rows.map((r) => r.name || r.description).join(" · ")}
                      </span>
                    </span>
                    <span className="flex-none text-right text-[10.5px] tabular-nums text-ink-3">
                      {kcal} · {prot} g<br />{rows.length} item{rows.length === 1 ? "" : "s"}
                    </span>
                  </button>
                );
              })()}
              {(foodList.length > 0 || (!typing && library.length > 0)) && (
                <>
                  <p className={`${sect} flex items-baseline`}>
                    <span className="flex-1">
                      MY FOODS{!typing && !showAllFoods && <span style={{ color: hue }}> · {MEAL_LABELS[mealType].toUpperCase()}</span>}
                      {!typing && showAllFoods && <span> · ALL</span>}
                    </span>
                    {!typing && (showAllFoods || otherFoods > 0) && (
                      <button onClick={() => setShowAllFoods((v) => !v)} className="text-[9px] tracking-micro text-ink-2">
                        {showAllFoods ? `ONLY ${MEAL_LABELS[mealType].toUpperCase()}` : `ALL ${library.length} →`}
                      </button>
                    )}
                  </p>
                  {!typing && !showAllFoods && foodList.length === 0 && (
                    <p className="py-1 text-[11px] text-ink-3">No My Foods logged at {MEAL_LABELS[mealType].toLowerCase()} yet.</p>
                  )}
                  <div>
                    {foodsShown.map((f) => pickRow(f.id, () => addMyFood(f), f.name, f.brand, foodMeta(f),
                      <>
                        {f.role === "topping" && <span className="ml-1.5 text-[9px] tracking-micro text-ink-3">TOPPING</span>}
                        {isAlcoholic(f) && <span className="ml-1.5 text-[9px] text-ink-3">{f.abv}%</span>}
                        {f.role === "combo" && (
                          <span className="block text-[10.5px] text-ink-3">{f.components.map((c) => libById[c.my_food_id]?.name ?? "?").join(" + ")}</span>
                        )}
                      </>))}
                    {typing && !showAllFoods && foodList.length > foodsShown.length && (
                      <button onClick={() => setShowAllFoods(true)} className="w-full py-1.5 text-left text-[11px] text-ink-3">
                        {foodList.length - foodsShown.length} more →
                      </button>
                    )}
                  </div>
                </>
              )}
              {recentList.length > 0 && (
                <>
                  <p className={`${sect} flex items-baseline`}>
                    <span className="flex-1">RECENT{!typing && <span style={{ color: hue }}> · {MEAL_LABELS[mealType].toUpperCase()}</span>}</span>
                    {[["recent", "LATEST"], ["frequent", "FREQUENT"]].map(([v, l]) => (
                      <button key={v} onClick={() => setRecentSort(v)}
                        className={`ml-3 text-[9px] tracking-micro ${recentSort === v ? "text-ink" : "text-ink-3"}`}
                        aria-pressed={recentSort === v}>{l}</button>
                    ))}
                  </p>
                  <div>
                    {recentsShown.map((m) => pickRow(m.id, () => applyRecent(m), m.name || m.description, m.brand,
                      `${m.calories != null ? Math.round(m.calories) : "—"}${m.protein_g != null ? ` · ${m.protein_g.toFixed(0)} g` : ""}`,
                      <>
                        {recentSort === "recent" && m.timestamp
                          ? <span className="ml-1.5 text-[10px] tabular-nums text-ink-3">{(() => {
                              const days = Math.round((new Date(localTodayStr()) - new Date(String(m.timestamp).slice(0, 10))) / 86400000);
                              return days <= 0 ? "today" : days === 1 ? "yesterday" : `${days}d ago`; })()}</span>
                          : m.uses > 1 && <span className="ml-1.5 text-[10px] tabular-nums text-ink-3">×{m.uses}</span>}
                        {typing && m.meal_type !== mealType && <span className="ml-1.5 text-[10px] text-ink-3">{m.meal_type}</span>}
                      </>))}
                    {!showRecents && recentList.length > recentsShown.length && (
                      <button onClick={() => setShowRecents(true)} className="w-full py-1.5 text-left text-[11px] text-ink-3">
                        {recentList.length - recentsShown.length} more →
                      </button>
                    )}
                  </div>
                </>
              )}
              {typing && !foodList.length && !recentList.length && (
                <p className="mt-3 text-[11px] text-ink-3">Nothing saved matches — parse it above.</p>
              )}
            </>
          )}
        </div>

        {/* ── Pinned footer: running total + the one primary action ─────────── */}
        <div className="flex-none border-t border-rule bg-card px-4 pb-8 pt-3">
          {saveError && <div className="mb-2">{err(saveError)}</div>}
          <div className="flex items-center gap-3">
            <button onClick={onClose} className="flex-none px-1 py-2.5 text-[13px] text-ink-2">Cancel</button>
            <div className="min-w-0 flex-1 text-[10.5px] leading-tight text-ink-3">
              {canSave && (hasParsed || scoops > 0) ? (
                <>
                  <span className="text-[14px] font-semibold tabular-nums text-ink">{totKcal.toLocaleString()}</span> kcal
                  {" · "}<span className="tabular-nums">{totP} g protein</span>
                  <br />
                  {items.length} item{items.length === 1 ? "" : "s"}{scoops > 0 ? " + powder" : ""}
                  {totals.omitted > 0 && <span style={{ color: "var(--breakfast)" }}> · +≈{totals.omitted} uncounted</span>}
                </>
              ) : "nothing added yet"}
            </div>
            <button onClick={handleSave} disabled={!canSave || saving}
              className={`flex-none rounded-xl px-5 py-2.5 text-[13px] font-semibold ${
                canSave ? "bg-ink text-canvas" : "bg-surface text-ink-3"} disabled:opacity-60`}>
              {saving ? "Saving…" : isEdit ? "Save" : items.length > 1 ? `Log ${items.length}` : "Log"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
