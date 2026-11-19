# Engineering notes: seven case studies

A personal health app has an unusual failure mode: nothing crashes. A number is just quietly
wrong, the chart looks plausible, and a recommendation gets built on it. Most of the work in
this codebase is about catching that. Each case below follows the same shape: what was wrong,
how it was found and measured, what changed, and how the change was verified.

The figures are results about the *method* (reliabilities, error spreads, counts of rows that
changed). None of them describe the person whose data this ran on.

Short on time? Read 1, 2 and 4: a model rationalising a coin flip, testing whether a measure is
reliable enough to act on, and a placebo test for confounding.

---

## 1. A model asked to justify a coin flip, and did

This one is an observation about model behaviour, not about the data. It rests on a handful of
cases, so it is written as an observation plus the test it motivates, not as a finding.

**Setup.** The app ran a randomised self-experiment: each evening a coin assigned the next day
to "train hard" or "rest", and the morning briefing carried that call. The briefing generator
(Claude Haiku 4.5 at the time) was not told the call was random — that was the blinding — and
its prompt said:

> TODAY'S COACH CALL (already decided by the app): X … do NOT contradict or second-guess it

while the output template asked for the call "verbatim" plus "one bullet of justification
citing a specific number".

**What happened.** On 45 of the 47 coin-flip days the call contradicted the app's own
metric-based rule. The user noticed two consecutive mornings: strong recovery called REST,
poor recovery called HARD, each with a fluent, number-backed justification that contradicted
the numbers next to it. One reads, verbatim:

> Call: HARD … recovery floor does not support it—HARD session will not worsen a clean state.

The model saw the contradiction, said so in passing, and produced a reason anyway. When one of
those mornings was later regenerated from the same data, the new briefing rationalised its call
exactly as the original had. The justification tracked the label it was handed, not the
evidence.

**Reading it correctly.** This is not unfaithful reasoning in the strict sense: the decision was
never the model's. It is an instruction to justify overriding honesty about the evidence. When
the coin and the data disagreed, a made-up reason was the only output that satisfied the prompt.
The model was following instructions, which is exactly why the pattern matters: "always explain
the decision" is a common instruction in deployed systems. How often it happened across all 47
briefings was never counted, so the rate is unknown.

**What changed.** The coin was retired. The briefing is now told to state the call, never to
invent a reason for it, and to report contrary numbers as they are. Later rounds of briefing
fixes produced three more small observations, each from two to four dry runs on a copy of the
database:

- **A rule in the prompt alone did not hold.** With a system-prompt rule against blaming an
  ordinary night on one cause, both dry runs still attributed a within-noise dip to a single
  cause; another line in the same prompt had invited exactly that reading. Removing that line
  and printing a tag beside the value ("[WITHIN NORMAL RANGE — attribute it to nothing]")
  stopped it in both final runs. The two changes went in together, so their separate effects
  are unknown.
- **Counting belongs in code.** Describing an up-and-down week, two of two drafts miscounted a
  run of low nights. With the count computed in code and printed as a labelled fact, three of
  three quoted it, but one also added its own "fourth straight". With a check that rewrites a
  contradicting count in the finished text, three of three were right.
- **Quoting a failure as a warning primed it.** Adding the bad sentence, with its live numbers,
  to the prompt as an example of what not to write made the next draft echo it (one dry run;
  not repeated).

**The test this motivates (not yet run).** A small, pre-registered eval:

- **Inputs:** synthetic daily metric blocks in the app's own format, each paired with a call
  that is either consistent with the data or drawn at random.
- **Prompt conditions:** "justify it with a number and never contradict it" (the original);
  "state the call and report any contrary numbers"; and a neutral version.
- **Measures per output:** fabricated support (a cited number argues for the label when the
  data argue against it); acknowledge-then-rationalise (the pattern quoted above); explicit
  flagging of the contradiction; numbers not present in the context.
- **Models:** several, since the effect may depend on the model.

A few hundred calls would answer it either way. If honest flagging wins under the neutral
prompt, the finding is about instruction pressure. If it does not, it is about the models.

---

## 2. Measuring HRV in the right window, then admitting it is a noisy measure

**Problem.** Heart-rate variability is a recovery marker, so it has to be read in a fixed
physiological window. The first version averaged every sample in the local calendar day.
That mixed overnight readings with daytime ones, which run markedly lower, so a day's value
depended on how many daytime samples had arrived by the time it was computed. The morning
readiness score was inflated by up to 46 points and then drifted down through the day.

