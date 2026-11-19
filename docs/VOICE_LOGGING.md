# Voice logging — "Hey Siri, log …"

Designed 2026-09-28. Phases 1–2 (foundation + water) shipped that day; caffeine, alcohol
and meal were to follow, one testable slice at a time.

## Status — on hold

Shipped: the `VOICE_TOKEN` setting, an `entry_channel` provenance column on
`health_metrics` / `food_log` / `caffeine_log`, the `voice_drafts` table, `/api/voice/water`
(logs immediately), `/drafts/{id}/commit`, `DELETE /drafts/{id}`, `/undo`, the spoken-time
reader (39-case test) and the water reader (30-case test). Verified end to end through the
reverse proxy: 401s, spoken failures, log → correct row → undo → gone. Nothing calls it until
a Shortcut does, so leaving it deployed is harmless.

Not yet exercised: the phone end to end, and the draft → "Log it" → commit path
(water is quick). Shortcuts pitfalls worth knowing before building the next one:
1. Adding *Get Contents of URL* after *Ask for Input* auto-inserts *Provided Input* into the
   URL field as a variable chip; typing the URL then RENAMES that variable instead of
   setting the URL. Fix: chip → Clear → type the URL as plain text (no chip, no icon).
2. Header must be Key `Authorization`, Value `Bearer <token>` — not `Authorization=Bearer`
   in the key column.
3. JSON body key must be lowercase `text` (no `=`), and its Value must be the
   *Provided Input* chip — an empty Value sends nothing ("I didn't hear anything").

Plan when resumed (test on the phone after each):
1. "Log water" and "Undo last log" — the checkpoint for the iOS / VPN / certificate chain.
2. Caffeine ("log caffeine"): presets with spoken names; custom mg from a reference table,
   read back as an estimate; additions ("with oat milk") priced by the food parser;
   `POST /drafts/{id}/revise` — a correction updates only the fields it mentions.
3. Alcohol: preset / saved drink, or oz × ABV × type; a missing ABV gets a stated default
   that must be correctable before logging; quantity.
4. Meal: meal from a menu; saved food or parser; "skipped" = meal skip; drinks in the text
   are left out with a pointer to the right Shortcut; catch-up meal without a time → slot.
5. Finish: a VIA SIRI tag + one-tap undo on Today; a Siri mis-transcription test corpus
   ("venti" → "twenty", brand names) across all four.

## Why this shape

- **One Shortcut per logger** — "log water", "log caffeine", "log alcohol", "log meal".
  Routing is by the phrase you chose, never guessed from the sentence, so a beer can't
  land in the food log and a cold brew can't be counted twice (caffeine and alcohol
  entries already write their own calorie rows).
- **Nothing new is exposed.** The phone reaches the server over a private VPN and
  already trusts its certificate. An agent in someone else's cloud could not, and the fix
  for that — putting the app on the public internet — was rejected. Off the VPN the
  Shortcut simply fails and says so.
- **Draft → "Log it" → commit.** Siri reads back what it understood, including the time
  and any assumed default, and nothing is written until you confirm. "Change" takes a
  correction. **Water is the exception** — low stakes, logged immediately.
- **Numbers come from presets, My Foods or fixed reference tables.** A model may only
  extract what was SAID ("12 oz", "7 percent"); the readback says when a figure is an
  estimate.
- **Provenance:** every voice row carries `entry_channel='voice'` (NULL = app). NOT a new
  `health_metrics.source` value — the HAE upsert protects rows by
  `source == 'wellness_app'`, and a new value would let a sync overwrite a voice entry.
- **Undo:** "undo last log" removes exactly the rows the most recent voice commit wrote
  (recorded on the draft), within 30 minutes.

## API (`routers/voice.py`, Bearer `VOICE_TOKEN` — grants only `/api/voice/*`)

Every user-facing outcome is **HTTP 200** with `say`, the sentence to speak — failures
included — so a Shortcut has one code path. Only auth uses real status codes (401/503).

| Call | Body | Result |
|---|---|---|
| `POST /api/voice/{kind}` | `{text, meal?}` | QUICK kinds: logged. Others: `{draft_id, confirm: true, say: "… Log it?"}` |
| `POST /api/voice/drafts/{id}/commit` | — | `"Logged."` |
| `DELETE /api/voice/drafts/{id}` | — | `"Cancelled. Nothing was logged."` |
| `POST /api/voice/undo` | — | `"Removed …"` |

Response: `{ok, say, draft_id, confirm, logged?}`. Drafts expire after 10 minutes.

## Spoken times (`services/voice_time.py`, test: `tests/verify_voice_time.py`)

Nothing said → now. Bare hour → most recent past occurrence. An am/pm time later than now
with no day word → yesterday. Day/meal words without a clock ("this morning", "with
lunch", "yesterday") → a typical time, read back as "(estimated)". Clock phrases need an
anchor (at / colon / am-pm), and a following unit vetoes them — "at 7 percent" is an ABV,
not 7 AM (the test file caught exactly that).

## Kinds

| Kind | Status | Notes |
|---|---|---|
| water | **shipped** | oz / ml / liters / presets incl. custom ones / bare number = oz. Quick. Readback adds today's total. Test: `tests/verify_voice_water.py` |
| caffeine | planned | presets need spoken names + a clean name/brand; custom mg from a reference table ("≈140 mg (estimate)"); additions ("with oat milk") priced by the food parser and added to the preset's calories |
| alcohol | planned | preset / saved drink, or oz × ABV × type (carbs per oz by type already exist); a missing ABV gets a stated default you can correct |
| meal | planned | meal picked from a menu; saved food or parser; "skipped" = meal skip; drinks found in the text are left out with a pointer to the right Shortcut; a meal said with no time uses its slot when "now" is clearly catch-up |

## Shortcuts

Build on the Mac's Shortcuts app if easier — they sync to the iPhone through iCloud, and
the token (`VOICE_TOKEN` in the server's `backend/.env`) can be pasted there. Never paste
it into chat or a note.

### "Log water"
1. **Ask for Input** — Text, prompt "How much water?". Under Siri this is spoken and your
   answer is dictated; tapped, it's a text box.
2. **Get Contents of URL** — `https://<your-server>/api/voice/water`; Method **POST**;
   Headers: `Authorization` = `Bearer <token>`; Request Body **JSON**, one Text field
   `text` = *Provided Input*.
3. **Get Dictionary Value** — Value for key `say` in *Contents of URL*.
4. **Show Result** — *Dictionary Value*. Siri reads it aloud when invoked by voice; shown
   on screen when tapped.

The Shortcut's NAME is the Siri phrase: "Log water".

### "Undo last log"
1. **Get Contents of URL** — `https://<your-server>/api/voice/undo`; POST; same header;
   Request Body JSON with no fields.
2. **Get Dictionary Value** — `say`. 3. **Show Result**.

Off the VPN, step 2 fails with a network error — connect (or set the VPN to connect on
demand) and retry.
