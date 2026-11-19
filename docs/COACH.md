# Coach — weekly plan (shipped 2026-09-12, note 29)

The Today tab's coach card on every day no **training** experiment is running.
Code: `backend/app/services/coach_plan.py` (`plan_for_day`). Experiment days still use
the blinded arm path in `services/coach.py`, unchanged.

## Why it replaced `_training_rec`

- Experiment 1 set the card on 47 of 63 days by coin flip and contradicted the metrics
  96% of the time; the daily summary was told "never contradict the call" and "justify
  it with a number", so it rationalised coin flips (see case 1 in ENGINEERING.md). Experiment 1 ended
  2026-09-12.
- The fallback rule, `_training_rec`, forced EASY whenever any one of four signals
  crossed a soft threshold (about half of all days), never read the readiness score, and
  in practice its REST calls were ignored about as often as they were followed.
- The real decision is **which of the open days** to take a class, against a weekly target —
  not "how hard today". Class content is set by the gym.
- An observational check (sick and travel days excluded, adjusted for same-morning readiness
  and energy) found no readiness band in which a class day was followed by worse next-morning
  energy, and readiness itself did not predict next-morning energy once same-morning energy was
  known. So readiness is not an input: the default is "go", and only red flags stop it.

`_training_rec` is NOT retired: experiment eligibility still reads it.

## Layers

1. **Class today?** A weekly target (`WEEKLY_CLASS_TARGET`, default 3) per Mon–Sun week.
   - **Hard stops → REST (red):** gym closed, `sick` or `travel` day flag,
     illness early-warning `elevated` (today's row only), or 3 consecutive training days.
   - **Soft signals** — HRV z < −1.5, RHR z > +1.5, last night < 6 h sleep: if the week has
     room → **REST (amber)** "better another day"; if not → **CLASS**, "keep it controlled".
   - Otherwise **CLASS**, with week position ("1 of 3 done, 4 open days left").
   - **ACWR** only softens effort: > 2.0 "go lighter", > 2.5 "easy effort only". The bands
     were set against the user's own ACWR distribution, so they fire on a small minority of
     days rather than on every busy week.
2. **4th class (earned).** Offered (**+ 4TH**) only when exactly 3 are done, no hard stop,
   no soft signal, the mean of ≥4 of the last 7 HRV z-scores ≥ 0, sleep debt ≤ 1 h,
   ACWR ≤ 2.0. Otherwise **TARGET MET**. Not an experiment — review after ~8 such weeks.
3. **Pull block (home).** 2 a week, ≥48 h apart, not on sick / illness-alarm days
   (a closed-gym day is fine — it's at home). Line shows back hard sets this week
   (lats + upper_back + traps) against 10+. **Only on days it FITS** (2026-09-22, note 44):
   a REST / TARGET MET day, a closed-gym day, or after the class is done — offering it on class
   days too meant the identical sentence 14 days running. After `PULL_DORMANT_DAYS` (14)
   with no HOME block it stops repeating and asks ONCE, on Mondays: keep 2/week, drop to
   1, or bin it.
   **Leg block (home, 2026-11-01).** 1 a week, on the same days the pull block fits, never on a rest
   called for sickness, travel, an alarm or a training streak: Bulgarian split squat, single-leg RDL, single-leg calf raise, 3 sets each
   with the heaviest dumbbell or the kettlebell (about 9 sets, 20 min). Log it in Hevy titled
   `HOME – Legs` with the weight entered: bodyweight-only sets of these don't count as building
   sets. Any HOME title containing LEG counts here, not as a pull block. After 14 days with none
   it asks once, on Mondays. One block adds about 3–4.5 sets per muscle, so it narrows the leg gap
   rather than closing it; move to 2 a week only if 1 sticks.
4. **Protein.** One line only when `protein_pace` says meaningfully behind
   (the goal follows `training_goal`, which switched cutting → building 2026-09-12).

5. **ALSO line** (`_extra_line`, note 44). At most one per day, drawn from the user's own
   findings and stating its own strength (confirmed vs suggestive): yesterday's drinks
   when today's HRV is actually down (3-day backstop), sleep debt ≥ 1 h, unusually low load (ACWR < 0.85, every other day), a Zone 2 nudge on rest
   and closed-gym days (every 3 days), and a run prompt when the
   last run is ≥ 28 days old (every 14 days). Candidates with `every` are throttled
   deterministically by date — a CHRONIC fact ("no run in 100 days") is true daily and
   became the nag it replaced in the first replay. Rotation is by date ordinal; when
   nothing qualifies the card says nothing. Replay 2026-09-09..22: 5 lines, 4 kinds, no
   day-to-day repeat, against 14 identical pull lines before.

Vocabulary: CLASS · REST · TARGET MET · + 4TH, plus + PULL / ✓ PULL, PROTEIN and ALSO lines.
A future training experiment would still show its arms as HARD/REST.

## Logging

- **Pull blocks: Hevy only, title starting `HOME`** — case-insensitive, so "Home – Pull"
  works too. No Apple
  workout: a Functional/Traditional Strength session counts as a gym class; Flexibility,
  Yoga, Cool Down count as a walk; anything else counts toward the 3-in-a-row stop.
- **Tape: Body tab → TAPE card**, inches in, cm stored (`body_measurements`, Alembic
  0061). Same-day entries merge. Weekly, same morning, before eating. Today's log view
  shows a WEEKLY "Tape measurement due" row under HABITS once the last entry is ≥7 days
  old; it taps through to Body.

## Pushes and summary

- 21:00: `Coach tomorrow: LABEL` + detail, from `plan_for_day(tomorrow, morning=True)` —
  provisional, since overnight HRV/RHR/sleep don't exist yet.
- Morning: `Coach: LABEL`. The daily summary receives label + detail and is told to
  **state the call and never invent a reason for it**, reporting contrary numbers as they
  are.

## Known limits

- The history replay used for sign-off runs on today's recomputed metrics, not the
  morning snapshot each call would actually have seen.
- The scale's body-fat % is ~a function of weight (R² 0.990, 26 weeks), so the
  COMPOSITION card's lean/fat split and the Katch-McArdle RMR both inherit weight's
  trend. Recomposition is only visible via tape (or DEXA).
- Per-lift progression is not tracked: class exercises vary too much (hundreds of distinct
  exercises, only a handful of sessions per lift). The pull block's movements are the anchor lifts.
