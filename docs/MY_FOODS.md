# My Foods — design spec (2026-09-13; phases 1 and 3 BUILT, phase 2 pending)

Personal food library: the foods worth precise nutrition facts. Logging becomes picking
items; the model is the fallback for food the library does not know yet.

## Why (measured on four months of real logs)

- **A few dozen repeat foods covered about two thirds of all entries.** Logging is mostly
  re-logging, so a personal library pays for itself quickly.
- **Most repeat foods carried more than one calorie value.** Part is honest portion variation.
  Much is UNIT CHAOS — the same food logged in incompatible units: a pizza as a fraction of a
  pie one day and as "1 piece" the next, a snack in grams and in bags, a drink in oz and in
  cans. Per-unit spreads ran from tens of percent to orders of magnitude.
- **About a third were combinations** (e.g. a bagel with a spread, a sandwich with cheese):
  base × topping, not distinct foods.
- Half of entries carried typed label numbers, and a branded entry in the parser prompt's
  reference table was already a one-item "My Food" hard-coded into a prompt.

## Data model

**`my_foods`** (new)
| column | notes |
|---|---|
| id, name | display name; also written to `food_log.name`, so `food_tags.item_key(name)` keeps every food-group tag working |
| role | `food` \| `topping` (toppings surface as "usually with" chips) |
| serving_label | as printed or natural: "1 bar", "1 bagel", "2 tbsp", "1 can (12 oz)" |
| serving_qty, serving_unit | 1, "bar" / 28, "g" — the unit the macros are per |
| units (JSON) | alternates as factors of one serving: `[{"label": "slice", "factor": 0.125}, {"label": "g", "factor": 0.0028}]` — this is what ends the pizza/chips/soda unit chaos |
| calories, protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg | PER SERVING; carbs = TOTAL (fiber included) |
| source | `label` (scanned) \| `confirmed` (typed/checked by the user) \| `estimate` (seeded from history, not yet checked) |
| notes, archived, created_at, updated_at | |

**`food_log`** gains `my_food_id` (nullable) and `servings` (float). The row still stores its
own macros — a SNAPSHOT at log time — so totals, correlations and tags are untouched and
history never silently changes. Correcting a My Food offers "apply to the N past entries"
explicitly (exactly what an earlier one-off backfill did by hand, with a rollback file).

"Usually with" is computed, not stored: toppings co-logged with a base at the same timestamp.

## How it plugs in

1. **Picker**: My Foods first (search + most-used), then recent entries. Tap → adds the item at
   one serving; a stepper changes servings; a unit switch uses `units`. After a base, its
   usual toppings show as chips (cream cheese / peanut butter / butter).
2. **Typed text**: unchanged path, but after parsing, any item whose `item_key` matches a My
   Food takes the My Food's numbers × the parsed quantity — deterministic, and it replaces the
   hard-coded reference-table entries. The instant-repeat cache stays in front of both.
3. **Label scan** → "Save as My Food" (source `label`).
4. **Any logged entry** → "Make this a My Food" (edit/confirm numbers first).
5. **Note 10**: when the parser flags an unnamed spread, the one-click chips are YOUR toppings
   with YOUR numbers, not a generic +90 kcal.

## Build phases

1. **Backend** — `my_foods` + CRUD API, `food_log.my_food_id/servings`, the parse-match hook,
   "apply correction to past entries" (with a rollback file), offline tests.
2. **Frontend** — My Foods manager (list / edit / create from label or entry), picker
   integration with servings + unit switch, "usually with" chips.
3. **Seeding** — one-time review of the top candidates: accept / edit / split combos into base +
   topping / skip. Nothing is created without the user confirming it.

## Decisions (2026-09-13)

1. **Snapshot + explicit apply-to-past.** Undo restores from `my_food_backfills`.
2. **Separate rows per part**, grouped by meal.
3. **Seeding review, ORGANISED BY MEAL TYPE**, top 15 (adjustable) within each group —
   `GET /api/my-foods/candidates?per_group=15`.
4. **Manager in the log modal plus a page under Body → Nutrition.**

## Phase 1 — built (Alembic 0063)

- `my_foods`, `my_food_backfills`; `food_log.my_food_id`, `food_log.servings`.
- `/api/my-foods`: list (with uses/last used), create (409 on a duplicate item key), update
  (library only), archive (soft — never deleted), `from-log/{id}` ("make this a My Food",
  links that entry), `candidates`, `{id}/past-entries` (preview; unit mismatches listed and
  never touched), `{id}/apply-to-past` (snapshot first), `backfills/{id}/undo`.
- `/api/nutrition/parse`: every result — fresh, cached repeat, history match — passes
  through `apply_my_foods`: a matching item takes the food's numbers × servings. Exact TYPED
  numbers win (`my_food_hint` explains why the library was not applied), and a unit the food
  cannot convert is left alone with a hint. Log endpoints accept `my_food_id` + `servings`.
- Units: counts are interchangeable ("1 piece" of a bar = 1 bar), but a SLICE is not a count
  — it belongs in `units` as a fraction of the pie. Offline test: `tests/verify_my_foods.py`.
