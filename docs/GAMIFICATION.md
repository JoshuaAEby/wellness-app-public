# Gamification — RPG stats layer (DESIGN, not built)

Status: **design agreed, v1 decisions LOCKED 2026-08-03, not implemented.** A separate
tab that presents the app's real data as an RPG-style character sheet, to make progress
visible and motivating **without** corrupting the app's core value: honest, unbiased
measurement.

Read this whole file before building any of it. The stat table below was revised
2026-08-03 against the live DB — the original draft's "solid / fuzzy" judgements were
wrong in both directions (see "Data reality"). Trust the revised table, not intuition
about which stats are well-founded.

---

## The core tension (read first)

This app is an **honest measurement instrument** — blinded n-of-1 experiments,
FDR-corrected correlations, completeness gating, confounded-day exclusion. Its
value is that its numbers are *true*.

Gamification is an **incentive layer** aimed at changing the behavior of the
person being measured (who is also the logger). That collides with the
instrument in three ways, all of which must be designed around:

1. **It can bias logging** — a score that rewards logging (or good days) creates
   missing-not-at-random data, the exact bias completeness gating exists to kill.
2. **It confounds correlations** — if the game drives a behavior (meditation,
   training), that behavior is no longer varying naturally; it's a co-intervention.
3. **It can leak blinding** — a "recovery stat" could expose readiness/experiment
   signals the coach deliberately hides.

The whole design is about getting the motivation without paying these costs.

## THE INVARIANT (the rule that makes it safe)

**The game layer is a read-only mirror.** It IMPORTS from the metrics / habits /
correlations / training services and EXPORTS NOTHING back. No game state ever
writes to `daily_metrics`, correlations, experiments, or the coach. It is a
faithful re-presentation of data computed elsewhere. If this holds, the
instrument stays honest.

## Two layers: permanent stats vs daily condition

RPG systems separate permanent attributes (STR, DEX — earned slowly) from
temporary state (HP, mana, status effects). The app maps to the same split, and
this split is load-bearing — it prevents the brittleness failure mode (you must
never "level down" from one bad night).

