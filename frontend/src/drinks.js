// Named drinks (note 34, Alembic 0067). A drink is a My Food with a kind and, when
// alcoholic, an ABV and an oz serving — so standard drinks are exact arithmetic.
// Kind ids mirror services/intake.ALCOHOL_CATEGORIES (the calorie-estimate densities)
// plus non_alcoholic, and must match routers/my_foods.DrinkKind.

export const DRINK_KINDS = [
  { id: "beer",          label: "Beer",          emoji: "🍺" },
  { id: "beer_light",    label: "Light beer",    emoji: "🍺" },
  { id: "wine",          label: "Wine",          emoji: "🍷" },
  { id: "liquor",        label: "Liquor",        emoji: "🥃" },
  { id: "cocktail",      label: "Cocktail",      emoji: "🍸" },
  { id: "seltzer",       label: "Seltzer",       emoji: "🥫" },
  { id: "non_alcoholic", label: "Non-alcoholic", emoji: "🥤" },
];
export const DRINK_KIND_BY_ID = Object.fromEntries(DRINK_KINDS.map((k) => [k.id, k]));

export const isDrink = (f) => f?.meal_type === "drinks" || Boolean(f?.drink_kind);
export const isAlcoholic = (f) =>
  (f?.abv ?? 0) > 0 && (f?.serving_unit || "").trim().toLowerCase() === "oz";

// US standard drink = 0.6 fl oz of ethanol (services/intake.ETHANOL_OZ_PER_SDU).
export const sduOf = (oz, abv, ozPerSdu = 0.6) => (Number(oz) * Number(abv) / 100) / ozPerSdu;

// A STARTING GUESS for a drink found in the log history: the typical pour and strength
// for its kind — the same numbers as the default alcohol presets. Always shown as a
// guess; the can or the menu is the real number, and the ABV is the one fact only the user
// can supply. Order matters: "light" and "seltzer" before beer, cocktails before
// spirits ("gin and tonic", "bourbon old-fashioned").
const GUESSES = [
  [/\blight\b|\blite\b|ultra\b/i,                                  { kind: "beer_light", oz: 12, abv: 4.2 }],
  [/seltzer|high noon|white claw|truly|kombucha/i,                  { kind: "seltzer",    oz: 12, abv: 5 }],
  [/martini|manhattan|old.fashioned|margarita|negroni|mojito|cocktail|and tonic|mule|spritz/i,
                                                                    { kind: "cocktail",   oz: 3,  abv: 25 }],
  [/\bipas?\b|double|imperial/i,                                    { kind: "beer",       oz: 16, abv: 7 }],
  [/beer|lager|\bales?\b|pilsner|stout|porter|dunkel|hefe|wheat/i,  { kind: "beer",       oz: 12, abv: 5 }],
  [/wine|merlot|cabernet|pinot|ros[eé]|chardonnay|sauvignon|prosecco|champagne/i,
                                                                    { kind: "wine",       oz: 5,  abv: 12 }],
  [/bourbon|whiske?y|scotch|tequila|vodka|\bgin\b|\brum\b|mezcal|liquor|\bshots?\b/i,
                                                                    { kind: "liquor",     oz: 1.5, abv: 40 }],
];

export function guessDrink(name) {
  for (const [re, g] of GUESSES) if (re.test(name || "")) return { ...g };
  return { kind: "non_alcoholic", oz: null, abv: null };
}