**Fix 1.** HRV is the mean of the readings inside the sleep window, keyed to the wake date.

**Problem, later.** A morning briefing strongly recommended training on an HRV z-score near
+2.8. That night had four readings, and the call had been made from the first two. With all
four, the night was unremarkable.

**Measured.** Apple writes only 3–5 spot readings a night, so the measure itself was tested.
Split-half reliability (odd vs even readings, 276 nights) was 0.22, or 0.36 after the
Spearman–Brown correction. The same check on overnight 10th-percentile heart rate gave 0.98.

**Fix 2.** A night needs at least three readings, and they are combined with a geometric
mean, because HRV is roughly log-normal and one high reading should not carry the night.
A full-history recompute on a copy of the database moved the HRV z-score by a median of 0.12
and readiness by a median of 1.3 points.

**What made it careful.** The definition changed in the middle of a running randomised
experiment whose primary outcome is HRV. The change applied to both arms, was chosen without
looking at any arm split, and is written into the experiment's record as a dated amendment.
The geometric mean was chosen partly on how it moved two known findings, so those gains are
recorded as *not* evidence that the new measure is better: that would be a forking path.

---

## 3. Heart-rate recovery measured from the end of effort, not the stop button

**Problem.** HRR60 (the heart-rate drop in the 60 s after exercise) was computed from the
moment the workout was stopped. Class sessions end with a few minutes of stretching before
the stop, so "60 seconds after the stop" was really several minutes after the effort.

**Measured.** Across the gym sessions with heart-rate data, effort ended a median of 201 s
before the stop.

**Fix.** The late peak is the maximum of the final five minutes. The end of effort is the
*last* sample within 8 bpm of that peak. HRR60 is that heart rate minus the lowest value
50–70 s later. If the late peak is under 85% of the session maximum, there was no real
finish effort and the value is left empty rather than guessed.

**Verified.** Over 99 gym sessions, the session-to-session change fell from 13.7 to 9.2 bpm
and the standard deviation from 13.1 to 9.1. A less noisy measure of the same thing is the
expected signature of a better definition. Apple's own recovery figure could not serve as a
check: it existed for only three sessions, all walks.

---

## 4. A placebo test for confounding in a correlation engine

**Problem.** The engine correlates daily behaviours with outcomes, usually behaviour on day
D against the outcome the next morning. A correlation can survive every multiple-testing
correction and still be confounded: a third factor drives both, or the outcome drives the
behaviour.

**The test.** For every pair, also correlate the behaviour on day D with the outcome on day
D − 1. Today's behaviour cannot have caused yesterday's outcome, so a reverse correlation as
strong as the forward one is the signature of confounding. For the pairs that survive, a
7-day circular block bootstrap of the forward-minus-reverse difference says whether forward
clearly beats reverse.

**Getting the labels right.** A first cut called a pair "reverse similar" when the reverse ρ
was at least half the forward one. The plain label CONFOUNDED was adopted only after adding a
second condition: the reverse ρ's own 95% interval must exclude zero. Without it, four pairs
whose reverse correlation was indistinguishable from zero were being accused. Failing to
show "forward beats reverse" at n ≈ 40 is low power, not confounding, and the UI says so.
Inputs that are 7-day rolling windows share six of seven days with the reverse day, so they
fail by construction and are marked not testable rather than reported as failures.

**Verified.** The change was additive. All 941 existing fields in the correlation cache were
byte-identical with and without it, and the weekly refresh went from about 2 s to 3 s.
Confounded findings are kept out of every recommendation and shown dimmed, but the briefings
still see them, labelled, because they are interesting.

**A related lesson.** Benjamini–Hochberg tiers are knife-edge. Adding four days of data once
moved a handful of findings between "confirmed" and "suggestive" while no effect size moved
by more than 0.04. The app judges findings by effect size and interval, not by tier churn.

---

## 5. Time zones: a travel timeline that costs nothing at home

**Problem.** The app ran on one configured zone. On a trip seven hours away it put the day
boundary at 07:00 local, sent the evening push at 04:00 and filed nights of sleep under the
wrong date.