> ## ⚠️ SUPERSEDED 2026-08-04 — read this before the table below
>
> The band-function model described in this section was **replaced by a cumulative
> XP accumulator for ALL six stats**, and the Vitality frozen epoch was **reversed**.
> Implementation and the authoritative model docs are in
> `backend/app/services/game.py`. What changed and why:
>
> * **Why:** banding raw values means holding steady at a high level shows as no
>   progress. The user: *"as long as I'm pushing myself and working hard I should get
>   credit for that."* Note this is NOT a trajectory/derivative model despite the
>   word "trajectory" in the conversation that produced it — a derivative pays out
>   only while something is *changing* and reads zero at steady state, which is the
>   exact failure being fixed. It is stock-and-flow.
> * **The model:** `score` = today vs YOUR OWN rolling norm, in [0,2] (1.0 = at your
>   norm → full daily credit); `xp += 10 * score`, never negative; `level` = largest
>   L with `cum_xp >= 200 * L^1.7`. Cumulative XP never decreases, so a bad week
>   slows accrual and never costs a level.
> * **Vitality's frozen epoch is GONE.** The epoch (2025-01-09→04-09) landed on
>   the two highest HRV months on record, so the user measured at exact parity
>   despite a later month being the highest reading ever. It
>   turned a real all-time high into "zero progress". Percentile-vs-trailing-365d
>   replaces it — no privileged reference window anywhere in the system.
> * **The Schmitt-trigger hysteresis is unnecessary** under an accumulator (levels
>   can't fall), so it was not built.
> * **XP accrual is gated on real input availability** — see `services/game.py`.
>
> The stat/anchor reasoning in the table below is still correct and still worth
> reading; only the *level basis* column is superseded.

### Attributes — the character sheet (6 stats in v1, slow, RATCHETING)

Levels rise on *sustained real gains*; they decay only if a trend genuinely
reverses over weeks — never from a single day. "XP to next level" = the real
distance to the next band. A level is a band-function over data that already exists.

**Ratchet + decay must be an explicit Schmitt trigger,** or it will feel arbitrary:
level up when the metric holds above `T_up` for N consecutive days, down only when it
holds below `T_down < T_up` for M > N days. Write both thresholds and both dwell times
per stat.

| Stat | Level basis (DECIDED) | Anchor | Grounding |
|---|---|---|---|
| **Strength** | per-session/per-week **tonnage** as the spine; per-lift e1RM as the detail view | own history only | RELATIVE-ONLY — see below |
| **Endurance** | VO₂max sets the coarse band, CTL modulates within it | VO₂max age/sex norms (external) | strongest external anchor |
| **Vitality** | HRV **baseline** + RHR baseline vs a **frozen epoch** (2025-01-09 → 2025-04-09) | frozen epoch; RHR may use age norms | solid, and it MOVES |
| **Attunement** | rolling **SD of wake time in minutes** (continuous), + sleep debt, + morning light | own pattern | solid; upgrade the input, see below |
| **Constitution** | **FFMI** as the spine + protein consistency + illness resistance | FFMI absolute scale (external) | solid |
| **Discipline** | **cumulative XP accumulator**, difficulty-weighted, superlinear level curve | n/a (accumulator) | viable — see the accumulator note |
| **Mind** | DEFERRED to a later version — reframed inputs, see Guardrails | — | inputs were empty; reframed |

Notes:
- **Strength has no external anchor and must not claim one.** The original draft
  specified bodyweight strength standards; those tables are for barbell compounds, and
  this user's program is essentially all dumbbell accessory work. Epley is also weakest exactly there
  (isolation work often exceeds the reps ≤ 10 validity cutoff, so those sets drop out),
  and band-assisted movements produce garbage e1RM (`Pull Up (Band)` computes to 90.7 kg).
  Hence tonnage as the spine. **Normalize tonnage per session or per week, not
  cumulative** — otherwise adding exercises to the program reads as a strength gain.
  Getting an absolute anchor here is a training-program change (add barbell compounds),
  not a code change.
- **ACWR is NOT in Endurance.** The original draft put it there; it is a state variable
  that rises when you are acutely overreached, and it is already 20% of the readiness
  score. Empirically decisive: in one month ACWR sat well below 1 while CTL was near its
  peak — the fittest stretch of the year would have *lowered* Endurance. ACWR belongs in Condition.
- **Attunement should use a continuous wake-time SD**, not the existing binary
  `consistent_bedtime` / `consistent_waketime` (within ±60 min of a 14-day rolling mean,
  `routers/habits.py`). The binary discards most of the signal. Compute SD directly from
  `sleep_sessions` (years of nights available).
  - Seam: sunrise-relative morning-light credit only exists from **2026-07-18**
    (`weather_daily.sunrise`); earlier days use the fixed-hour fallback. Either include
    light only from that date forward or weight it low.
- **Vitality vs Constitution** are deliberately distinct (like Elden Ring's
  Vigor/Endurance/Vitality): Vitality = recovery *capacity* (HRV), Constitution =
  structural *robustness* (fuel + composition + illness resistance).
- **Attunement** was chosen over "Sleep"/"Regeneration" to foreground circadian
  regularity (the bed/wake-consistency + morning-light work). A later version
  could split timing (Attunement) from depth (Regeneration).
- **Constitution** was chosen over splitting into Sustenance + Physique, for a
  tighter radar. The split remains a valid future option if fueling and
  composition should move independently (the protein gap arguably deserves its
  own spoke). Body composition is IN (explicit user decision 2026-08-03). Band on a
  **30-day smoothed** body-fat value — the source is scale-derived bioimpedance, so
  day-to-day is noise and only the month-scale trend is usable. There is **no DEXA
  data and no DEXA table**; the original draft's mention of DEXA was wrong.

### The Discipline accumulator (decided 2026-08-03)

Discipline is a **cumulative XP accumulator**, not a rolling adherence rate: every
successful day earns points that never decay, so sustained good adherence keeps the stat
rising indefinitely rather than sitting flat at a percentage.

**Correction (2026-08-03):** an earlier draft of this section claimed the habits are
"pinned at a rail" and that a rate would therefore carry no information. That was wrong
— it counted `sum(col IS 1)` over `sum(col IS NOT NULL)`, which is *not* how the
adherence engine counts a manual habit (unlogged days belong in the denominator). Run
through the real engine over a 30-day window, the habits show substantial spread:

Rates across the manual and derived habits spread widely, with only one habit pinned at
a rail.

So Discipline is better founded than that draft implied, and a rate would in fact have
been informative. The accumulator is still the right choice — it ratchets, it never
punishes a bad week, and it matches the attribute model — but pick it for those reasons,
not because a rate was thought to be degenerate.

Two mechanics keep the accumulator from degenerating into a tenure meter
(level ≈ f(days elapsed)):

1. **Superlinear level thresholds** — each level costs more XP than the last, so a
   constant earn rate yields decelerating levels. Fast early, slow later, which is the
   correct shape for an attribute.
2. **Difficulty weighting** — a habit's daily point value ∝ (1 − its historical hit
   rate). A habit hit every day earns nearly nothing; one hit half the time earns real
   points. This is the variance monitor's logic in a costume, and it makes the stat
   measure discipline rather than attendance. **Point values drift as hit rates change
   — recompute them rarely (quarterly) or freeze them**, or the scale moves under you.

