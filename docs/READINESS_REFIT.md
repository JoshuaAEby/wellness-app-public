# Readiness refit: pre-registered plan

**Written 2026-11-01, before any candidate was scored.** Fixes what `compute_readiness` promised:
weights tested at n ≥ 150 finer-grained check-ins, adopted only if better out-of-sample. Change
this plan only by a dated amendment at the bottom, and only before the comparison is run.

## What this can and cannot do
Expect steadier, not more accurate. From ρ(HRV z, energy) +0.29, ρ(RHR z, energy) −0.29 and
ρ(HRV z, RHR z) −0.44, any HRV/RHR split from 45/25 to 25/45 predicts about the same relation to
energy (0.336–0.342). The aim is fewer label changes that are only noise.

The stakes are limited. **Rho's call does not read readiness** (`coach_plan.py`: "it is not an
input") and `_strength_hint` reads the components, not the composite. Readiness reaches the
Today ring, briefing and summary text, class board display, game HP, trends and the stratified
correlations. Find every consumer with grep before recomputing.

## Already seen (2026-05-01..11-01, none of it scored on a candidate)
| Measure | Value |
|---|---|
| Nightly HRV split-half / Spearman–Brown (276 nights) | 0.22 / 0.36 |
| Overnight HR p10 Spearman–Brown (351 nights) | 0.98 |
| Readiness vs energy / day score | ρ 0.34 (170) / 0.08 (87) |
| HRV z vs energy / day score | ρ 0.29 / 0.04 |
| RHR z vs energy / day score | ρ −0.29 / −0.06 |
| Sleep debt vs energy / day score | ρ −0.06 / 0.11 |
| Sleep debt spread | SD 0.26 h; 63% of days ≤ 0.5 h |
| Readiness spread | SD 15; median daily change 14.3 pts |
| Label changes | 98 of 167 day pairs |
| Sleep hours vs energy, ledger holdout | ρ +0.52 |

**Forking-paths note:** candidate D exists because of that last row and the debt rows, so D is
partly selected on these targets. Days after 2026-11-01 are the cleaner test of it; see the
sensitivity check below.

## Trigger
Run once, when BOTH hold on days from 2026-07-12 on (when the 0.1-step sliders began):
- at least 150 days with `subjective_log.energy`
- at least 150 days with `subjective_log.day_score`

On 2026-11-01 these stood at about 100 and 85. At the recent pace, day score is the limit:
mid-to-late January 2027. Run it once. Do not check candidates early.

## Candidates (fixed; none may be added)
Same component scales, renormalising and suppression as `compute_readiness`, and the same label
cut points (75 / 50 / 25).

| | HRV | RHR | Third part (30%) |
|---|---|---|---|
| A (today) | 45% | 25% | sleep debt |
| B | 35% | 35% | sleep debt |
| C | 25% | 45% | sleep debt |
| D | 25% | 45% | last night's sleep |

D's component is on debt's scale: `100 − 50 × max(0, target_sleep_hours − last night's hours)`,
clipped to 0–100, using `sleep_sessions` for that wake date (naps merged, as stored).

## Data
- Days from 2026-07-12, using each day's stored HRV z, RHR z and debt at analysis time.
- Drop days flagged `travel`, `sick` or `travel_recovery`, the same normal-day frame the findings
  use.
- Every candidate is scored on the same days. A day needs at least HRV or RHR.

## Measures
- **Accuracy:** Spearman ρ of the score with same-morning `energy`, and with same-date `day_score`.
- **Steadiness (decides between candidates):** share of consecutive available day pairs where the
  label changes.
- **Reported, not decisive:** median |daily change|; 95% paired 7-day block-bootstrap interval for
  ρ(candidate) − ρ(A) on each target.

## Decision rule
1. A candidate qualifies if ρ(candidate) ≥ ρ(A) − 0.05 on BOTH targets.
2. Of those, take the lowest label-change rate.
3. Adopt it only if that rate is at least 5 percentage points below A's. Otherwise keep A: a
   smaller gain does not justify rewriting history.

Ties go to the candidate closest to A, in the order B, C, D.

**Sensitivity, reported and not decisive:** the same table for all days including flagged ones,
and for days after 2026-11-01 only. If the chosen candidate would fail rule 1 on the post-11-01
days, say so plainly in the result.

## Bias notes
- Morning energy is rated in a full-screen takeover before Today, whose copy says "before you
  review your metrics", so the score is normally unseen. A push (Rho's call) can arrive first.
- Day score is rated in the evening, after the score has been on screen all day, so it is the more
  exposed of the two. Its near-zero ρ so far suggests little effect.
- Both targets share last night's sleep with the score. This tests "does it match how I feel", not
  whether it predicts anything later.

## If a candidate is adopted
Prod copy first → list what changes for every consumer → manual backup → change the weights →
recompute all days → correlation refresh → a dated amendment record in the project notes. Experiment primary
outcomes do not use readiness; `experiment_assignments.readiness_at_assignment` stays as recorded.

## Result
Not run.

## Amendments

### 2026-11-01 (before any candidate was scored)
**Inputs stay objective.** A readiness score may use only tracked health metrics. Check-ins
are the test of a candidate and never an input to one.

**Candidate E added: sleeping respiratory rate.** Chosen from measures that used no check-in:

| Measure | Sleeping resp rate | Wrist temp |
|---|---|---|
| Nights, since 2025-11-01 | 362 | 355 |
| Readings per night | 47 (median) | 1 |
| Spearman–Brown reliability | 0.97 | can't be split |
| Lag-1 ρ | not measured | 0.04 |
| ρ with RHR z / HRV z | +0.34 / −0.21 | not measured |

Resp rate is as reliable as overnight HR and adds to it rather than repeating it. Wrist temp stays
out: one reading a night, and no carry-over from night to night. Earlier findings (alcohol and
training raise next-morning resp rate) and a past illness spike were known when this was added,
but its relation to energy or day score was not looked at.

| | HRV | RHR | Resp rate | Sleep debt |
|---|---|---|---|---|
| E | 25% | 30% | 15% | 30% |

Component: mean `respiratory_rate` in that night's sleep window, needing at least 3 readings;
z against the prior 60 nights, needing at least 14, with `travel`, `sick` and `travel_recovery`
nights left out of the baseline; scored `(−z + 2) / 4 × 100`, clipped to 0–100, the same scale as
RHR. A night without it drops the component and renormalises, as with the others.

Order for ties is now B, C, D, E. Every other rule stands.

If E wins, adoption also needs a stored nightly value and z (migration + backfill), since neither
exists today. A hard session will lower the next morning's score through this component. That is
intended: the score reports recovery state, not whether something went wrong.