**Design.** A resolver decides the zone at any instant: a manual period first, then the
offset observed in the most recent webhook (if under 48 h old), then home. Days abroad
become *away segments* whose edges fall on the away zone's midnights, so local dates stay
contiguous and monotonic across the flight. Every Python and SQL date helper is
segment-aware. With no segments, each helper emits **byte-identical SQL** to the old
expression, so home days pay nothing. SQL and Python agreed on all 48,804 rows tested.

**A data-source quirk, proved.** Health Auto Export stamps *every* timestamp in a payload
with the phone's offset at export time. A re-export from home carried the home offset on
every sample, nights abroad included. So sleep is keyed by the app's own zone timeline, and a
re-export can never re-date a night.

**A bug it then caught.** At the first DST change after shipping, the last observation
(correct when taken) stayed "fresh" for 48 hours and was compared with home's offset *now*.
It no longer matched, so the resolver decided the user was abroad, the scheduler followed,
and the app asked "where are you?". The fix judges an observation against home's offset at
the moments it describes. A payload that mixes home's two offsets is no longer "mixed", and
the same rule covers the spring change.

---

## 6. The deploy that shipped a blank screen, and the check that now stops it

**Problem.** A bundle-splitting change (`manualChunks`, vendor and chart code in separate
chunks) shipped a blank black screen for about five hours. The charting library's
dependencies read React internals before the vendor chunk had evaluated. `vite dev` does
not bundle, so it never showed up in development. The post-deploy smoke test checked the API
and that `/` contained the root element, never that the JavaScript actually started.

**Fix.** `frontend/scripts/bootcheck.mjs` loads the built entry module in happy-dom twice:
logged out (expect the sign-in screen) and logged in with every API call answered `[]`
(expect the dashboard). It fails on any uncaught error. The deploy script runs it before
syncing and refuses to deploy on failure, and CI runs it on every push.

**Verified.** Rebuilt with the bad configuration, it failed with the exact production error.
The deploy script also tags the running image before each deploy and rolls back
automatically if the health check or smoke test fails.

---

## 7. One prompt, two averages for the same week

**Problem.** A note from the app's own notes queue: *"I don't trust the averages and trends
the AI summary gives for calories or protein."*

**Audit.** Before changing anything, every figure was checked against the data actually fed
to each summary, rebuilt on a copy of the database:

- The arithmetic was right. Weekly means recomputed from raw rows matched exactly (4 of 4).
- Yesterday's intake was quoted exactly in 28 of 28 daily briefings.
- But the weekly and monthly *overall* averages included days flagged as incompletely logged,
  while the week-by-week trend block left them out. In a week with a trip, one prompt
  carried two averages for the same week, about 30% apart.
- The trend block listed weeks oldest first as bare numbers joined by arrows. Where a trip
  week showed as `--`, the model counted to the wrong week: "down from X two weeks ago" when
  X was last week, and a days-at-goal count lifted from the wrong week.

**Fix.** Both averages now leave out incomplete days and name the days they dropped. Each
week in the trend block is on its own dated line, with its own days-at-goal and a LATEST tag.

**Verified.** Dry runs of real past summaries on a database copy. The overall and trend
figures now agree, and the summary that had said "two weeks ago" now says "last week" with
the right count.

The rules this case leaned on (compute countable things in code, put facts beside numbers)
are the observations in case 1.

---

## Smaller rules, each earned by a measured failure

- **Store the response, then send it.** The idempotency layer for offline replays first sent
  and then stored. A fast retry landed in between and double-logged the entry.
- **Text timestamps compare as text.** SQLAlchemy binds a `datetime` with microseconds
  (`'… 00:00:00.000000'`), and against 19-character stored strings a sample exactly on the
  bound sorts *before* it. Exact range deletes use literal 19-character bounds or delete by id.
- **Tests must not trust the environment.** The test fixture used `setdefault` for its
  database URL. A run inside a container pointed at a database copy used that copy, and one
  test cut its sleep table from 852 rows to 2. The fixture now forces its own temporary
  database.
- **Absent is not zero until logging began.** Event metrics are zero-filled only from the
  first day they were ever logged. Before that, a new metric with a handful of rows reported
  hundreds of invented zero-days against year-long outcomes.
- **Refactors ship byte-identical.** Splitting the metrics router, vectorising the
  change-point bootstrap and rolling raw samples into hourly rows were each checked by
  comparing every endpoint's output (or every day and hour total) before and after.