Missed days fail to add; they never subtract (forgiveness by design → the stat is
monotone non-decreasing).

### Condition — today's buffs / debuffs / ailments (STATE, not progress)

Daily fluctuating stuff lives here as status, never as permanent-stat changes:
- **Readiness / HP** — today's recovery score. Low = debuff, not a level loss.
- **Energy / Mana** — subjective energy (check-in).
- **Caffeine charge** — the caffeine `onboard_at_bed` PK metric: a daytime
  "stimulant buff" that becomes a "Restless" sleep debuff if still high at bed.
- **Status ailments** (all from real signals) — Sick day-flag → *Ill*; Travel
  day-flag → *Away* (stats held, not penalized); high sleep debt → *Exhausted*;
  the illness composite (elevated RHR + resp rate + skin temp) → *Run Down*;
  **ACWR > 1.5 → *Overreaching*** (moved here from Endurance — it is state, not
  progress). These are where it'll feel most game-like while staying 100% honest.

## Guardrails

- **Mind — HELD for a later version, and its inputs must be reframed.** The original
  draft's inputs turned out to be nearly empty in the data: two of its three specified
  inputs had only a handful of rows. The live sources are **subjective mood + energy**
  (stress and day score were too new to weight) and **auto-collected, categorised screen
  time**. Reframed, Mind becomes almost entirely auto-sensed.
  Whatever it ends up as, frame it DESCRIPTIVELY ("your mood/energy trend + how your
  screen time is distributed"), never causally ("you skipped meditation so your mind
  dropped"). That causal claim is exactly what the correlation engine is meant to TEST,
  not assert.
- **Logging bias — deliberately NOT designed around (user decision 2026-08-03).** The
  original draft required Discipline to be based on behaviors done rather than logged.
  For manually-logged habits that distinction does not exist — there is no independent
  sensor for "took creatine", and the adherence code already encodes unlogged = miss
  (`Habits.jsx`). The user's position: he logs honestly because he wants the logs to be
  right, and will not fake entries for a number. Accepted; Discipline may draw on
  manually-logged habits. **If a future user or a future self is less certain of that,
  this is the assumption to revisit first.**
- **No discrete jackpot on the randomized variable.** An earlier draft of this bullet
  said "never grant XP for a same-day action, all stats are multi-week aggregates."
  That was overbroad, and it contradicted the Discipline accumulator above (which earns
  daily by design). It was also bad game design — a tab that only changes every few
  weeks is dead on arrival. **Stats should update daily.**

  The actual hazard is narrow: a large discrete reward tied to a single same-day action
  on the variable currently under randomization. While the HIIT-vs-rest n-of-1 runs, a
  workout-completion jackpot would push toward training on REST-arm days (ITT stays
  unbiased under randomization, but the estimand shifts and power drops).

  This resolves itself without a special rule, because the chosen level bases are already
  smooth: CTL is a 42-day EWMA, so one session moves Endurance slightly rather than
  paying out. Keep it that way — daily movement, continuous inputs, no lump sums on
  training days.

## Design principles

- **Forgiveness by design.** Rolling/decaying, not brittle streaks. Direct
  evidence that punishing mechanics get abandoned: the variance monitor flagged
  a habit that had been abandoned outright. The forgiving substrate (adherence %) is the right base.
- **The radar surfaces imbalance without scolding.** A 7-spoke radar shows a
  lagging stat (e.g. Constitution while protein adherence is low) as an honest visible
  gap, not a guilt-trip push. This is how you get "your stats are languishing"
  motivation without the guilt failure mode.
- **Passive mirror vs active nudger — OPEN DECISION.** Notifications are where the
  game reaches in and confounds behavior. Recommend starting PASSIVE; never nudge
  a variable that's under active experiment.
- **No boss fight.** Health is chronic — the win condition is trend + maintenance.
  Celebrate personal-bests and consistency, not a finish line.

## v1 scope (DECIDED 2026-08-03)

**Six spokes: Strength, Endurance, Vitality, Attunement, Constitution, Discipline.**
Plus the radar, level-up moments, and the Condition strip. **Mind is held** for a later
version pending the reframe above.

Six was chosen over four deliberately. A 4-spoke radar renders as a diamond and barely
reads as a radar; more importantly, **adding a spoke later rescales the whole chart
geometry**, making every earlier screenshot and memory of the radar incomparable. Fix
the shape now.

Most of the work is presentation + threshold design + juice, not new computation — but
see "Codebase reality" for the parts that genuinely do not exist yet.

---

## Data reality (verified against the live DB, 2026-08-03)

They were queried directly against the live database. They are why the stat table changed.

| Source | Coverage (shape, not values) |
|---|---|
| `daily_metrics.ctl` | ~1.5 years of daily rows. Rose sharply over one training block — the biggest real change in the dataset. |
| `hrv_baseline` / `rhr_baseline` | ~1.5 years. Both moved steadily in the healthy direction over the period. |
| `sleep_sessions` | ~2 years of nights, ~30/month coverage, complete. |
| `hevy_sets` / `hevy_workouts` | A few thousand sets; dense only from the spring the training block began, sporadic before. All dumbbell accessory work. |
| `vo2_max` | ~130 samples. Moves slowly. |
| body composition | Scale-derived lean mass, weight and BF%, internally consistent (so it is a function of weight, not an independent measurement). |
| `food_log` | Only ~3.5 months at the time. |
| `habit_logs` | Under 100 rows — logging had begun ~3 months earlier. Adherence over a 30-day window runs ~40–80% across habits — see the Discipline section, and do NOT recompute these as `IS 1 / IS NOT NULL`, which gives wrong answers for manual habits. |
| `subjective_log` | ~100 rows; mood and energy on nearly every one, stress and day score only recently added. |
| `screentime_events` | ~12k auto-collected events over ~7 weeks, categorised. |
| `caffeine_log` | Just shipped — the "Caffeine charge" buff had effectively no data yet. |

**Non-orthogonality is real and visible:** Vitality and Endurance both rose on the same
training build, so those two spokes move together. Sleep debt feeds Attunement *and*
readiness/HP; HRV feeds Vitality *and* readiness. A radar implies independent axes;
these are not fully independent. Enforce disjoint input sets where possible and document
each stat's inputs.

---

## Decisions locked 2026-08-03

1. **Do NOT filter lifting out of CTL for Endurance** — deferred, see below.
2. **Strength spine = tonnage**, per-lift e1RM as the detail view.
3. **Vitality = frozen epoch** (2025-01-09 → 2025-04-09, the first 90 days of continuous
   HRV). Put the epoch and every band edge in **named constants** so re-scaling later is
   a one-line documented change — a frozen epoch eventually saturates if HRV keeps
   climbing.
4. **Six spokes**, Discipline included, Mind held.

### Deferred — filter lifting out of the Endurance CTL

`daily_load` is Banister TRIMP, which is HR-based, so lifting sessions feed CTL and
partly inflate Endurance. `services/training_load.py` documents that TRIMP structurally
under-weights resistance work, so lifting's CTL contribution is a distorted partial
signal — neither good cardio data nor good strength data.

Not fixed in v1 because lifting volume was small at the time, a rounding error next to
the cardio that drove most of the CTL rise. Note the contamination in the stat's
info tooltip.

**Revisit when lifting volume grows.** The fix is a second CTL-like EWMA computed over
non-strength workouts only (key off `hevy_workouts.apple_uuid` linkage to identify
strength sessions — imperfect, misses unlinked ones), which means a parallel load
pipeline maintained only for the game, plus a full 42-day-EWMA history recompute.

---

## Codebase reality (checked 2026-08-03 — read before estimating)

The invariant says the game layer "imports from the metrics / habits / correlations /
training services." **That premise is false as the code stands**:

- **There is no habits service.** Sleep regularity (`_consistent_bedtime`,
  `_consistent_waketime`) and morning-light credit (`_morning_light_days`) live inside
  `backend/app/routers/habits.py`, entangled with route handlers.
- **There is no adherence engine.** Adherence % is computed **client-side** in
  `frontend/src/tabs/Habits.jsx` (`AdherenceCard`). A backend service cannot import it.
- **Per-session e1RM** is computed in the route body of `backend/app/routers/hevy.py`
  (`/strength/e1rm`), not in `hevy_sync.py` — only the scalar `epley_e1rm` is a service.
- **`routers/streaks.py` uses the brittle logic this design rejects** (any unlogged day
  breaks the streak, no gap tolerance). Do not reuse it.

So v1 needs either duplicated band computations in a new `services/game.py` (guaranteed
drift) or a small extraction refactor pulling regularity, morning-light, adherence and
per-session e1RM into services with the routers as thin callers (~250 lines moved, no
behavior change). **The extraction is the right call but is a refactor — confirm before
starting it.**

Other build notes:

- **Level-up events require persisted prior state.** You cannot emit "you leveled up"
  without remembering yesterday's level, so the feature needs its own table (next
  Alembic revision after 0030). This does not violate the invariant — restate it
  precisely: *the game may write only to game-owned tables, and no analysis path may
  ever read them.*
- **Precompute daily in the morning pipeline.** Banding six stats over 19 months on
  every tab load, against SQLite via async SQLAlchemy, will be slow, and levels are
  slow-moving by design. Add a step to `services/morning.py` after the recompute; that
  is also the natural home for level-up detection.
- **Placement DECIDED 2026-08-03: a card on the Today tab, not a 7th nav tab.**
  `components/Nav.jsx` renders 6 flex-1 tabs; at 375 px that's ~62 px each and a 7th
  makes labels unreadable. The character sheet goes **near the top of Today**, in a
  **collapsible card that is CLOSED by default** so it costs no vertical space while the
  design is still being iterated on. Use the existing `Collapse` wrapper from the Trends
  tab rather than a new pattern. Promote to its own tab later if it earns the space.
- **Blinding is not at risk** from the Condition strip: readiness is already user-visible
  on Today (the coach hides its *mechanism*, not the numbers).

## Why this is worth doing

The reason it's a good idea is the same reason it's risky: the numbers are true.
Most health games lie (arbitrary points, Goodhart-baited scores). This one
wouldn't — the stat costume is an honest re-presentation of real measurement.
Protect that, and it's a rare thing: a health game that doesn't lie to you.

(Deliberately NOT built: a single global "score" — a scalar projection whose
weights hide all the arbitrariness, and a Goodhart trap. If ever added, make it
fully decomposable and never a logging target.)
