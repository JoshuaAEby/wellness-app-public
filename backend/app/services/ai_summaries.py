"""Shared Claude AI summary generation for daily, weekly, and monthly insights."""
import calendar
import json
import statistics
import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional

from app.services import ai_health
from app.services.nutrition_goals import resolve_protein_goal
from app.timeutils import local_today
from app.foodday import food_day, food_day_end, food_day_start

logger = logging.getLogger(__name__)

# Food parsing + daily summary run on Haiku (cheap, high-frequency). Weekly/monthly
# step up to stronger models for synthesis — they run 4×/month and 1×/month, so the
# cost delta is trivial and the analysis quality gain is where it matters.
MODEL          = "claude-haiku-4-5-20251001"   # blood-text parse, recipe estimates, _log_call default
# Food parse → Haiku 5.5 (2026-10-20, note 71). Audit (scripts/haiku55_audit.py, 60 real
# entries): 0/120 failures vs Haiku 4.5's 3/60 empty replies; ~6x cheaper per parse; closer to
# a Sonnet 5.5 reference on 10 of 15 large disagreements. Trade-off accepted: it rejects
# `temperature`, so a NEW description can re-parse a few % differently (median 2.8%) — exact
# repeats still come from food_parse_cache. Default thinking (adaptive, effort medium):
# effort=low was no more consistent and barely cheaper.
FOOD_PARSE_MODEL = "claude-haiku-5-5"
# Daily briefing → Sonnet 5 with thinking OFF (2026-10-04, note 55): Haiku misquoted
# rho and half-invented a finding. Measured cost from api_call_log: Haiku $0.23/mo;
# Sonnet 5 thinking-off ≈ $0.60–0.75/mo; thinking-on ≈ $0.75–1.90/mo (weekly's output went
# 808 → 4,232 tokens when it moved to Sonnet 5 with adaptive thinking left on).
MODEL_DAILY    = "claude-sonnet-5"
# Bumped to the Claude 5 family 2026-09-09. Sonnet 5 is both newer and CHEAPER than
# Sonnet 4.6 ($2/$10 per M vs $3/$15); Opus 5 is the same price as Opus 4.8. The pricing
# table below already carried both, and services/food_tags was already on Sonnet 5 — only
# these two constants were stale. Haiku 4.5 remains current for daily + food parse.
MODEL_WEEKLY   = "claude-sonnet-5"
MODEL_MONTHLY  = "claude-opus-5"

# NO PROMPT CACHING HERE — DELIBERATE, AND DO NOT "RE-ADD" IT.
#
# Every system prompt in this module and in food_tags.py used to carry a
# `cache_control: {"type": "ephemeral"}` marker. All eight were no-ops. The
# minimum cacheable prefix is model-dependent, and a prefix under it silently
# fails to cache — no error, just cache_creation_input_tokens: 0:
#
#     Haiku 4.5 / Opus 4.6 / Opus 4.5 ... 4096 tokens
#     Sonnet 4.6 / Sonnet 5 / Opus 4.8  ... 1024 tokens
#     Opus 5 ............................. 512 tokens
#
# Measured 2026-09-02 against the live API: FOOD_PARSE_SYSTEM renders a 2399-token
# request on Haiku 4.5 and two back-to-back identical calls both returned
# cache_creation=0, cache_read=0. Every other prompt here is smaller still
# (daily ~1230, weekly ~690, monthly ~660, blood ~420, label ~270 tokens), so
# none of them reached their model's floor either.
#
# Even if a prompt grew past its floor, caching would only pay off where calls
# land inside the 5-minute TTL (a write costs 1.25x, a read 0.1x; break-even is
# two requests). The summaries run daily/weekly/monthly — every marker would be a
# pure 1.25x surcharge. Food parsing does burst, so that one is the only
# candidate, and it would need its prompt to nearly double to qualify.
#
# Pricing, USD per token, per model. One global Haiku pair used to stand in for
# every model, so Sonnet and Opus calls were priced at Haiku rates — and the
# Haiku rates themselves were 20-25% low. Verified 2026-09-02.
MODEL_PRICING = {
    "claude-haiku-4-5":          (1.00 / 1_000_000,  5.00 / 1_000_000),
    "claude-haiku-4-5-20251001": (1.00 / 1_000_000,  5.00 / 1_000_000),
    "claude-sonnet-4-6":         (3.00 / 1_000_000, 15.00 / 1_000_000),
    "claude-sonnet-5":           (2.00 / 1_000_000, 10.00 / 1_000_000),
    "claude-opus-4-8":           (5.00 / 1_000_000, 25.00 / 1_000_000),
    "claude-opus-5":             (5.00 / 1_000_000, 25.00 / 1_000_000),
    # Added 2026-10-20 (note 71). Haiku 5.5's rate is for prompts up to 100K tokens
    # ($0.50/$2.50 beyond) — every call here is far below that.
    "claude-haiku-5-5":          (0.10 / 1_000_000,  0.50 / 1_000_000),
    "claude-sonnet-5-5":         (2.00 / 1_000_000, 10.00 / 1_000_000),
    "claude-opus-5-5":           (4.00 / 1_000_000, 20.00 / 1_000_000),
}
# An unrecognised model is far likelier to be a new frontier model than a new
# budget one, so fall back to the top tier. A cost report that overstates is
# recoverable; one that understates is why the old rates went unnoticed.
_FALLBACK_PRICING = (5.00 / 1_000_000, 25.00 / 1_000_000)


def cost_for(model: str, input_tokens: int, output_tokens: int) -> float:
    """USD for one call.

    `input_tokens` as returned by the API is the UNCACHED remainder only — the
    full prompt is input + cache_creation_input_tokens + cache_read_input_tokens.
    Nothing here caches (see the note above), so the two-field form is exact
    today. If a prompt ever does start caching, this signature has to take the
    cache fields too or the number goes quietly low.
    """
    cost_in, cost_out = MODEL_PRICING.get(model, _FALLBACK_PRICING)
    return input_tokens * cost_in + output_tokens * cost_out

DAILY_SYSTEM_PROMPT = """You are an elite sports scientist writing a daily briefing for a \
specific athlete who knows HRV, z-scores, ACWR, and training load. Skip definitions.

GOALS ARE FIXED TARGETS, PERFORMANCE IS A TRAJECTORY. When a GOAL TRAJECTORY block is \
present, judge against the target AND against where he was. Never rebase a target to what he \
actually did, and never restate the same static shortfall you reported last time — if the gap \
is closing, lead with the movement and say the direction plainly even though the goal is \
still unmet; a widening or stalled gap is what earns a complaint.

FORMAT: BULLETS, NOT PROSE. This is a glanceable briefing, not an essay. No preamble, no \
sign-off, no transitions between sections. Every bullet is a fragment, not a sentence — cut \
"you should", "consider", "it looks like", "remember to".

THE FAILURE MODE YOU ARE FIXING: this briefing has been repeating the same generic advice \
daily ("eat enough protein", "get to sleep early"). The athlete has read it a hundred times. \
Standing advice is NOT news, and repeating it trains him to skip the whole card. If a bullet \
would be equally true on any other day, it does not belong in Notice — at most it is one \
clause in Basics.

INPUTS: today's recovery/load metrics, last night's sleep, a PER-DAY SERIES for the last \
~10 days, a multi-window trend block, an illness/overtraining alarm, energy balance, the \
morning check-in, a WHAT'S NEW block, a YESTERDAY'S INPUTS block, YOUR LAST BRIEFINGS \
(novelty ledger), and a "YOUR PERSONAL PATTERNS" block from the correlation engine.

YESTERDAY'S INPUTS IS THE RECORD — NEVER GUESS. It lists what actually happened yesterday \
(caffeine dose, timing and amount still on board at bedtime, alcohol, food, screens, \
training...) against his own usual range. Never write "if X was...", "check the timing" or \
"may explain" about an input it lists — read the value and say it. An input under "not \
recorded" did not happen or was not measured: say nothing about it, and drop any finding \
whose input it is.

A CORRELATION EARNS A LINE ONLY IF IT BEARS ON TODAY: its input was UNUSUAL yesterday, or it \
is New since the last briefing — and in either case state yesterday's actual value beside \
it. A finding whose input was in its usual range explains nothing about today; omit it.

YESTERDAY IS NOT THE BASELINE. If yesterday's value was itself an outlier (|z| >= 1.5 in the \
per-day series), judge today against baseline, not against yesterday: a return toward \
baseline is not a drop and must never be called "sharp". Give the z-score.

FLAGGED DAYS. When yesterday or today carries a flag (travel, sick), the flag is the leading \
explanation for any change in recovery. Do not attribute a change to a lever unless \
YESTERDAY'S INPUTS lists that lever as UNUSUAL.

CAUSAL CLAIMS NEED A FINDING. Even an UNUSUAL input may be named as a cause of today's \
numbers only if WHAT'S NEW or YOUR PERSONAL PATTERNS links that input to that outcome. \
Otherwise report the unusual input as a fact, without "explains", "compounds" or "drove".

DIRECTION. HRV z below 0, RHR z above 0 and recovery below ~60 all point toward rest or easy \
work; the reverse points toward training. Never describe numbers as arguing against a rest \
call when they point toward rest.

USE THE PER-DAY SERIES FOR "IS THIS A BLIP OR A RUN?" — it is the fastest way to tell \
whether today's number is one bad night or the fourth in a row, and a multi-day run is \
news in a way a single day is not. Read it BEFORE reaching for the novelty ledger: the \
series is data, your prior briefings are your own prose, and continuing your own narrative \
is the failure this card keeps falling into. HRV's RUN line gives the count, worked out in \
code: quote it, and never count a run of days yourself from a series of numbers. \
"Declining" or "rising" is the 7-day trend label only, never a count of days.

DELIBERATELY ABSENT HERE: the weekly and monthly reviews carry a REVISITING line (test a \
prior claim) and a WORTH A LOOK line (propose one hypothesis). A DAY is the wrong horizon \
for both — a single day cannot settle a claim, and one hypothesis per day is 365 a year, \
which is noise. Do not improvise them.

ASSESSMENT KNOWLEDGE (use silently; cite numbers, never dump the rubric):
• Recovery weighs HRV 45%, RHR 25%, sleep 30% — recovery only; load (ACWR) is separate. \
Sleep <6h or deep+REM <15% caps recovery low.
• ACWR, his own scale: <0.8 light · 0.8-2.0 normal · 2.0-2.5 high, Rho eases effort · \
>2.5 very high, easy only. Consecutive \
training days compound risk.
• Subjective fatigue/mood can lead HRV by 1-2 days. Localized soreness overrides systemic \
state for that tissue. If the alarm is ELEVATED, lead with it.

CORRELATIONS — LABEL THEIR STATUS, ALWAYS. The athlete cannot currently tell whether a \
correlation is a fresh finding or something he was told last month, and that ambiguity makes \
all of them easy to ignore. Every correlation you mention MUST open with its status:
• "New:" — first time it has reached the reporting threshold. This is the only kind that \
justifies changing behaviour today.
• "Firming:" — previously suggestive, now stronger or better-supported. Say what changed.
• "Known:" — already established and already told to him. Mention ONLY if it directly \
explains today's numbers; otherwise omit entirely.
An unlabelled correlation is a formatting error. If no correlation is new or firming, say \
nothing about correlations at all — do not pad with known ones.

[lever] vs [context] — NEVER TURN CONTEXT INTO AN INSTRUCTION. Findings arrive tagged.
• [lever] — an input he controls (sleep, screen time, protein, alcohol, training). Only \
these may produce a "do X today".
• [context] — ambient and NOT his to change (weather, air quality, barometric pressure, \
whether his partner was home). A [context] finding may explain a number or temper the Call, \
but must never be written as something to do, watch for, track, or monitor. "Pressure swing \
→ wrist temp, watch for thermal drift today" is the exact failure: real correlation, \
impossible instruction. If a [context] finding does not explain today's numbers, omit it.
Never invent monitoring homework ("track X over the next N sessions") for either kind — if \
there is no action, there is no bullet.

ONE NIGHT IS NOT AN EXPLANATION (2026-10-28). A marker within about ±1σ of his baseline \
(|z| < 1) is an ordinary night: call it "within your normal range" and attribute it to \
NOTHING — no behaviour, no finding. A single night cannot show its own cause. Only a marker \
outside ±1σ may be linked to yesterday's behaviour, only through a finding in PERSONAL \
PATTERNS, and only as "consistent with <behaviour> (ρ=…)" — never "reflects", "because of", \
"due to" or "caused by". The failure this prevents: naming yesterday's behaviour as the \
reason for a marker that was within its normal range.

OUTPUT (omit any section with nothing genuinely worth saying — omitting is correct, not lazy):

**Call:** [the day's recommendation in <12 words. If a TODAY'S COACH CALL is provided, state it \
verbatim — do not re-decide it and do not invent a reason for it.]
• [one bullet, AT MOST 25 WORDS: the single most useful number or one of THIS athlete's \
measured patterns for today, reported as it is — even when it points against the call — \
then a few words in the voice of Rho, his coach in the app (a caring trainer who still \
pushes hard), delivering RHO'S TONE TODAY exactly as given — PUSH: encourage him to work \
hard today; STEADY: keep the call, name the reason given, and tell him plainly it's fine to \
take it easier (never "push", "get after it" or "body's not objecting"); REST: back the \
rest, or with a HOME BLOCK listed, say the rest is from the gym and name the block. If YESTERDAY'S CLASS VS RHO'S TARGETS lists a target he BEAT, crediting that one lift \
by name is the best use of the coaching words; never mention an "under". Never pick a \
different tone or speak of yourself by name. Caring, never scolding: no guilt about missed \
days or "slipping".]
RHO'S VOICE APPEARS IN THAT ONE BULLET ONLY. Basics, Notice and Watch stay terse factual \
fragments.

**Basics:** [ONE line, <20 words total, comma-separated status only — e.g. "protein 118g, \
sleep 7h20, ACWR 1.1". Numbers, not advice. Flag a miss with a single word. Never explain \
why protein or sleep matter. A goal is a FIXED target — never restate one as though it moved.]

**Notice:** [1-3 bullets, ONLY things that are true today and were not true yesterday. \
Genuine news: an outlier, a multi-day trajectory turning, a new/firming correlation, a \
number crossing a threshold. Each bullet <20 words. If nothing qualifies, omit this whole \
section — a two-line briefing on a quiet day is the correct output, not a failure.]

**Watch:** [only if the alarm level is ELEVATED or confidence is low — one bullet, <15 words. \
Name the alarm by its level exactly as given (WATCH is not "elevated").]

HARD LIMITS: total <=90 words — this is a CEILING you have been exceeding, not a target to \
fill; count before you answer and cut the weakest bullet if you are over. Quiet day <=25 \
words. At most 2 Notice bullets. Never restate a fact, number, or correlation from YOUR LAST \
BRIEFINGS unless it materially changed — and then cite the CHANGE, not the fact. State a \
missing metric in three words and move on."""

# REWRITTEN 2026-09-09 (note 24 — "they always sort of say the same thing").
#
# The old prompt was a TEMPLATE, not a brief: six mandatory blocks, each with a
# prescribed subject and sentence count, under 320 words. Every week had to produce a
# Recovery, Training AND Nutrition paragraph whether or not anything happened in those
# dimensions — so it manufactured significance to fill them, and the only thing that
# varied week to week was which numbers landed in which slot.
#
# Three changes, in order of expected effect:
#   1. THE MIDDLE SECTIONS ARE CONDITIONAL. Include a dimension only if it moved.
#   2. A QUIET WEEK MAY BE REPORTED AS QUIET. Permission to write two lines and stop is
#      itself a fix; the old word floor guaranteed filler.
#   3. WORTH A LOOK + REVISITING give it somewhere to think. The accuracy rules added on
#      2026-09-07 (to kill an invented "130g protein lever") also forbade it from
#      forming ANY hypothesis, so it could only restate what the correlation engine had
#      already found. WORTH A LOOK restores that, fenced: labelled as observation, never
#      as a lever, and REQUIRED to name what would test it — which is what lets an
#      observation become an n-of-1 experiment instead of just more prose.
#
# REVISITING pairs with the trimmed prior-claims block (summary_context._claims_only).
# Feeding the model 1,350 tokens of its own past essays made it CONTINUE its narrative;
# feeding it just the claims and asking it to test them turns the same input into a
# falsification step.
WEEKLY_SYSTEM_PROMPT = """You are a sharp performance analyst delivering a weekly review \
for a specific athlete who understands training metrics (profile is given below). Be \
analytical and specific; never generic. If a sentence would be true for any athlete, \
delete it.

ACCURACY RULES — these bind everything below.
- CITE ONLY WHAT YOU WERE GIVEN. Every number comes from the blocks below.
- A "lever" means a CONFIRMED finding in the PERSONAL PATTERNS block. Never call anything \
else a lever, and never invent a threshold. An invented threshold reads exactly like a \
finding and is worse than silence.
- You MAY reason beyond the confirmed levers — that is what WORTH A LOOK is for — but \
anything not in PERSONAL PATTERNS must be labelled as an observation, not a finding.
- GOALS ARE FIXED TARGETS. Never rebase a target to what he actually did. Judge against \
the target AND against where he was: a gap that is closing is progress and should be \
named as progress even while the goal is unmet.

WRITE ONLY WHAT THE WEEK EARNS.
A quiet week is a real result. If nothing moved, say so in two lines and stop — do not \
manufacture significance to fill a section. The three middle sections are CONDITIONAL: \
include one only if that dimension actually moved against baseline or against the prior \
weeks. Omitting two of them is a valid and useful week.

USE THE PER-WEEK SERIES. It is the only place you can see week-over-week movement \
directly. Read it for trends, turning points and things that moved together — do not \
infer the past from your own prior summaries.

FORMAT — markdown bold labels, no preamble.

**HEADLINE:** [one sentence — the story of the week, with a number. If the week was \
unremarkable, say that plainly.]

**THE ONE THING:** [the single highest-leverage change for next week, grounded in a \
confirmed lever or the biggest baseline deviation. Specific and actionable.]

**REVISITING:** [Take the strongest claim from the prior-weeks block and test it against \
this week's data. Say SUPPORTED, CONTRADICTED or TOO EARLY, with the number that decides \
it. If a claim you made has stopped holding, saying so is the most valuable line in this \
report. Skip only if there is no prior claim to test.]

[CONDITIONAL — Recovery / Training / Nutrition, as **bold labels**. Include each ONLY if \
it moved. 2-3 sentences, cite numbers, name multi-day patterns and metric chains. For \
nutrition report the TRAJECTORY against each target rather than the static distance from \
it, and when DIET COMPOSITION or FOOD-GROUP blocks are present write "no source logged in \
N days", never "you stopped eating X".]

**WORTH A LOOK:** [ONE observation you think might matter that is NOT already a confirmed \
lever — a pattern in the per-week series, an interaction between two inputs, an ordering \
or lag effect. State it as a hypothesis, give the numbers behind it, and END WITH WHAT \
WOULD TEST IT: a specific comparison, or a week of a specific behaviour. If nothing this \
week is genuinely interesting, write "Nothing this week" and stop — a manufactured \
observation is worse than an honest blank.]

Total under 400 words, excluding any section you correctly omitted."""


# REWRITTEN 2026-09-09, same treatment as the weekly. The monthly had the identical
# shape — five mandatory blocks under a word cap, plus a "Key findings" trio that mostly
# restated HEADLINE and THE ONE THING — so it produced a report of the same five
# dimensions every month whether or not any of them moved.
#
# A month is the RIGHT cadence for both new sections. REVISITING has a real horizon to
# test a claim over (a week is often too short to falsify anything), and one hypothesis
# per month is a sustainable rate — which is why the daily deliberately does NOT get
# WORTH A LOOK: 365 hypotheses a year is noise, and the daily prompt already forbids
# inventing "monitoring homework" for exactly that reason.
MONTHLY_SYSTEM_PROMPT = """You are a top-tier performance analyst delivering a monthly \
review for a specific athlete who understands HRV, ACWR, z-scores, and training load. Be \
analytical, data-driven, and willing to draw a strong, well-supported conclusion. Not \
motivational, never generic. A month of data is enough to reason about CAUSE, not just \
describe averages — that is the difference between this and the weekly.

ACCURACY RULES — these bind everything below.
- CITE ONLY WHAT YOU WERE GIVEN. Every number comes from the blocks below.
- A "lever" means a CONFIRMED finding in the PERSONAL PATTERNS block. Never call anything \
else a lever, and never invent a threshold. An invented threshold reads exactly like a \
finding and is worse than silence.
- You MAY reason beyond the confirmed levers — that is what WORTH A LOOK is for — but \
anything not in PERSONAL PATTERNS must be labelled as an observation, not a finding.
- GOALS ARE FIXED TARGETS. Never rebase a target to what he actually did. Judge against \
the target AND against where he was: a gap that is closing is progress and should be \
named as progress even while the goal is unmet.
- Distinguish signal from noise. Only call a shift real if the numbers support it; a \
month is long enough to say "this did not move" with confidence, and saying so is useful.

WRITE ONLY WHAT THE MONTH EARNS.
An uneventful month is a real result — say so and stop rather than manufacturing \
significance to fill a section. The three middle sections are CONDITIONAL: include one \
only if that dimension actually moved against baseline or against the prior months.

USE THE PER-MONTH SERIES. It is the only place you can see month-over-month movement \
directly — read it for trajectory, inflections and things that moved together. Do not \
reconstruct the past from your own prior summaries.

FORMAT — markdown bold labels, no preamble.

**HEADLINE:** [one sentence — the defining story of the month, with numbers. If the month \
was unremarkable, say that plainly.]

**THE ONE THING:** [the single highest-leverage adjustment for next month, grounded in a \
confirmed lever or a clear multi-week trend.]

**REVISITING:** [Take the strongest claim from the prior-months block and test it against \
this month's data. Say SUPPORTED, CONTRADICTED or TOO EARLY, with the numbers that decide \
it. A month is long enough to actually settle one of these, and a claim you made that has \
stopped holding is the most valuable line in this report. Skip only if there is no prior \
claim to test.]

[CONDITIONAL — Recovery trend / Training / Nutrition, as **bold labels**. Include each \
ONLY if it moved. 3-4 sentences for recovery, 2-3 for the others. Recovery: trajectory vs \
baseline and prior months, with the distribution of high/low days, not just the mean. \
Training: CTL progression, volume by session type, ACWR range, avg RPE, and whether the \
mix matched the goal. Nutrition: averages vs goals WITH the multi-week trajectory toward \
or away from each target, logging consistency, and how intake interacted with recovery \
through the confirmed patterns; when DIET COMPOSITION and FOOD-GROUP blocks are present \
use them — a month is long enough for a real shift in what the diet is made of — ignoring \
deltas marked "flat", and writing "no source logged in N days" rather than "you stopped \
eating X".]

**INTAKE:** [ALWAYS included when the INTAKE block is present — 2-4 sentences. Fluids, \
caffeine and alcohol against the references in that block AND against the prior months \
there: say which way each moved. Use only the block's numbers and its references; never \
supply a threshold of your own, and describe the caffeine-at-bedtime figure without \
grading it. Where the two alcohol references disagree, say so plainly rather than picking \
the kinder one. Link to a confirmed lever in PERSONAL PATTERNS only if one exists for that \
input. A partial month is labelled — treat it as provisional.]

**WORTH A LOOK:** [ONE observation you think might matter that is NOT already a confirmed \
lever — an interaction between two inputs, a lag or ordering effect, a threshold the \
per-month series hints at, something that changed when something else did. State it as a \
hypothesis, give the numbers behind it, and END WITH WHAT WOULD TEST IT: a specific \
comparison, or a month of a specific behaviour. A month is the right horizon to propose \
one of these. If nothing is genuinely interesting, write "Nothing this month" and stop — a \
manufactured observation is worse than an honest blank.]

Total under 520 words, excluding any section you correctly omitted."""


def _fmt(v, spec=".0f"):
    return format(v, spec) if v is not None else "N/A"


def _athlete_profile(settings) -> str:
    """e.g. "male, 40, 5'10"" — DERIVED from user_settings, never hardcoded.

    The weekly and monthly system prompts once described the athlete with a literal age in
    a static prompt, which is wrong the moment it is written and drifts further every
    year. The profile comes from `user_settings` and rides in the USER message, which is
    built per request, so it cannot go stale.
    """
    sex = getattr(settings, "biological_sex", None) if settings else None
    bits = [sex] if sex else []
    by = getattr(settings, "birth_year", None) if settings else None
    if by:
        bits.append(str(local_today().year - by))
    h = getattr(settings, "height_cm", None) if settings else None
    if h:
        total_in = round(h / 2.54)
        bits.append(f"{total_in // 12}'{total_in % 12}\"")
    return ", ".join(bits) or "profile not set"


def _chart_prompt(kind: str) -> str:
    from app.services.summary_charts import prompt_block
    return prompt_block(kind)


def _text_of(response) -> str:
    """The response's TEXT, by block type rather than by position.

    `response.content[0].text` breaks on the Claude 5 family: with adaptive thinking on,
    content[0] is a ThinkingBlock and has no `.text`. It surfaces as
    `'ThinkingBlock' object has no attribute 'text'` and the summary silently becomes
    None — which is exactly what the 2026-09-09 model bump hit. `services/food_tags` had
    already met this and solved it inline; this is that fix, shared, so the next model
    bump does not rediscover it a third time.

    Behaviour is unchanged on models that emit a single text block (Haiku 4.5): joining
    one block is that block.
    """
    return "".join(b.text for b in response.content if b.type == "text").strip()


def _check_truncated(response, feature: str, max_tokens: int) -> None:
    """Log loudly when the model ran out of budget mid-answer.

    Matters much more now that thinking tokens come out of the SAME max_tokens as the
    answer: a budget sized for the visible output alone can be consumed by reasoning and
    leave a summary cut off mid-sentence, with nothing in the logs to say why.
    """
    if getattr(response, "stop_reason", None) == "max_tokens":
        logger.error("%s hit max_tokens (%d) — thinking likely consumed the budget; "
                     "raise it rather than trimming the prompt", feature, max_tokens)


async def _create_complete(client, create_kwargs: dict, feature: str):
    """messages.create, retried ONCE at double the budget if the answer was cut off or empty.

    Note 88 (2026-10-27): the weekly review for 10-19..25 used its whole 6,000-token budget
    on thinking, came back with NO text, and that empty string was stored and shown as a
    blank summary — and, being empty, skipped its push. Thinking spend varies run to run,
    so a fixed budget will be hit again sometime; one retry at 2× absorbs that. The bumped
    budget stays in `create_kwargs`, so the citation checker's re-ask inherits it. The
    first call's usage is logged too — it was paid for.
    """
    msg = await client.messages.create(**create_kwargs)
    if getattr(msg, "stop_reason", None) == "max_tokens" or not _text_of(msg).strip():
        await _log_call(feature + "_retry", msg.usage.input_tokens, msg.usage.output_tokens,
                        create_kwargs["model"])
        logger.warning("%s: cut off or empty at max_tokens=%d — retrying at %d", feature,
                       create_kwargs["max_tokens"], create_kwargs["max_tokens"] * 2)
        create_kwargs["max_tokens"] *= 2
        msg = await client.messages.create(**create_kwargs)
    _check_truncated(msg, feature, create_kwargs["max_tokens"])
    return msg


async def _log_call(feature: str, input_tokens: int, output_tokens: int, model: str = MODEL) -> None:
    """Record one API call to BOTH sinks. Neither may ever break a parse.

    Also the single place a SUCCESS is recorded for ai_health: it already runs after
    every completed call, so putting it here keeps the health state from drifting
    out of step with four separate call sites.

    The local `api_call_log` row is what GET /api/metrics/usage reads. Writing it
    stopped on 2026-06-07, when this function was changed to POST to the household
    app instead — so that endpoint has reported a flat $0 for every month since,
    while the calls kept happening. The local write goes FIRST and, critically,
    ahead of the household config guard below: that `return` is why moving the
    usage sink silently took the local one with it.
    """
    from app.config import settings as app_settings

    ai_health.record_success(feature)

    try:
        from app.database import AsyncSessionLocal
        from app.models import ApiCallLog
        async with AsyncSessionLocal() as session:
            session.add(ApiCallLog(
                feature=feature, model=model,
                input_tokens=input_tokens, output_tokens=output_tokens,
                cost_usd=cost_for(model, input_tokens, output_tokens),
            ))
            await session.commit()
    except Exception as exc:                          # noqa: BLE001
        logger.warning("Failed to log usage locally: %s", exc)

    url = app_settings.household_usage_url
    key = app_settings.household_usage_key
    if not url or not key:
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as hclient:
            await hclient.post(
                url,
                json={"app_name": "wellness", "feature": feature, "model": model,
                      "input_tokens": input_tokens, "output_tokens": output_tokens},
                headers={"Authorization": f"Bearer {key}"},
            )
        logger.info("Usage logged to household: feature=%s tokens=%d+%d",
                    feature, input_tokens, output_tokens)
    except Exception as exc:
        logger.warning("Failed to log usage to household app: %s", exc)


FOOD_PARSE_SYSTEM = """You are a nutrition database API. Output ONLY a raw JSON object — no markdown fences, no prose, no preamble. Start your response with "{".

═══════════════════════════════════════════
RULES
═══════════════════════════════════════════

Estimation:
- Always estimate, even for vague descriptions. Use a typical adult portion if none given.
- Restaurant / fast food: use chain-average values; err 5-10% high on calories.
- Home-cooked: use standard recipe estimates. For cooking fat see Cooking fat below.
- Multiple foods in one entry: return one ITEM PER FOOD (see Item splitting).

Item splitting — the single most important rule:
- Split on FOODS AS EATEN, never on ingredients. The test is what you would count
  separately on a plate or a receipt.
- "A meatball sub and a bag of chips" -> 2 items. "A burger, fries and a shake" -> 3.
- A composite dish stays ONE item. A burger is one item, not bun + patty + cheese.
  A sandwich, a burrito, a smoothie, a stir fry, a casserole, a salad: each is one
  item however many ingredients it names.
- If the user gives ONE combined figure for the whole thing ("frozen pizza, 1200 cal
  total"), that is one item. Never split a user-supplied total across invented parts.
- Sides, drinks, desserts and condiments served alongside are separate items.
- Do not split a single food into portions ("two bagels" is one item, quantity 2).
- Every rule below applies PER ITEM: each item gets its own portion, its own macros,
  and its own arithmetic self-check.
- Prefer fewer, larger items when genuinely unsure. A wrongly split item is worse
  than a wrongly merged one — it invents a food that was never eaten.

Ingredient fidelity — applies to FOODS, not to cooking medium:
- ONLY include foods explicitly stated in the user's input.
- Do NOT infer foods from prior messages, user habits, or common pairings.
- If a likely FOOD was not mentioned (a side dish, a drink, a condiment, a spread, a dressing, a sauce, a topping), do NOT add it to `calories`. Put its calories in `omitted_kcal` instead.
- If the user says "with cheese" do not also add sauce. If they say "a burger" do not add fries. Estimate only what is described.
- THIS IS ABSOLUTE, and it is the rule most often broken. `calories` is a FLOOR: it contains what was named and nothing else. A sandwich, burger, wrap, bagel or salad is priced WITHOUT the spread or dressing unless the user named one — put that in `omitted_kcal` and leave the total alone.
- Your notes must match what you did. Writing "typical spread (mayo ~1 tbsp)" while that mayo is inside `calories` is the specific failure this rule exists to stop: it makes the total quietly higher than what was described, and it makes `omitted_kcal` read 0 when something WAS left out. Describe an unnamed item as assumed-and-excluded, never as included.
- Cooking fat is NOT covered by this rule. It is a property of the cooking method, not a food someone forgot to list — see Cooking fat.

Cooking fat:
- This user does not state whether oil was used. Silence is NOT evidence of a dry pan. Infer the cooking medium from the METHOD, exactly as "a sandwich" implies bread.
- Methods that REQUIRE fat — add a realistic amount even when no oil is mentioned: roasted, fried, pan-fried, sautéed, seared, stir-fried, scrambled or fried eggs, "cooked in a pan", anything browned or crisped.
- Typical ABSORBED amounts (what ends up in the food, not what went in the pan):
    sauté / pan-fry a protein or vegetables ... ~1 tbsp (14 g, 120 cal) per 2 servings
    roasted vegetables .......................... ~1-2 tsp (5-9 g, 40-80 cal) per serving
    scrambled / fried eggs ...................... ~1 tsp (5 g, 40 cal) per 2 eggs
- Methods that need NO added fat — add nothing: boiled, steamed, poached, raw, microwaved, air-fried, plain grilled, baked goods (already in the recipe).
- Already-included cases — do NOT add fat on top, it is double counting:
    deep-fried items (falafel, fries, chips): standard values already carry absorbed oil.
    restaurant, chain and packaged items: published figures already include cooking fat.
    a user-supplied total for the dish.
- When no cooking method is stated or implied at all ("chicken and rice"), add nothing and say so in the notes.
- Always name what you added in the notes, e.g. "sautéed; ~1 tbsp oil assumed".

Arithmetic integrity:
- When the user provides partial macros, compute missing values from calorie balance: protein_g × 4 + carbs_g × 4 + fat_g × 9 = calories.
- Report the arithmetically derived values. Do NOT override computed results with "typical composition" adjustments.
- Self-check: after assembling the final JSON, verify that protein_g × 4 + carbs_g × 4 + fat_g × 9 is within ±8% of the stated calories. If not, adjust the least-constrained macro (usually fat or carbs) to close the gap. carbs_g is TOTAL carbohydrate, fiber included — never report net carbs to make this check pass. Fiber supplies ~0–2 kcal/g, so a high-fiber item (protein bars, prebiotic sodas, bran) can honestly sit up to 4 × fiber_g below 4P + 4C + 9F.

Fractional portions:
- When the user specifies a fraction of a composite item (e.g., "half a bagel with cream cheese"), apply the fraction to the ENTIRE composite — base item AND toppings/condiments — unless the user explicitly indicates otherwise (e.g., "half the bagel but extra cream cheese").
- portion_quantity must reflect the fraction (e.g., 0.5, not 0).

Per-gram sanity bounds (flag or correct if violated):
- Cooked meat/poultry protein: 25–33 g per 100 g. If your estimate exceeds 40 g/100 g, recheck.
- No whole food exceeds ~9 kcal/g (pure fat/oil). Most mixed meals fall in 1.0–2.5 kcal/g.
- Raw fruits and vegetables: sodium < 10 mg per 100 g unless salted/seasoned.

═══════════════════════════════════════════
REFERENCE TABLE — COMMON ITEMS
═══════════════════════════════════════════
Use these as anchors when the user mentions these items. Do not guess when data is available.

Costco Kirkland plain bagel: ~113 g, 300–350 cal, 11–12 g protein, 62–73 g carbs, 2 g fat, 710 mg sodium.

Costco/Kirkland beef patty — label, PER PATTY, 151 g: 380 cal, 26 g protein, 0 g carbs, 30 g fat.
  ONE product. "Costco beef patty", "Costco frozen burger", "Costco hamburger patty" and
  "Costco GRASS-FED beef patty" all mean this same patty — grass-fed is NOT a leaner
  variant here and must not be estimated lower.
  BUILD THE TOTAL, do not recall one. Multiply 380 by the number of patties STATED, then
  add every component the user named, from this list:
    patty (each) ......... 380 cal, 26 P,  0 C, 30 F
    hamburger / sesame bun 145 cal,  5 P, 27 C,  2 F
    cheddar slice ........ 110 cal,  7 P,  1 C,  9 F
    shredded cheese ...... 110 cal,  7 P,  1 C,  9 F
    chipotle mayo / mayo .. 90 cal,  0 P,  1 C, 10 F
    ranch, a squirt ....... 75 cal,  0 P,  1 C,  8 F
    pesto, 1 tbsp ......... 80 cal,  1 P,  1 C,  8 F
  A one-patty burger is roughly HALF a two-patty one. Never reuse a two-patty total for it.
  Carbs come almost entirely from the bun (~27 g); one bun does not reach 70 g.
  CHECK BEFORE YOU ANSWER: add up exactly the components you list in `notes`. That sum IS
  `calories`. Listing a condiment in the notes while leaving it out of the total is the
  specific error to avoid here.

Cream cheese (1 standard spread, ~2 tbsp / 30 g): 100 cal, 1.5 g protein, 1.5 g carbs, 9.5 g fat.
Gouda cheese: per 100 g — 356 cal, 25 g protein, 2.2 g carbs, 27 g fat, 820 mg sodium.

Beer (per 12 oz):
  Light lager (~4% ABV): 100–110 cal, 5–7 g carbs.
  Standard lager (~5% ABV): 140–155 cal, 10–14 g carbs.
  IPA (~6–7% ABV): 180–220 cal, 15–20 g carbs.
  Stout / porter (~5–7% ABV): 170–210 cal, 15–20 g carbs.
  Do NOT model beer as alcohol-only calories. Residual carbohydrates are significant.

Wine (5 oz / 150 ml): red ~125 cal, 4 g carbs; white ~120 cal, 3.5 g carbs.

Starbucks Vanilla Sweet Cream Cold Brew:
  Grande (16 oz): 110 cal, 1 g protein, 14 g carbs, 6 g fat.
  Venti (24 oz): 200 cal, 2 g protein, 23 g carbs, 11 g fat.

Olipop prebiotic soda (12 oz can, varies by flavor): 35–50 cal, 0 g protein, 16 g carbs, 0 g fat, 9 g fiber, 2–5 g sugar.

Qdoba burrito (flour tortilla ~300 cal alone; standard chicken burrito with rice, beans, cheese: 800–1000 cal).

Cooked chicken breast (skinless, no oil): per 100 g — 165 cal, 31 g protein, 0 g carbs, 3.6 g fat.
Cooked chicken thigh (skin-on): per 100 g — 229 cal, 26 g protein, 0 g carbs, 13 g fat.

Medium falafel ball (deep-fried, ~25 g): 55–60 cal, 2.5 g protein, 5 g carbs, 3 g fat.

Watermelon (raw): per 100 g — 30 cal, 0.6 g protein, 7.6 g carbs, 0.2 g fat, 0.4 g fiber, 6.2 g sugar, 1 mg sodium.

═══════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════

Required JSON (no extra fields). ALWAYS an object with an "items" array — a single
food is an array of one. Never return a bare item object.
{
  "items": [
    {
      "name": "<clean food name, 2-5 words AND at most 26 characters. This is the
                label in a narrow picker list, so it is a HANDLE, not a description —
                trim qualifiers before the head noun and never list contents:
                'Chicken melt', not 'Chicken melt with American cheese';
                'Texas Roadhouse chicken' not 'Texas Roadhouse chicken critters,
                fries, Caesar salad'. Contents belong in `description`.>",
      "description": "<this item alone, as a short standalone phrase. NOT the user's
                       whole sentence — each item is stored and tagged on its own, so
                       'a bag of potato chips', never 'meatball sub and a bag of chips'>",
      "portion_description": "e.g. '1 medium bowl, ~300 g cooked oatmeal'",
      "portion_quantity": <number>,
      "portion_unit": "<g | ml | oz | cup | piece | slice | tbsp | serving | …>",
      "calories": <integer>,
      "protein_g": <number, 1 decimal>,
      "carbs_g": <number, 1 decimal>,
      "fat_g": <number, 1 decimal>,
      "fiber_g": <number, 1 decimal>,
      "sugar_g": <number, 1 decimal>,
      "sodium_mg": <integer>,
      "notes": "<key assumptions for THIS item, ≤15 words. Not a full sentence.>",
      "confidence": "<high | medium | low>",
      "omitted_kcal": <integer — calories you believe were eaten but are NOT in the
                       number above, 0 when nothing was left out>,
      "portion_options": <null, or 2-3 alternatives — see Portion options>,
      "caffeine_mg": <null, or an integer — see Caffeine and alcohol>,
      "alcohol_abv": <null, or % ABV as a number — see Caffeine and alcohol>,
      "alcohol_oz": <null, or fluid oz of that drink for the whole portion>,
      "restaurant": <true, false or null — see Restaurant>
    }
  ]
}

Caffeine and alcohol (these are logged as doses alongside the food, so they matter):
- caffeine_mg: ONLY for an item that contains caffeine — coffee and espresso drinks, tea,
  cola, energy drinks, pre-workout. The total for the portion: 12 oz drip coffee ~140,
  one espresso shot ~65, Starbucks grande cold brew ~205 / venti ~310, 8 oz black tea ~45,
  12 oz cola ~35. Decaf ~5. null for everything else, including chocolate.
- alcohol_abv and alcohol_oz: ONLY for an alcoholic drink, both or neither. Use the stated
  or usual ABV (lager ~5, IPA ~6.5, wine ~13, spirits ~40) and the drink's total volume
  (two 12 oz beers = 24; a cocktail = its whole volume with its blended ABV). null otherwise.

Restaurant:
- restaurant: true when the item was bought already prepared from a restaurant, fast-food
  chain, cafe or coffee shop, bar or pub, food truck or deli counter — a named chain
  (Chipotle, Starbucks, Five Guys), "from the pub", "takeout", "leftovers from the
  restaurant" all count. false when it is clearly home-made or a packaged product (a
  grocery-store item, a frozen meal, a protein bar). null when you cannot tell — a plain
  "burrito" or "IPA" says nothing about where it came from. Never guess true.

═══════════════════════════════════════════
CONFIDENCE, OMITTED CALORIES, PORTION OPTIONS
═══════════════════════════════════════════

These three exist so the estimate can be checked. An estimate that hides its own
uncertainty cannot be corrected, and a wrong number that looks certain is worse than
a wrong number that says so.

confidence — about the CALORIE total for this item, nothing else:
- "high"   the amount is pinned: user gave calories or macros, a weight or count, a
           packaged item, or a chain menu item. Most repeat staples are high.
- "medium" a normal dish with a normal implied portion; you know the food, you are
           estimating the size.
- "low"    the size is genuinely unpinned ("a bowl of", "some", "a few handfuls"), or
           a key component is unstated ("stir fry" with no protein named). If a
           plausible reading of the input differs from yours by more than ~30%, it is
           low, not medium.

omitted_kcal — what you deliberately did NOT count:
- Ingredient fidelity means unmentioned FOODS are excluded. That exclusion is correct,
  but it is invisible, so state its size here. A side, a condiment, a drizzle, a
  spread the user did not mention: estimate the calories and put the number here.
- The common case is a composite eaten with something the user did not bother to say.
  A sandwich, burger, wrap or bagel named without a spread nearly always had one; a
  salad named without a dressing usually had one; toast is rarely dry. Exclude it from
  `calories` — see Ingredient fidelity, which is absolute — and report it here:
  ~90 for a tbsp of mayo, ~100 for 2 tbsp of dressing, ~100 for a pat of butter.
- Deciding to exclude and then reporting 0 is the same error as folding it in. If a
  normal serving of the named food carries an unnamed extra, this field is NOT 0.
  The two must agree: what you left out of `calories` is exactly what goes here.
- This is NOT a licence to invent. Report an omission only where a specific,
  nameable thing is missing. A stir fry with no protein named is a LOW-confidence
  estimate, not an omission — you do not know that meat was there.
- Cooking fat you DID add is included in the item, so it is NOT omitted — do not
  double-report it. Neither is a packaged, chain or user-costed item, whose figure
  is already complete: those are 0.
- Never fold omitted_kcal into `calories`. They are separate on purpose: the number
  is only useful if it can be summed on its own.

portion_options — offer a size choice ONLY when the size is the problem:
- Emit when confidence is "low" or "medium" AND the reason is portion size. That is
  the case this field exists for: roughly 40% of real entries name no quantity at all,
  and most of those are medium, not low.
- NEVER when confidence is "high". A pinned number does not get a chooser, and
  offering one there says the estimate is softer than it is.
- Never for: a stated weight or count, a packaged or chain item, user-given calories,
  or an item whose uncertainty is a missing ingredient rather than a size. A size
  chooser cannot answer "was there chicken in it".
- 2-3 entries, each a complete alternative for THIS item:
    {"label": "<1-2 words: Small | Medium | Large, or 'Half' / 'Whole'>",
     "portion_quantity": <number, same unit as the item>,
     "calories": <integer>, "protein_g": <n>, "carbs_g": <n>, "fat_g": <n>,
     "fiber_g": <n>, "sugar_g": <n>, "sodium_mg": <integer>}
- The item's own values MUST equal one of the options — the middle one unless the
  input points elsewhere. The options are a correction, not a replacement.
- Spread them over the range a reasonable person would mean, not a token ±10%.

═══════════════════════════════════════════
FEW-SHOT EXAMPLES
═══════════════════════════════════════════

--- Example 1: Partial macros provided → arithmetic completion ---
User: "A protein bar, 210 cal, 10 g protein, 12 g sugar"
Reasoning (internal, not output):
  Known: 210 cal, 10 g protein, sugar 12 g (part of carbs).
  Protein cal: 10 × 4 = 40.
  Remaining: 210 − 40 = 170 cal from carbs + fat.
  Typical bar carbs ~22 g → 22 × 4 = 88 cal from carbs.
  Fat: (210 − 40 − 88) / 9 = 9.1 g → round to 9 g.
  Check: 40 + 88 + 81 = 209 ✓
Output:
{"items":[{"name":"Protein bar","description":"a protein bar","portion_description":"1 bar","portion_quantity":1,"portion_unit":"piece","calories":210,"protein_g":10.0,"carbs_g":22.0,"fat_g":9.0,"fiber_g":2.0,"sugar_g":12.0,"sodium_mg":170,"notes":"User-provided cal/protein/sugar; fat and carbs from calorie balance"}]}

--- Example 2: Half-portion of a composite item ---
User: "Half a parmesan bagel with cream cheese"
Reasoning (internal):
  Full bagel ~310 cal + cream cheese ~100 cal = 410 cal composite.
  Half composite = 205 cal. Scale all macros by 0.5.
Output:
{"items":[{"name":"Parmesan bagel with cream cheese","description":"half a parmesan bagel with cream cheese","portion_description":"Half a bagel with proportional cream cheese","portion_quantity":0.5,"portion_unit":"piece","calories":205,"protein_g":7.5,"carbs_g":22.0,"fat_g":9.0,"fiber_g":1.0,"sugar_g":2.5,"sodium_mg":400,"notes":"Half of full bagel+cream cheese composite; bagel ~310 cal base"}]}

--- Example 3: Beer with correct carb accounting ---
User: "Two IPA beers"
Reasoning (internal):
  Per reference table: IPA 12 oz → ~200 cal, 17 g carbs, 2 g protein, 0 g fat.
  × 2 = 400 cal, 34 g carbs, 4 g protein.
Output:
{"items":[{"name":"IPA beer","description":"two IPA beers","portion_description":"2 × 12 oz cans","portion_quantity":24,"portion_unit":"oz","calories":400,"protein_g":4.0,"carbs_g":34.0,"fat_g":0.0,"fiber_g":0.0,"sugar_g":0.0,"sodium_mg":30,"notes":"Standard IPA ~6.5% ABV; carbs from residual malt sugars"}]}

--- Example 4: Two foods in one entry → two items ---
User: "Meatball sub sandwich and a small bag of olive oil potato chips"
Reasoning (internal):
  A sub and a bag of chips are counted separately on a plate → 2 items.
  The sub itself stays whole: no bread/meatball/sauce breakdown.
Output:
{"items":[{"name":"Meatball sub sandwich","description":"a meatball sub sandwich","portion_description":"1 sub, ~8 inch","portion_quantity":1,"portion_unit":"piece","calories":750,"protein_g":28.0,"carbs_g":78.0,"fat_g":34.0,"fiber_g":3.0,"sugar_g":9.0,"sodium_mg":1500,"notes":"Marinara and cheese included; no added cooking fat mentioned"},{"name":"Olive oil potato chips","description":"a small bag of olive oil potato chips","portion_description":"1 small bag, ~50 g","portion_quantity":50,"portion_unit":"g","calories":270,"protein_g":3.0,"carbs_g":26.0,"fat_g":17.0,"fiber_g":2.0,"sugar_g":0.5,"sodium_mg":200,"notes":"Small single-serve bag"}]}

--- Example 5: One user-supplied total → ONE item, never split ---
User: "Kirkland frozen cheese pizza, 1200 cal 48g protein 48g fat total"
Reasoning (internal):
  One combined figure for the whole pizza. Splitting into crust/cheese/sauce would
  invent parts the user never gave. One item; carbs from calorie balance:
  (1200 − 48×4 − 48×9) / 4 = (1200 − 192 − 432) / 4 = 144 g.
Output:
{"items":[{"name":"Kirkland frozen cheese pizza","description":"kirkland frozen cheese pizza","portion_description":"1 whole pizza","portion_quantity":1,"portion_unit":"piece","calories":1200,"protein_g":48.0,"carbs_g":144.0,"fat_g":48.0,"fiber_g":6.0,"sugar_g":12.0,"sodium_mg":2400,"notes":"User-provided cal/protein/fat; carbs from calorie balance"}]}"""


BLOOD_PARSE_SYSTEM = """You extract lab results from a blood-panel report (PDF pages, portal \
tables, pasted text — any format). Return ONLY JSON, no prose:
{"date": "YYYY-MM-DD or null if no draw/collection date appears in the report",
 "markers": [{"marker": "...", "label": "...", "value": 123.4, "unit": "...",
              "ref_low": 0.0, "ref_high": 99.9}]}

Rules:
- Extract EVERY quantitative analyte. Skip qualitative results (e.g. "negative").
- "marker" is a canonical snake_case key so the same analyte matches across labs. Use these \
when applicable: apob, ldl_c, hdl_c, vldl_c, total_cholesterol, triglycerides, lp_a, hba1c, \
fasting_glucose, fasting_insulin, hs_crp, ferritin, iron, vitamin_d, vitamin_b12, folate, \
testosterone_total, testosterone_free, estradiol, shbg, tsh, free_t4, free_t3, cortisol, \
alt, ast, ggt, alp, bilirubin_total, albumin, total_protein, creatinine, egfr, bun, uric_acid, \
sodium, potassium, chloride, co2, calcium, magnesium, hemoglobin, hematocrit, wbc, rbc, \
platelets, mcv, mch, mchc, rdw, neutrophils, lymphocytes, monocytes, eosinophils, basophils, \
psa. Otherwise snake_case the lab's name.
- White-cell differential: the plain names (neutrophils, lymphocytes, ...) are the PERCENT \
rows. ABSOLUTE counts ("Neutrophils (Absolute)", "ANC", "Lymphs (Absolute)", unit like \
x10E3/uL) are neutrophils_abs, lymphocytes_abs, monocytes_abs, eosinophils_abs, basophils_abs.
- "label" is the lab's own display name verbatim.
- "ref_low"/"ref_high": the lab's reference range when shown; null when absent; for one-sided \
ranges ("<100") set only the bounded side.
- Do NOT mistake chart axis tick labels or reference-range endpoints for the result value. \
Card-style reports draw the range endpoints as loose numbers right beside the result.
- Keep the lab's units verbatim. Never convert units. Never invent values."""

# Vision arm. Opus, not Haiku: on card-layout PDFs Haiku read the axis tick "0" as
# ref_low for every one-sided range, 5/5 markers on 5/5 runs — deterministic, not
# noise. Opus made zero such errors over the same runs. Neither model ever got a
# *value* wrong in testing, so this buys reference-range fidelity, not value safety.
BLOOD_PDF_MODEL = "claude-opus-5"


async def parse_blood_report(text_input: str) -> Optional[dict]:
    """Use Claude Haiku to extract markers from pasted lab-report text. No DB write."""
    from app.config import settings as app_settings

    if not app_settings.anthropic_api_key:
        ai_health.record_disabled("No ANTHROPIC_API_KEY configured.")
        return None

    import anthropic
    client = anthropic.AsyncAnthropic(api_key=app_settings.anthropic_api_key)

    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=3000,
            extra_body={"temperature": 0},
            system=[{"type": "text", "text": BLOOD_PARSE_SYSTEM}],
            messages=[{"role": "user", "content": text_input[:20000]}],
        )
        _check_truncated(response, "blood_parse", 3000)
        text = _text_of(response)
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else text
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        data = json.loads(text)
        await _log_call("blood_parse", response.usage.input_tokens, response.usage.output_tokens)
        return data
    except Exception as exc:
        ai_health.record_failure("blood_parse", exc)
        return None


LABEL_PARSE_SYSTEM = """You read a Nutrition Facts label from a photo. Return ONLY JSON, \
no prose:
{"name": "product name if legible on the packaging, else null",
 "serving_size": "text as printed, e.g. \\"2 bars (56 g)\\"",
 "servings_per_container": number or null,
 "calories": number, "protein_g": number, "carbs_g": number, "fat_g": number,
 "fiber_g": number or null, "sugar_g": number or null, "sodium_mg": number or null}

Rules:
- Every nutrient value is PER SERVING, exactly as printed. Do NOT multiply by servings \
per container. If the label says 380 calories, return 380.
- "% Daily Value" percentages (18%, 25%, ...) are NOT amounts. Never put a percentage \
into a gram or milligram field.
- "Total Carbohydrate" is carbs_g. "Total Sugars" is sugar_g (not "Includes Xg Added \
Sugars", which is a subset and not requested).
- Use the number as printed, without unit conversion. null for anything not shown.
- If the image is not a nutrition label at all, return {"error": "not a nutrition label"}."""

# Haiku, not Opus. Measured on synthetic labels (flat scan / angled phone photo /
# dim+blurred+noisy), 3 runs each: both models scored 27/27 numeric fields with zero
# per-container errors and zero %DV-into-grams leaks. With no accuracy difference,
# paying 5x for the frequent path is waste. Note this is the OPPOSITE call from
# blood-panel parsing, where Haiku had a deterministic reference-range failure — the
# model should follow the measurement, not a blanket "use the best one".
LABEL_MODEL = MODEL


async def parse_nutrition_label(image_bytes: bytes, media_type: str) -> Optional[dict]:
    """Extract per-serving macros from a photo of a Nutrition Facts panel. No DB write."""
    from app.config import settings as app_settings

    if not app_settings.anthropic_api_key:
        ai_health.record_disabled("No ANTHROPIC_API_KEY configured.")
        return None

    import base64
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=app_settings.anthropic_api_key)

    b64 = base64.standard_b64encode(image_bytes).decode()
    try:
        response = await client.messages.create(
            model=LABEL_MODEL,
            max_tokens=1500,
            extra_body={"temperature": 0},
            system=[{"type": "text", "text": LABEL_PARSE_SYSTEM}],
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64",
                                             "media_type": media_type, "data": b64}},
                {"type": "text", "text": "Read this nutrition label."},
            ]}],
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        data = json.loads(_strip_json_fence(text))
        await _log_call("label_parse", response.usage.input_tokens,
                        response.usage.output_tokens, model=LABEL_MODEL)
        return data
    except Exception as exc:
        ai_health.record_failure("label_parse", exc)
        return None


def _strip_json_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return text


async def parse_blood_report_pdf(pdf_bytes: bytes) -> Optional[dict]:
    """Extract markers by sending the PDF itself to a vision model. No DB write.

    A `document` block gets the model BOTH the text layer and a rendered image of
    every page, which is why this is the arm that survives a scan (no text layer at
    all) and why it can tell a callout value from the axis ticks beside it — the
    spatial relationship that plain text extraction flattens away.
    """
    from app.config import settings as app_settings

    if not app_settings.anthropic_api_key:
        ai_health.record_disabled("No ANTHROPIC_API_KEY configured.")
        return None

    import base64
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=app_settings.anthropic_api_key)

    b64 = base64.standard_b64encode(pdf_bytes).decode()
    try:
        response = await client.messages.create(
            model=BLOOD_PDF_MODEL,
            # Thinking is on by default on Opus 5 and shares this budget with the
            # response, so it needs more headroom than the text arm's 3000. Note
            # `temperature` is NOT passed — Opus 5 rejects sampling params with a 400.
            max_tokens=8000,
            system=[{"type": "text", "text": BLOOD_PARSE_SYSTEM}],
            messages=[{"role": "user", "content": [
                {"type": "document", "source": {"type": "base64",
                                                "media_type": "application/pdf",
                                                "data": b64}},
                {"type": "text",
                 "text": "Extract every quantitative marker from this lab report."},
            ]}],
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        data = json.loads(_strip_json_fence(text))
        # `model=` is NOT optional here: _log_call defaults to MODEL (Haiku), and this
        # arm is Opus — 5x the per-token cost. Omitting it silently understates spend
        # in the household usage tracker.
        await _log_call("blood_parse_pdf", response.usage.input_tokens,
                        response.usage.output_tokens, model=BLOOD_PDF_MODEL)
        return data
    except Exception as exc:
        ai_health.record_failure("blood_parse_pdf", exc)
        return None


# Screenshot panels (2026-10-28): there is no text layer, so the PDF path's second
# MODALITY doesn't exist. The cross-check is a second MODEL on the same images instead:
# weaker evidence than vision-vs-text (a misreading both models share, e.g. a blurred
# digit, is not caught), but it does catch one model's slip, which a single parse can't.
# The UI says which kind of cross-check ran. Sonnet 5, not Haiku: Haiku's deterministic
# axis-tick → ref_low failure (above) would flag every one-sided range as a conflict.
BLOOD_IMAGE_CHECK_MODEL = "claude-sonnet-5"

BLOOD_IMAGES_INSTRUCTION = """These {n} images are consecutive screenshots of ONE lab \
report, in order. Treat them as a single document:
- Screenshots usually OVERLAP: the same row can appear at the bottom of one image and the \
top of the next, and a table can continue across images. Report each analyte EXACTLY ONCE.
- The collection/draw date may appear on only one image; it applies to the whole report.
- Add "image": the number of the image (1-{n}) where that marker's value is shown; if it \
appears on several, the one where its row is complete.
Extract every quantitative marker."""


async def parse_blood_report_images(images: list[tuple[bytes, str]], model: str,
                                    feature: str) -> Optional[dict]:
    """Extract markers from several screenshots of ONE report, in ONE request. No DB write.

    One request, not one per image: a table that spans two screenshots, a date printed
    only on the first, and rows repeated in the overlap are only resolvable with every
    image in view. Each image is preceded by an "Image k of n" caption so the model's
    `image` field can be checked against what the user sees.
    """
    from app.config import settings as app_settings

    if not app_settings.anthropic_api_key:
        ai_health.record_disabled("No ANTHROPIC_API_KEY configured.")
        return None

    import base64
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=app_settings.anthropic_api_key)

    n = len(images)
    content: list[dict] = []
    for k, (data, media_type) in enumerate(images, start=1):
        content.append({"type": "text", "text": f"Image {k} of {n}:"})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": media_type,
            "data": base64.standard_b64encode(data).decode()}})
    content.append({"type": "text", "text": BLOOD_IMAGES_INSTRUCTION.format(n=n)})

    try:
        # Same budget as the PDF arm (thinking shares it); _create_complete retries once
        # at 2x if thinking ate it. No `temperature` — the Claude 5 models reject it.
        response = await _create_complete(client, {
            "model": model, "max_tokens": 8000,
            "system": [{"type": "text", "text": BLOOD_PARSE_SYSTEM}],
            "messages": [{"role": "user", "content": content}],
        }, feature)
        data = json.loads(_strip_json_fence(_text_of(response)))
        await _log_call(feature, response.usage.input_tokens,
                        response.usage.output_tokens, model=model)
        return data
    except Exception as exc:
        ai_health.record_failure(feature, exc)
        return None


_CONFIDENCE = ("high", "medium", "low")
_OPTION_NUMERIC = ("portion_quantity", "calories", "protein_g", "carbs_g", "fat_g",
                   "fiber_g", "sugar_g", "sodium_mg")


ATWATER_TOLERANCE_PCT = 8.0
# Order to raid when the macros do not account for the stated calories. Fat first
# because it is the least often stated by this user and carries 9 cal/g, so it closes
# a gap with the smallest gram change; protein LAST because it is a tracked goal and
# silently inflating it would corrupt the number most often looked at.
_ADJUST_ORDER = (("fat_g", 9.0), ("carbs_g", 4.0), ("protein_g", 4.0))


_STATED_CAL_RE = re.compile(r"(\d{2,4})\s*(?:cal\b|calories\b|kcal\b)", re.I)

# ── Numbers the user typed are final (2026-09-13) ─────────────────────────────
# About half of all entries carry numbers transcribed from a label. The model and the
# Atwater reconciliation could still move them (the fiber bug was one route). Exact
# stated values are now LOCKED onto a single-item parse and never adjusted. Hedged
# numbers ("about 200 cal") and per-unit ones ("180 cal each") are deliberately NOT:
# a hedge is a hint the reference table may beat — the Kirkland bar was "about 200"
# for months against a 170 label — and a per-unit figure needs multiplying.
_HEDGE_RE = re.compile(r"\b(about|around|approx\w*|roughly|maybe|estimat\w*|guess\w*|ish)\b|~", re.I)
_PER_RE = re.compile(r"\b(each|per|apiece)\b|/\s*(serving|bar|piece)", re.I)
_NUM = r"(\d{1,4}(?:\.\d+)?)"
_STATED_PATTERNS = {
    "calories":  re.compile(_NUM + r"\s*(?:k?cals?|calories)\b", re.I),
    "protein_g": re.compile(_NUM + r"\s*g(?:rams?)?\s*(?:of\s+)?protein\b|\bprotein\s*:?\s*" + _NUM + r"\s*g\b", re.I),
    "carbs_g":   re.compile(_NUM + r"\s*g(?:rams?)?\s*(?:of\s+)?(?:total\s+)?carb(?:s|ohydrates?)?\b|\bcarb(?:s|ohydrates?)?\s*:?\s*" + _NUM + r"\s*g\b", re.I),
    "fat_g":     re.compile(_NUM + r"\s*g(?:rams?)?\s*(?:of\s+)?(?:total\s+)?fat\b|\bfat\s*:?\s*" + _NUM + r"\s*g\b", re.I),
    "fiber_g":   re.compile(_NUM + r"\s*g(?:rams?)?\s*(?:of\s+)?fib(?:er|re)\b|\bfib(?:er|re)\s*:?\s*" + _NUM + r"\s*g\b", re.I),
    "sugar_g":   re.compile(_NUM + r"\s*g(?:rams?)?\s*(?:of\s+)?(?:total\s+)?sugars?\b|\bsugars?\s*:?\s*" + _NUM + r"\s*g\b", re.I),
    "sodium_mg": re.compile(_NUM + r"\s*mg\s*(?:of\s+)?sodium\b|\bsodium\s*:?\s*" + _NUM + r"\s*mg\b", re.I),
}
# A count unit with quantity != 1 makes a bare total ambiguous ("2 bars, 340 cal").
_COUNT_UNITS = {"piece", "pieces", "serving", "servings", "bar", "bars", "slice", "slices",
                "item", "items", "egg", "eggs", "patty", "patties", "can", "cans",
                "bottle", "bottles", "scoop", "scoops"}


def _stated_values(text: str) -> dict:
    """Exact numbers the user typed, by field ({} when hedged or per-unit). A field
    stated twice with different values is ambiguous and left out."""
    if not text or _HEDGE_RE.search(text) or _PER_RE.search(text):
        return {}
    out = {}
    for field, rx in _STATED_PATTERNS.items():
        vals = {float(g) for m in rx.finditer(text) for g in m.groups() if g}
        if len(vals) == 1:
            out[field] = vals.pop()
    return out


def _reconcile_atwater(item: dict, user_text: str = "", locked=frozenset()) -> dict:
    """Force 4P + 4C + 9F to agree with `calories`, or say why it could not.

    WHY THIS IS CODE AND NOT A PROMPT RULE. The prompt has told the model to run this
    exact self-check from the start, and 84 of 521 food items (16%) still violate it,
    median 11% off. Instructions about arithmetic are not binding on the model; this
    is. The failure that made it undeniable: for a two-patty Costco burger the model
    listed "2x patty (380), bun (145), shredded cheese (110), ranch (75)" in its own
    notes and returned 815 rather than 1090.

    WHICH SIDE MOVES IS ASYMMETRIC, and the asymmetry comes from the data:

      calories > macros  (80 of the 84 violators). 36 of those carry a user-stated
        calorie figure — "each one is 180 cal" — so the total is transcription and the
        macros are the derived, under-accounted side. Nine identical "Chicken melt"
        rows sat at 360 stated against 298 accounted for. MOVE THE MACROS UP.

      macros > calories  (4 violators, but this is the Costco-burger shape). To land
        here the model would have to overstate protein AND carbs AND fat at once,
        which is far less likely than mis-adding one total. So the TOTAL is the wrong
        number. MOVE CALORIES UP to what the macros account for — unless the user
        stated a calorie figure, in which case their number always wins and the macros
        come down to meet it.

    Getting this backwards is not neutral: an earlier version moved macros in both
    directions and "fixed" the burger by cutting fat 79 g -> 49 g, destroying correct
    macros to defend a total that was the actual error.

    Alcohol is exempt (ethanol is 7 cal/g and absent from the Atwater terms), and the
    function refuses rather than forces when no single macro can absorb the gap without
    going negative — that means something arithmetic cannot fix.
    """
    cal = item.get("calories")
    macros = {k: item.get(k) for k, _ in _ADJUST_ORDER}
    if not cal or cal <= 0 or any(macros[k] is None for k, _ in _ADJUST_ORDER):
        return item

    accounted = sum(float(macros[k]) * per for k, per in _ADJUST_ORDER)
    # FIBER-AWARE BAND (2026-09-12). carbs_g is TOTAL carbohydrate, fiber included, but
    # fiber supplies ~0–2 kcal/g, not 4 — so a high-fiber item legitimately sits up to
    # 4 x fiber_g BELOW the 4-4-9 total. Counting fiber at 4 read a label-true Kirkland
    # protein bar (170 cal, 22 P, 22 C, 11 fiber, 4 F) as 25% "over" and raised two bars
    # to 424 cal; Olipop (45 cal, 16 C, 9 fiber) would have read 64. Anything between
    # fiber-at-0 and fiber-at-4 is consistent. With no fiber value the band collapses
    # to a point and this is exactly the old |gap| <= tolerance test.
    fiber = item.get("fiber_g")
    fiber_kcal = 4.0 * min(max(float(fiber or 0), 0.0), max(float(macros["carbs_g"]), 0.0))
    floor = accounted - fiber_kcal
    tol = float(cal) * (ATWATER_TOLERANCE_PCT / 100.0)
    if floor - tol <= float(cal) <= accounted + tol:
        return item
    # Gap to the NEAREST edge of the band. Above it the macros are short, as always.
    # Below it even fiber-at-0 over-accounts, and a raised total goes only to the floor —
    # the smallest calorie figure those macros can honestly support.
    if float(cal) > accounted:
        gap = float(cal) - accounted
    else:
        gap = float(cal) - floor
        accounted = floor

    # `locked` = fields the user typed exactly (2026-09-13): never moved by this.
    user_stated = bool(_STATED_CAL_RE.search(user_text or "")) or "calories" in locked

    # Macros exceed the total and the user did not name a calorie figure: the total is
    # the mis-computed number, so raise it instead of vandalising three good macros.
    if gap < 0 and not user_stated:
        item["calories"] = int(round(accounted))
        item["_atwater_fix"] = {
            "field": "calories", "from": float(cal), "to": item["calories"],
            "gap_kcal": round(gap), "gap_pct": round(gap / float(cal) * 100, 1),
        }
        return item

    for field, per_gram in _ADJUST_ORDER:
        if field in locked:
            continue                     # a number the user typed is never moved
        delta_g = gap / per_gram
        new_val = round(float(macros[field]) + delta_g, 1)
        if new_val < 0:
            continue                     # this macro cannot absorb it; try the next
        item[field] = new_val
        item["_atwater_fix"] = {
            "field": field, "from": float(macros[field]), "to": new_val,
            "gap_kcal": round(gap), "gap_pct": round(gap / float(cal) * 100, 1),
        }
        return item

    # Nothing could absorb the gap. Leave every number untouched and downgrade the
    # claim instead — a wrong estimate that says it is unsure beats one that does not.
    item["_atwater_unreconciled"] = round(gap / float(cal) * 100, 1)
    if not locked:                       # the user's own numbers are not "low confidence"
        item["confidence"] = "low"
    return item


# Ethanol is 7 cal/g and is not an Atwater term, so anything alcoholic reads far
# "low" by construction. meal_type alone is not enough — a beer logged as a snack
# would still be silently given carbohydrate it does not have.
_ALCOHOL_RE = re.compile(
    r"\b(beer|lager|ale|ipa|stout|porter|pilsner|cider|wine|prosecco|champagne|"
    r"vodka|gin|rum|whisk(?:e)?y|bourbon|tequila|scotch|liqueur|cocktail|martini|"
    r"margarita|manhattan|negroni|seltzer|shot|spirits|sake|mead)\b", re.I)


def _is_alcoholic(item: dict, meal_type: str | None) -> bool:
    if (meal_type or "").strip().lower() == "drinks":
        return True
    text = f"{item.get('name') or ''} {item.get('description') or ''}"
    return bool(_ALCOHOL_RE.search(text))


def _normalise_item(item: dict, meal_type: str | None = None,
                    user_text: str = "", locked=frozenset()) -> dict:
    """Coerce one parsed item's self-reporting fields into shapes the client can trust.

    The three fields added 2026-09-04 (confidence / omitted_kcal / portion_options)
    are the ones a model is most likely to improvise on, and each has a specific way
    of going wrong that must not reach the UI:

      • confidence      an unexpected word ("very high", "moderate") would fall through
                        every equality check downstream and silently render nothing.
                        Unknown values become "medium" — the honest default is the
                        middle, not the flattering end.
      • omitted_kcal    a string, a negative, or a number bigger than the meal itself.
                        Clamped to [0, calories]: an omission larger than the food is
                        not a measurement, it is a malfunction.
      • portion_options dropped entirely unless every entry is usable. A half-formed
                        chooser is worse than none — tapping an option with a null
                        calorie value would blank the item's numbers.
    """
    conf = str(item.get("confidence") or "").strip().lower()
    item["confidence"] = conf if conf in _CONFIDENCE else "medium"
    # Doses logged with the food (note 100). Anything unusable is dropped, never guessed.
    def _num(k, lo, hi):
        try:
            v = float(item.get(k))
        except (TypeError, ValueError):
            return None
        return v if lo <= v <= hi else None
    item["caffeine_mg"] = _num("caffeine_mg", 1, 1000)
    abv, oz = _num("alcohol_abv", 0.5, 70), _num("alcohol_oz", 0.5, 100)
    item["alcohol_abv"], item["alcohol_oz"] = (abv, oz) if abv and oz else (None, None)
    # Note 84: only a real boolean counts; "yes" / 1 / anything else is unknown.
    r = item.get("restaurant")
    item["restaurant"] = r if isinstance(r, bool) else None
    if "calories" in locked:
        item["confidence"] = "high"      # the user's own figure, not an estimate

    try:
        omitted = float(item.get("omitted_kcal") or 0)
    except (TypeError, ValueError):
        omitted = 0.0
    cal = float(item.get("calories") or 0)
    item["omitted_kcal"] = int(max(0.0, min(omitted, cal))) if cal > 0 else 0

    opts = item.get("portion_options")
    clean = []
    if isinstance(opts, list):
        for o in opts:
            if not isinstance(o, dict) or not str(o.get("label") or "").strip():
                continue
            if o.get("calories") is None or o.get("portion_quantity") is None:
                continue
            row = {"label": str(o["label"]).strip()[:16]}
            for k in _OPTION_NUMERIC:
                try:
                    row[k] = None if o.get(k) is None else float(o[k])
                except (TypeError, ValueError):
                    row[k] = None
            clean.append(row)
    # One option is not a choice, and the prompt asks for 2-3.
    # The high-confidence bar is enforced HERE rather than trusted to the prompt:
    # it is the one invariant that matters (a pinned number must not be offered a
    # size chooser, which would tell the user it is softer than it is), and the
    # first measured run showed the model treating the low/medium boundary as
    # advisory. low/medium is left to its judgement — that is where the ~40% of
    # entries naming no quantity actually land.
    if item["confidence"] == "high":
        clean = []
    item["portion_options"] = clean if len(clean) >= 2 else None

    # Last, so it sees the coerced values and its own adjustment is what the caller
    # receives. Drinks are exempt — ethanol is 7 cal/g and not an Atwater term.
    if not _is_alcoholic(item, meal_type):
        item = _reconcile_atwater(item, user_text, locked)
    return item


async def _notify_ai_down() -> None:
    """One push per transition into credit/auth failure — the two that never
    self-resolve. Latched in ai_health.should_notify(), so a run of failed parses
    sends one notification rather than one each: alert fatigue on the topic that
    also carries the alerts that matter is a worse outcome than a missed ping.

    Fail-soft, like everything else on this path — a notification problem must not
    turn a degraded parse into a 500.
    """
    from app.services import ai_health
    if not ai_health.should_notify():
        return
    try:
        from app.config import settings as app_settings
        if not app_settings.ntfy_url:
            return
        from app.services.scheduler import _ntfy_post
        snap = ai_health.snapshot()
        await _ntfy_post(f"Wellness AI down: {snap['label']}",
                         (snap.get("action") or snap.get("detail") or ""),
                         "high", tags="warning")
    except Exception as exc:                       # noqa: BLE001
        logger.warning("AI-down notification failed: %s", exc)


async def parse_food_description(description: str, meal_type: str,
                                 reference: str = "") -> Optional[dict]:
    """Use Claude Haiku to parse a food description into nutritional estimates. No DB write."""
    from app.config import settings as app_settings

    if not app_settings.anthropic_api_key:
        ai_health.record_disabled("No ANTHROPIC_API_KEY configured.")
        return None

    import anthropic
    client = anthropic.AsyncAnthropic(api_key=app_settings.anthropic_api_key)

    # `reference` = the user's My Foods (routers/food_log._my_foods_reference).
    user_msg = f"{reference}Meal: {meal_type}\nFood: {description}"

    try:
        response = await client.messages.create(
            model=FOOD_PARSE_MODEL,
            # 600 -> 2000 (items array, 2026-09-02) -> 8000 (2026-09-12).
            #
            # 2000 was sized for an items array ALONE. `portion_options` landed on
            # 2026-09-04 and multiplies the cost per item — each option carries its own
            # label, quantity, calories and full macro set — so a five-item restaurant
            # order now blows the ceiling. Measured: "steak, mashed potatoes, green
            # beans, dinner roll, and a slice of cheesecake" returned stop_reason
            # max_tokens at exactly 2000 output tokens, 5294 chars of JSON cut off
            # mid-object.
            #
            # A truncated response is not a short answer, it is unparseable JSON — and
            # it surfaced to the user as "Expecting ',' delimiter: line 200 column 27",
            # which points at nothing they can act on. See the stop_reason check below.
            #
            # Output tokens are billed as used, so a higher ceiling costs nothing on the
            # common one-item parse; it only stops the rare big meal from failing.
            max_tokens=8000,
            # No `temperature`: Haiku 5.5 rejects non-default sampling params with a 400.
            # Haiku 4.5 ran at temperature 0 here for determinism; that was traded away
            # deliberately (see FOOD_PARSE_MODEL). food_parse_cache still makes an exact
            # repeat of a description return the same saved items.
            system=[{"type": "text", "text": FOOD_PARSE_SYSTEM}],
            messages=[{"role": "user", "content": user_msg}],
        )
        # CHECK BEFORE PARSING. A truncated response fails json.loads with a message
        # about a delimiter at some character offset, which reads as "the model emitted
        # bad JSON" when the real cause is "the answer did not fit". Saying so plainly
        # is the difference between a one-line fix and an afternoon.
        if response.stop_reason == "max_tokens":
            raise ValueError(
                f"Food parse hit max_tokens ({response.usage.output_tokens}) — the reply "
                f"was cut off mid-JSON, not malformed. Raise max_tokens, or split the "
                f"meal into fewer items."
            )
        text = _text_of(response)

        # Strip markdown code fences if model wraps in them
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else text
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        data = json.loads(text)
        await _log_call("food_parse", response.usage.input_tokens, response.usage.output_tokens,
                        model=FOOD_PARSE_MODEL)

        # Normalise to {"items": [...]}. The prompt asks for that shape, but a model
        # that ignores it returns the old bare item object, and one truncated at
        # max_tokens can return items as something other than a list — neither should
        # reach the client as a 200 with a shape it will silently mis-render.
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            items = data["items"]
        elif isinstance(data, dict) and "calories" in data:
            logger.warning("Food parse returned a bare item; wrapping")
            items = [data]
        else:
            logger.error("Food parse returned an unusable shape: %s", str(data)[:200])
            return None

        items = [i for i in items if isinstance(i, dict) and i.get("calories") is not None]
        if not items:
            return None
        # Typed numbers are final on a single-item parse (see _stated_values).
        locked: set = set()
        if len(items) == 1:
            stated = _stated_values(description)
            unit = str(items[0].get("portion_unit") or "").strip().lower()
            q = items[0].get("portion_quantity")
            ambiguous = unit in _COUNT_UNITS and q not in (None, 1, 1.0)
            if stated and not ambiguous:
                items[0].update(stated)
                locked = set(stated)
                items[0]["_stated"] = sorted(locked)
        return {"items": [_normalise_item(i, meal_type, description, locked) for i in items]}
    except Exception as exc:
        ai_health.record_failure("food_parse", exc)
        await _notify_ai_down()
        return None


# A within-noise marker, labelled AT THE NUMBER (2026-10-28). The system-prompt rule alone
# ("ONE NIGHT IS NOT AN EXPLANATION") did not hold: both dry runs still blamed a within-noise
# HRV dip on yesterday's drinks, and explained a modest RHR rise with alcohol + a calorie deficit.
# A fact next to the number is harder to talk past than a rule (note 55's lesson).
def _normal_range_note(z: Optional[float]) -> str:
    if z is None or abs(z) >= 1.0:
        return ""
    return "  [WITHIN NORMAL RANGE (|z| < 1) — an ordinary night; attribute it to nothing]"


# Rho's tone for the briefing's one coaching phrase (2026-10-29, voice v5). DECIDED HERE,
# not by the model: in v4 the prompt said "steady on a poor check-in" and on a clearly
# low-energy morning the model still wrote "get the class in". Morning readiness, HRV and
# ACWR are deliberately NOT inputs — none predicted lifting performance in the logged sessions, and ACWR
# was wrong-signed for next-day energy. What he reports and the real alarm are.
TRAINING_CALLS = ("MODERATE", "HARD", "CLASS", "+ 4TH")
# Steady when today's check-in energy is among his lowest TONE_LOW_PCT of the last 30 days.
# A percentile, not "mean − 0.75": the test day was the 2nd-lowest check-in of the month, yet
# sat just inside that fixed drop — a fixed drop missed the day it was meant for.
TONE_LOW_PCT = 0.15
TONE_MIN_CHECKINS = 5


def hrv_run(rows: list) -> tuple[int, int]:
    """(nights in a row below baseline, nights in a row at z <= -1), ending with the latest
    of `rows` = [(date, hrv_z), ...] oldest first. A night without HRV or a missing date ends
    the run. Counted here because the briefing counted it wrong from the series: an up-and-down
    week was called "declining 4th straight day" in both runs of a 2026-11-04 dry run."""
    def count(limit: float) -> int:
        n, prev = 0, None
        for d, z in reversed(rows):
            if z is None or z >= limit or (prev is not None and (prev - d).days != 1):
                break
            n, prev = n + 1, d
        return n
    return count(0.0), count(-1.0 + 1e-9)


_RUN_CARD = ["zero", "one", "two", "three", "four", "five", "six", "seven"]
_RUN_ORD = ["zeroth", "first", "second", "third", "fourth", "fifth", "sixth", "seventh"]
_RUN_RE = re.compile(
    r"\b(?P<n>\d+(?:st|nd|rd|th)?|" + "|".join(_RUN_CARD[1:] + _RUN_ORD[1:]) + r")"
    r"(?P<rest>\s+(?:straight|consecutive)\s+(?:\w+\s+)?(?:night|day)s?\b"
    r"|\s+(?:\w+\s+)?(?:night|day)s?\s+in\s+a\s+row\b)", re.I)


def enforce_run_counts(text: str, run_below: int, run_low: int) -> str:
    """On a line about HRV, make any "Nth straight night" / "N nights in a row" match hrv_run.
    A prompt rule alone left 1 of 3 dry runs (2026-11-04) quoting the computed 3 and then
    calling it the "fourth straight soft night" in the same line. With no run (0), a
    contradicting count is only logged: there is no right number to put in its place."""
    if not text:
        return text

    def fix_line(line: str) -> str:
        if "HRV" not in line:
            return line

        def fix(m: re.Match) -> str:
            raw = m.group("n")
            low = raw.lower()
            digits = re.match(r"\d+", raw)
            n = int(digits.group(0)) if digits else (
                _RUN_CARD.index(low) if low in _RUN_CARD else _RUN_ORD.index(low))
            if n in (run_below, run_low):
                return m.group(0)
            if run_below == 0:
                logger.warning("HRV run count %r but no run below baseline; left as written", m.group(0))
                return m.group(0)
            want = run_below
            if digits:
                suffix = raw[digits.end():]
                new = f"{want}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(want, 'th') }" if suffix else str(want)
            else:
                new = (_RUN_ORD if low in _RUN_ORD else _RUN_CARD)[want]
                new = new.capitalize() if raw[0].isupper() else new
            logger.warning("HRV run count corrected: %r -> %s", m.group(0), new)
            return new + m.group("rest")

        return _RUN_RE.sub(fix, line)

    return "\n".join(fix_line(l) for l in text.split("\n"))


_ALARM_ELEVATED_RE = re.compile(
    r"\balarm\b(?P<mid>[^.\n—:;,()]{0,25}?)\belevated\b|\belevated\s+(?:illness\S*\s+|overtraining\s+)?alarm\b",
    re.I)


def enforce_alarm_level(text: str, level: Optional[str]) -> str:
    """Call the alarm "elevated" only when it is. One dry run of three (2026-11-04) wrote
    "alarm elevated" on a 1-marker WATCH day; a prompt rule is not enough on its own (see
    enforce_gap_direction). Marker wording such as "resting HR elevated" is left alone."""
    if not text or not level or level == "elevated":
        return text

    def fix(m: re.Match) -> str:
        logger.warning("Alarm level corrected: %r -> %s", m.group(0), level.upper())
        return re.sub(r"elevated", level.upper(), m.group(0), count=1, flags=re.I)

    return _ALARM_ELEVATED_RE.sub(fix, text)


def rho_tone(coach_label: Optional[str], energy: Optional[float],
             recent_energy: Optional[list], soreness: Optional[str],
             sick: Optional[bool], alarm_level: Optional[str],
             home_block: bool = False) -> Optional[tuple[str, str]]:
    """(tone, reason) for the briefing's coaching phrase, or None when there is no call.

    push   — a training call and nothing he reported says otherwise.
    steady — a training call, but a flat check-in, soreness, a sick flag or an ELEVATED
             recovery alarm: keep the call, easing off is his choice.
    rest   — a non-training call (REST, TARGET MET, …): back the rest. With a home block
             due, the rest is from the gym only: the card offers the block the same day.
    """
    if not coach_label:
        return None
    if coach_label not in TRAINING_CALLS:
        if home_block:
            return ("rest", "no gym today, but a home block is on his card")
        return ("rest", "today's call is not a training day — back the rest")
    if alarm_level == "elevated":
        return ("steady", "recovery alarm is elevated")
    if sick:
        return ("steady", "he flagged himself sick")
    if energy is not None and recent_energy and len(recent_energy) >= TONE_MIN_CHECKINS:
        ranked = sorted(recent_energy)
        low = ranked[int(TONE_LOW_PCT * (len(ranked) - 1))]
        if energy <= low:
            return ("steady", f"check-in energy {energy:g} — among his lowest this month "
                              f"(usual {statistics.median(ranked):.1f})")
    if soreness and soreness.strip():
        return ("steady", f"soreness noted: {soreness.strip()[:60]}")
    return ("push", "a training call and nothing he reported says otherwise")


async def generate_daily_summary(coach_label: Optional[str] = None,
                                 for_date: Optional[date] = None,
                                 coach_detail: Optional[str] = None,
                                 home_blocks: Optional[list] = None) -> Optional[str]:
    """Generate, store, and return today's daily AI summary. Returns None on failure.

    for_date: regenerate the summary for a PAST day instead of today (backfill /
    repair). Writes to that day's row, not the newest one — without it this
    function always overwrites the latest row. Skips the webhook freshness gate.
    The date-less context helpers (trend, alarm, energy, patterns, events) still
    read as of today, so a backfill is approximate, not a faithful replay.

    coach_label / coach_detail: the coach card's call for today and its one-line detail.
    The summary STATES the call but never invents a reason for it, and reports numbers
    that point the other way as they are (note 29: "never contradict it" plus "justify
    it with a number" made the model rationalise coin flips). On a future randomized
    day that can unblind the arm — a trade chosen deliberately for honesty.

    home_blocks: names of the home blocks the card offers today (coach_plan.due_home_blocks)."""
    import statistics as _stats
    from sqlalchemy import select, desc
    from app.config import settings
    from app.database import AsyncSessionLocal
    from app.models import DailyMetric, FoodLog, SleepSession, SubjectiveLog, UserSettings, Workout
    from app.workout_categories import LOW_INTENSITY_TYPES

    if not settings.anthropic_api_key:
        logger.info("Daily summary: ANTHROPIC_API_KEY not set, skipping")
        return None

    async with AsyncSessionLocal() as db:
        user_settings = (await db.execute(select(UserSettings).limit(1))).scalars().first()
        if user_settings is not None and not user_settings.ai_enabled:
            logger.info("Daily summary: AI disabled in settings, skipping")
            return None

        ref_day = for_date or local_today()

        # Skip if no webhook has fired today — nothing new to summarise
        if (for_date is None and user_settings is not None
                and user_settings.last_webhook_at is not None):
            if user_settings.last_webhook_at.date() < local_today():
                logger.info("Daily summary: no webhook today (last: %s), skipping",
                            user_settings.last_webhook_at.date())
                return None

        latest_q = select(DailyMetric)
        if for_date is not None:
            latest_q = latest_q.where(DailyMetric.date == for_date)
        latest = (await db.execute(
            latest_q.order_by(desc(DailyMetric.date)).limit(1)
        )).scalars().first()
        if latest is None:
            return None

        # Fetch 21 days for load windows; reverse to get oldest-first order
        trend_rows = list(reversed((await db.execute(
            select(DailyMetric).where(DailyMetric.date <= latest.date)
            .order_by(desc(DailyMetric.date)).limit(21)
        )).scalars().all()))

        # Day-over-day: the row before latest
        prior_dm = (await db.execute(
            select(DailyMetric)
            .where(DailyMetric.date < latest.date)
            .order_by(desc(DailyMetric.date))
            .limit(1)
        )).scalars().first()

        # Last night ONLY (sessions are wake-date keyed). This used to take the latest
        # session <= ref_day, so a briefing that ran before the night had synced
        # silently reported the night before as "last night" (note 57: it quoted the
        # previous night's hours as last night's).
        last_sleep = (await db.execute(
            select(SleepSession).where(SleepSession.date == ref_day)
        )).scalars().first()

        training_goal = (user_settings.training_goal if user_settings else "building").capitalize()

        # Yesterday's nutrition — summary runs in the morning, yesterday is the complete day
        yesterday_dt = ref_day - timedelta(days=1)
        yesterday_start = food_day_start(yesterday_dt)      # FOOD day: 04:00 → 04:00
        yesterday_end   = food_day_end(yesterday_dt)
        food_rows = (await db.execute(
            select(FoodLog)
            .where(FoodLog.timestamp >= yesterday_start)
            .where(FoodLog.timestamp < yesterday_end)
        )).scalars().all()

        # NOT the raw settings column — it is NULL here and the goal is derived from
        # body weight. Reading it raw made every briefing say "no goal set", and the
        # model then borrowed the ">130g" example out of the system prompt. See
        # services/nutrition_goals.resolve_protein_goal.
        protein_goal_g = await resolve_protein_goal(db, user_settings)

        # Today's morning check-in (logged before reviewing metrics)
        today_dt = ref_day
        subj = (await db.execute(
            select(SubjectiveLog).where(SubjectiveLog.date == today_dt)
        )).scalars().first()
        # His own usual energy (30 days before today) — Rho's tone compares against it.
        recent_energy = [e for (e,) in (await db.execute(
            select(SubjectiveLog.energy)
            .where(SubjectiveLog.date >= today_dt - timedelta(days=30))
            .where(SubjectiveLog.date < today_dt)
            .where(SubjectiveLog.energy.isnot(None))
        )).all()]


        # Yesterday's workouts with RPE logged (local calendar day)
        from app.timeutils import local_day_bounds_utc, to_local_date
        yesterday_workouts_rpe = (await db.execute(
            select(Workout)
            .where(Workout.start_time >= local_day_bounds_utc(yesterday_dt)[0])
            .where(Workout.start_time < local_day_bounds_utc(yesterday_dt)[1])
            .where(Workout.rpe.isnot(None))
            .order_by(desc(Workout.start_time))
        )).scalars().all()

        # Recent workout types for consecutive training day classification
        # (walks don't count as training days)
        recent_workout_rows = (await db.execute(
            select(Workout.start_time, Workout.activity_type)
            .where(Workout.start_time >= local_day_bounds_utc(
                ref_day - timedelta(days=22)
            )[0])
        )).all()

    # ── Dates with real (non-walk) training sessions (local days) ──────────────
    real_training_dates: set[date] = {
        to_local_date(r.start_time)
        for r in recent_workout_rows
        if (r.activity_type or "") not in LOW_INTENSITY_TYPES
    }

    # ── 7-day HRV analysis ─────────────────────────────────────────────────────
    hrv_7d_rows = [r for r in trend_rows[-7:] if r.hrv_value is not None]
    hrv_7d_vals = [r.hrv_value for r in hrv_7d_rows]
    hrv_mean_7d = _stats.mean(hrv_7d_vals) if hrv_7d_vals else None
    hrv_cv_7d: Optional[float] = None
    if len(hrv_7d_vals) >= 3 and hrv_mean_7d and hrv_mean_7d > 0:
        hrv_cv_7d = _stats.stdev(hrv_7d_vals) / hrv_mean_7d * 100

    # 7-day HRV trend — least-squares slope over EVERY point in the window, in z units.
    #
    # This was `first3 = hrv_7d_vals[-3]` vs `last3 = hrv_7d_vals[-1]` with a ±1.5 ms
    # threshold: an endpoint comparison that ignored every value in between and used a
    # cutoff about 8% the size of the night-to-night noise. It is the same
    # defect that made the pre-sleep nudge fire on 47% of nights — fixed there
    # 2026-09-07, fixed here for the same reason.
    #
    # z rather than raw ms so the threshold means something: 0.75 z of modelled change
    # across the window matches HRV_SUPPRESSED_MEAN_Z in the scheduler, so the briefing
    # and the push agree about what "declining" means.
    hrv_z_vals = [r.hrv_z_score for r in trend_rows[-7:] if r.hrv_z_score is not None]
    hrv_trend_delta_z: Optional[float] = None
    if len(hrv_z_vals) >= 3:
        n = len(hrv_z_vals)
        xs = list(range(n))
        x_mean = sum(xs) / n
        y_mean = sum(hrv_z_vals) / n
        ssx = sum((x - x_mean) ** 2 for x in xs)
        slope = (sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, hrv_z_vals)) / ssx
                 if ssx else 0.0)
        hrv_trend_delta_z = slope * (n - 1)      # modelled change across the window
        if hrv_trend_delta_z >= 0.75:
            hrv_trend = "rising"
        elif hrv_trend_delta_z <= -0.75:
            hrv_trend = "declining"
        else:
            hrv_trend = "stable"
    else:
        hrv_trend = "insufficient data"

    hrv_series_str = " → ".join(f"{v:.0f}" for v in hrv_7d_vals) if hrv_7d_vals else "N/A"
    run_below, run_low = hrv_run([(r.date, r.hrv_z_score) for r in trend_rows[-7:]])

    # ── Load windows (daily_load = TRIMP, heart-rate based — NOT calories) ─────
    # The label said "active-calorie proxy" and printed "kcal"; daily_load is TRIMP
    # (09-14: daily_load 75.3 = that class's TRIMP 75). With today's in-progress 0 as the
    # newest value, the model wrote "yesterday 0 kcal load" (2026-09-15). The windows
    # still end at today; yesterday's value is now stated explicitly.
    all_loads = [(r.date, r.daily_load) for r in trend_rows if r.daily_load is not None]
    load_3d  = sum(v for _, v in all_loads[-3:])  if all_loads else 0.0
    load_7d  = sum(v for _, v in all_loads[-7:])  if all_loads else 0.0
    load_21d = sum(v for _, v in all_loads[-21:]) if all_loads else 0.0
    load_yday = next((v for d_, v in all_loads if d_ == ref_day - timedelta(days=1)), None)

    # Consecutive training days: count back from yesterday using actual workout types.
    # Walks don't count; skip today (load not yet populated when the morning summary fires).
    consec_training = 0
    for r in reversed(trend_rows):
        if r.date >= ref_day:
            continue
        if r.date in real_training_dates:
            consec_training += 1
        else:
            break

    # ── Sleep gate values ──────────────────────────────────────────────────────
    if last_sleep and last_sleep.total_min and last_sleep.total_min > 0:
        sleep_h = last_sleep.total_min / 60
        deep_rem_min = (last_sleep.deep_min or 0) + (last_sleep.rem_min or 0)
        deep_rem_pct = round(deep_rem_min / last_sleep.total_min * 100)
        gate_fail = sleep_h < 6 or deep_rem_pct < 15
        sleep_section = (
            f"  [{last_sleep.date}] Total: {sleep_h:.1f}h | Deep+REM: {deep_rem_pct}% "
            f"| Gate: {'⚠ FAIL (< 6h or < 15% deep+REM)' if gate_fail else 'pass'}"
        )
    else:
        sleep_section = ("  Last night's sleep has NOT SYNCED yet — do not state any sleep "
                         "duration, stage mix or sleep gate result for last night")
        gate_fail = False

    # ── Nutrition ──────────────────────────────────────────────────────────────
    if food_rows:
        nut_cal   = sum(r.calories   or 0 for r in food_rows)
        nut_prot  = sum(r.protein_g  or 0 for r in food_rows)
        nut_carbs = sum(r.carbs_g    or 0 for r in food_rows)
        nut_fat   = sum(r.fat_g      or 0 for r in food_rows)
        goal_str  = f"goal {protein_goal_g}g" if protein_goal_g else "no goal set"
        nutrition_line = (
            f"  {round(nut_cal)} kcal · {nut_prot:.0f}g protein ({goal_str}) · "
            f"{nut_carbs:.0f}g carbs · {nut_fat:.0f}g fat ({len(food_rows)} entries)"
        )
    else:
        nutrition_line = "  No meals logged"

    # ── Day-over-day delta block ───────────────────────────────────────────────
    if prior_dm is not None:
        def _delta(cur, prev, spec=".1f"):
            if cur is None or prev is None:
                return "N/A"
            return f"{cur - prev:+{spec}}"
        # When YESTERDAY was itself an outlier, a raw Δ reads a return to baseline as a
        # crash: an HRV z of about +1.8 followed by a normal night read as a "sharp drop",
        # and the briefing then labelled the Δ (about −2.6) as a z-score (note 55). Say so at the source.
        def _outlier_note(prev_z):
            if prev_z is None or abs(prev_z) < 1.5:
                return ""
            return (f"  [yesterday was an OUTLIER (z {prev_z:+.2f}); this Δ is mostly a return "
                    f"toward baseline — judge today by its own z, not by the Δ]")
        _prior_outlier = next((z for z in (prior_dm.hrv_z_score, prior_dm.rhr_z_score)
                               if z is not None and abs(z) >= 1.5), None)
        dod_lines = [
            f"  Readiness: {_fmt(latest.readiness_score)}/100  Δ{_delta(latest.readiness_score, prior_dm.readiness_score, '.0f')}"
            + ("  [yesterday's recovery inputs were outliers — this Δ is mostly a return toward "
               "baseline, not a drop]" if _prior_outlier is not None else ""),
            f"  HRV z-score: {_fmt(latest.hrv_z_score, '+.2f')}  Δ{_delta(latest.hrv_z_score, prior_dm.hrv_z_score, '.2f')}"
            + _outlier_note(prior_dm.hrv_z_score) + _normal_range_note(latest.hrv_z_score),
            f"  RHR z-score: {_fmt(latest.rhr_z_score, '+.2f')}  Δ{_delta(latest.rhr_z_score, prior_dm.rhr_z_score, '.2f')}"
            + _outlier_note(prior_dm.rhr_z_score) + _normal_range_note(latest.rhr_z_score),
            f"  Sleep debt: {_fmt(latest.sleep_debt_hours, '.1f')}h  Δ{_delta(latest.sleep_debt_hours, prior_dm.sleep_debt_hours, '.2f')}h",
            f"  ACWR: {_fmt(latest.acwr, '.2f')}  Δ{_delta(latest.acwr, prior_dm.acwr, '.2f')}",
        ]
        dod_block = "DAY-OVER-DAY:\n" + "\n".join(dod_lines) + "\n\n"
    else:
        dod_block = ""

    # ── Subjective check-in block ──────────────────────────────────────────────
    if subj is not None:
        rpe_parts = [
            f"{w.activity_type or 'Workout'} RPE {w.rpe:.0f}/10"
            for w in yesterday_workouts_rpe
        ]
        rpe_str = ", ".join(rpe_parts) if rpe_parts else "not logged"
        checkin_block = (
            f"SUBJECTIVE CHECK-IN (logged before reviewing metrics):\n"
            f"  Sleep quality: {subj.sleep_quality}/5\n"
            f"  Energy/fatigue: {subj.energy}/5\n"
            f"  Mood: {subj.mood}/5\n"
            f"  Soreness: {subj.soreness or 'none'}\n"
            f"  Yesterday's workout RPE: {rpe_str}\n\n"
        )
    else:
        rpe_parts = [
            f"{w.activity_type or 'Workout'} RPE {w.rpe:.0f}/10"
            for w in yesterday_workouts_rpe
        ]
        rpe_str = ", ".join(rpe_parts) if rpe_parts else "not logged"
        checkin_block = (
            f"SUBJECTIVE CHECK-IN: not logged today\n"
            f"  Yesterday's workout RPE: {rpe_str}\n\n"
        )

    # Personalized + trend enrichment (each block is fail-safe → "" on error)
    from app.services.summary_context import (
        daily_trend_block, day_flags_block, energy_block, events_block,
        enforce_gap_direction, goal_trajectory_block, period_series_block, personal_patterns_block,
        recent_summaries_block, recovery_alarm_block, yesterday_block,
    )
    async with AsyncSessionLocal() as ctx_db:
        # These five read "as of now" (newest row / local_today()). On a backfill they
        # would hand the model a SECOND, later "today" beside the real one, so they are
        # dropped rather than replayed wrongly — less context beats contradictory context.
        if for_date is None:
            events_ctx  = await events_block(ctx_db)
            trend_ctx   = await daily_trend_block(ctx_db)
            alarm_ctx   = await recovery_alarm_block(ctx_db)
            energy_ctx  = await energy_block(ctx_db)
            patterns_ctx = await personal_patterns_block(ctx_db)
        else:
            events_ctx = trend_ctx = alarm_ctx = energy_ctx = patterns_ctx = ""
        recent_ctx  = await recent_summaries_block(ctx_db, before=latest.date)
        # As-of `latest.date`, so it is correct on a backfill too (note 55).
        yday_ctx = await yesterday_block(ctx_db, latest.date)
        from app.services.summary_context import class_result_block
        class_ctx = await class_result_block(ctx_db, latest.date)
        # 14 days, not just yesterday: the daily briefing reports multi-day
        # trajectories, so it needs to know an illness sits inside the run it is
        # about to explain — not merely whether last night was flagged.
        flags_ctx = await day_flags_block(ctx_db, latest.date - timedelta(days=13), latest.date)
        from app.services.summary_context import today_flag_note
        # Stated on the TODAY line itself (note 55 audit) — see today_flag_note.
        today_flag = await today_flag_note(ctx_db, latest.date)
        today_flag_line = f"  ⚑ {today_flag}\n" if today_flag else ""
        # 4 weeks, not 6: a DAILY briefing is about the last few days, and a longer
        # trajectory here is context the weekly is better placed to carry.
        # Ends YESTERDAY: today's half-logged intake counted as a finished day and
        # produced "protein goal incomplete (not yet logged)" at 05:55 (note 55).
        goaltraj_ctx = await goal_trajectory_block(ctx_db, latest.date - timedelta(days=1), weeks=4)
        series_ctx = await period_series_block(ctx_db, "day", latest.date)

    coach_block = ""
    if coach_label:
        coach_block = (
            f"TODAY'S COACH CALL (decided by the app): {coach_label}"
            + (f" — {coach_detail}" if coach_detail else "") + "\n"
            "State this call on the Call line. Do NOT invent a reason for it: it comes "
            "from the week's plan and a few fixed rules, not a judgment you can "
            "reconstruct. If today's numbers point the other way, say so plainly — the "
            "athlete decides. Spend your words on what's NEW.\n"
        )
        if home_blocks:
            coach_block += (
                f"HOME BLOCK ON RHO'S CARD TODAY: {' and '.join(home_blocks)}. Name it once "
                "in the bullet under the Call; the card has the moves, so don't list them.\n"
            )
        coach_block += "\n"
    alarm_level = None
    if for_date is None:
        try:
            from app.services.recovery_alarm import assess_recovery_alarm
            async with AsyncSessionLocal() as alarm_db:
                a = await assess_recovery_alarm(alarm_db)
            alarm_level = a["level"] if a else None
        except Exception as exc:
            logger.warning("Daily summary: alarm level for Rho's tone failed: %s", exc)
    tone = rho_tone(coach_label, subj.energy if subj else None, recent_energy,
                    subj.soreness if subj else None, subj.sick if subj else None,
                    alarm_level, bool(home_blocks))
    if tone:
        coach_block += f"RHO'S TONE TODAY: {tone[0].upper()} — {tone[1]}\n\n"

    user_msg = (
        f"{coach_block}"
        f"{events_ctx}"
        f"TODAY ({latest.date}) — Training goal: {training_goal}\n"
        f"{today_flag_line}"
        f"  Readiness: {_fmt(latest.readiness_score)}/100 ({latest.readiness_label or 'N/A'})\n"
        f"  HRV: {_fmt(latest.hrv_value)} ms  z {_fmt(latest.hrv_z_score, '+.2f')}  "
        f"baseline {_fmt(latest.hrv_baseline)} ms{_normal_range_note(latest.hrv_z_score)}\n"
        f"  RHR: {_fmt(latest.rhr_value)} bpm  z {_fmt(latest.rhr_z_score, '+.2f')}"
        f"{_normal_range_note(latest.rhr_z_score)}\n"
        f"  Sleep debt: {_fmt(latest.sleep_debt_hours, '.1f')} h/night (14-day avg)\n"
        f"  ACWR: {_fmt(latest.acwr, '.2f')}  ATL {_fmt(latest.atl)}  CTL {_fmt(latest.ctl)}\n\n"
        f"{checkin_block}"
        f"LAST NIGHT'S SLEEP:\n{sleep_section}\n\n"
        f"7-DAY HRV (ms, oldest→newest):\n"
        f"  {hrv_series_str}\n"
        f"  RUN (counted in code): last night is {run_below} night(s) in a row below baseline "
        f"(z < 0), {run_low} in a row at z ≤ −1\n"
        f"  Mean: {_fmt(hrv_mean_7d, '.0f')} ms  |  "
        f"CV: {_fmt(hrv_cv_7d, '.1f')}%  |  7-day trend: {hrv_trend}"
        + (f" ({hrv_trend_delta_z:+.1f} z across the window)"
           if hrv_trend_delta_z is not None else "") + "\n\n"
        f"TRAINING LOAD (TRIMP — heart-rate training score, NOT calories):\n"
        f"  Yesterday ({ref_day - timedelta(days=1)}): "
        f"{'no data' if load_yday is None else f'{load_yday:.0f} TRIMP'}"
        f"  |  3-day: {load_3d:.0f}  |  7-day: {load_7d:.0f}  |  21-day: {load_21d:.0f} TRIMP"
        f" (windows include today, which is still in progress)\n"
        f"  Consecutive training days: {consec_training}\n\n"
        f"{dod_block}"
        f"{series_ctx}"
        f"{trend_ctx}"
        f"{flags_ctx}"
        f"{alarm_ctx}"
        f"{energy_ctx}"
        f"NUTRITION (yesterday):\n{nutrition_line}\n\n"
        f"{yday_ctx}"
        f"{class_ctx}"
        f"{goaltraj_ctx}"
        f"{patterns_ctx}"
        f"{recent_ctx}"
    )

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        create_kwargs = dict(
            model=MODEL_DAILY,
            max_tokens=450,
            # Explicitly off: Sonnet 5 runs ADAPTIVE thinking when `thinking` is omitted,
            # billed as output. A <=90-word briefing doesn't need it. With thinking off the
            # model can leak internal XML tags into visible text — hence the one-line guard
            # appended to the system prompt (Anthropic's documented mitigation).
            thinking={"type": "disabled"},
            system=[{"type": "text", "text": DAILY_SYSTEM_PROMPT
                     + "\n\nDo not include internal or system XML tags in your response."}],
            messages=[{"role": "user", "content": user_msg}],
        )
        msg = await _create_complete(client, create_kwargs, "daily_summary")
        text = _text_of(msg)
        await _log_call("daily_summary", msg.usage.input_tokens, msg.usage.output_tokens, MODEL_DAILY)
        # Every ρ / n quoted must come from THIS briefing's data — not from the earlier
        # briefings in recent_ctx, or a misquote would launder itself day to day.
        from app.services.citations import enforce as _enforce_citations
        text = await _enforce_citations(
            client, create_kwargs, text, user_msg.replace(recent_ctx, "") if recent_ctx else user_msg,
            kind="daily", period_key=latest.date.isoformat(), feature="daily_summary",
            model=MODEL_DAILY)
        text = enforce_gap_direction(text, goaltraj_ctx)
        text = enforce_run_counts(text, run_below, run_low)
        text = enforce_alarm_level(text, alarm_level)
    except Exception as exc:
        logger.error("Daily summary: Claude API failed: %s", exc)
        return None
    if not (text or "").strip():
        # Never store (or push) a blank briefing — note 88. ERROR → error-ledger push.
        logger.error("Daily summary: no text even after a retry — nothing stored")
        return None

    # Store on the DailyMetric row
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(DailyMetric).where(DailyMetric.id == latest.id)
        )).scalars().first()
        if row:
            row.ai_summary = text
            # What this briefing READ, with each input's own date (note 57). Sleep rows are
            # overwritten when a later sync revises a night, so the minutes used here are
            # the only record of what the briefing saw.
            import json as _json
            row.ai_summary_inputs = _json.dumps({
                "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "model": MODEL_DAILY,
                "ref_day": ref_day.isoformat(),
                "sleep": ({"date": last_sleep.date.isoformat(), "total_min": last_sleep.total_min,
                           "deep_min": last_sleep.deep_min, "rem_min": last_sleep.rem_min,
                           "bedtime": last_sleep.bedtime.isoformat() if last_sleep.bedtime else None,
                           "wake_time": last_sleep.wake_time.isoformat() if last_sleep.wake_time else None}
                          if last_sleep else None),
                "metrics_row": {"date": latest.date.isoformat(),
                                "computed_at": latest.computed_at.isoformat() if latest.computed_at else None,
                                "hrv": latest.hrv_value, "hrv_z": latest.hrv_z_score,
                                "rhr": latest.rhr_value, "readiness": latest.readiness_score},
                "nutrition": {"date": yesterday_dt.isoformat(), "entries": len(food_rows),
                              "kcal": round(sum(r.calories or 0 for r in food_rows)),
                              "protein_g": round(sum(r.protein_g or 0 for r in food_rows))},
            })
            await db.commit()

    logger.info("Daily summary stored for %s", latest.date)
    return text


def _session_split(workouts) -> str:
    """Lifting/conditioning split for a list of Workout rows, with the SAME classifier
    as the PER-WEEK series (workout_categories.classify_gym_session). The workout list
    used to give raw Apple labels only while the series gave the split, and the two
    disagreed on 9/22 — the model then quoted each in a different paragraph."""
    from app.workout_categories import classify_gym_session
    kinds = [classify_gym_session(w.activity_type, w.high_intensity_min) for w in workouts]
    n_l, n_c = kinds.count("lifting"), kinds.count("conditioning")
    other = len(kinds) - n_l - n_c
    return f"{n_l} lifting / {n_c} conditioning" + (f" / {other} other" if other else "")


async def generate_weekly_summary() -> Optional[str]:
    """Generate, store, and return this week's summary (covers Mon–Sun). Returns None on failure."""
    from sqlalchemy import select, desc
    from app.config import settings
    from app.database import AsyncSessionLocal
    from app.models import DailyMetric, FoodLog, SleepSession, Workout, UserSettings, WeeklySummary
    from app.models import HealthMetric, SubjectiveLog

    if not settings.anthropic_api_key:
        return None

    async with AsyncSessionLocal() as db:
        user_settings = (await db.execute(select(UserSettings).limit(1))).scalars().first()
        if user_settings is not None and not user_settings.ai_enabled:
            return None

        # Week bounds: the most recent completed Mon–Sun (or trailing 7 days if mid-week)
        today = local_today()
        # On Sunday (weekday 6), week_end = today; otherwise last Sunday
        days_since_sunday = (today.weekday() + 1) % 7
        week_end = today - timedelta(days=days_since_sunday)
        week_start = week_end - timedelta(days=6)

        daily_rows = (await db.execute(
            select(DailyMetric)
            .where(DailyMetric.date >= week_start)
            .where(DailyMetric.date <= week_end)
            .order_by(DailyMetric.date)
        )).scalars().all()

        sleep_rows = (await db.execute(
            select(SleepSession)
            .where(SleepSession.date >= week_start)
            .where(SleepSession.date <= week_end)
        )).scalars().all()

        from app.timeutils import local_day_bounds_utc
        workout_rows = (await db.execute(
            select(Workout)
            .where(Workout.start_time >= local_day_bounds_utc(week_start)[0])
            .where(Workout.start_time < local_day_bounds_utc(week_end)[1])
            .order_by(Workout.start_time)
        )).scalars().all()

        weight_rows = (await db.execute(
            select(HealthMetric)
            .where(HealthMetric.metric_type == "weight")
            .where(HealthMetric.timestamp >= local_day_bounds_utc(week_start)[0])
            .where(HealthMetric.timestamp < local_day_bounds_utc(week_end)[1])
            .order_by(HealthMetric.timestamp)
        )).scalars().all()

        wellbeing_rows = (await db.execute(
            select(SubjectiveLog)
            .where(SubjectiveLog.date >= week_start)
            .where(SubjectiveLog.date <= week_end)
        )).scalars().all()

        training_goal = (user_settings.training_goal if user_settings else "building").capitalize()
        # Derived, not the raw column — see the daily builder above.
        protein_goal_g = await resolve_protein_goal(db, user_settings)
        # Per-day, not resolve_calorie_goal()["value"] — that is None in balance/pace
        # mode, so the Nutrition paragraph has been asked to report "calorie adherence
        # vs goals" with no goal to compare against.
        from app.services.energy import effective_calorie_goal
        cal_goal_by_day: dict = {}
        if user_settings:
            _d = week_start
            while _d <= week_end:
                _g = (await effective_calorie_goal(db, user_settings, _d))["goal"]
                if _g:
                    cal_goal_by_day[_d] = _g
                _d += timedelta(days=1)
        # A dynamic goal has no single number; the median in force represents it.
        _cg = sorted(cal_goal_by_day.values())
        cal_goal_resolved = _cg[len(_cg) // 2] if _cg else None

        # Prior week for week-over-week comparison
        prior_week_start = week_start - timedelta(days=7)
        prior_week_end = week_start - timedelta(days=1)
        prior_daily_rows = (await db.execute(
            select(DailyMetric)
            .where(DailyMetric.date >= prior_week_start)
            .where(DailyMetric.date <= prior_week_end)
            .order_by(DailyMetric.date)
        )).scalars().all()

        prior_sleep_rows = (await db.execute(
            select(SleepSession)
            .where(SleepSession.date >= prior_week_start)
            .where(SleepSession.date <= prior_week_end)
        )).scalars().all()

        # Weekly nutrition
        food_rows_week = (await db.execute(
            select(FoodLog)
            .where(FoodLog.timestamp >= food_day_start(week_start))
            .where(FoodLog.timestamp < food_day_end(week_end))
            .where((FoodLog.skipped == False) | (FoodLog.skipped.is_(None)))  # noqa: E712
        )).scalars().all()
        # Incomplete-log days are left out of the averages, as goal_trajectory_block
        # does (note 104): averaged in, the same prompt carried two means for one week
        # (in one trip week this line read ~30% below the trajectory's mean).
        from app.services.day_flags import nutrition_excluded_dates
        nutrition_excluded = await nutrition_excluded_dates(db, week_start, week_end)

    if not daily_rows:
        logger.info("Weekly summary: no data for week %s–%s, skipping", week_start, week_end)
        return None

    from app.workout_categories import LOW_INTENSITY_TYPES

    # ── Format context ─────────────────────────────────────────────────────────

    # Per-day raw metrics table — gives Claude actual numbers to spot trends,
    # rather than re-processing text summaries of summaries.
    sleep_by_date = {s.date: s for s in sleep_rows}
    daily_lines = []
    for r in daily_rows:
        sleep = sleep_by_date.get(r.date)
        if sleep and sleep.total_min and sleep.total_min > 0:
            deep_rem_min = (sleep.deep_min or 0) + (sleep.rem_min or 0)
            deep_rem_pct = round(deep_rem_min / sleep.total_min * 100)
            sleep_str = f"{sleep.total_min/60:.1f}h ({deep_rem_pct}% deep+REM)"
        else:
            sleep_str = "no data"
        daily_lines.append(
            f"  {r.date} ({r.date.strftime('%a')}): "
            f"Readiness {_fmt(r.readiness_score)}/100 ({r.readiness_label or 'N/A'}) · "
            f"HRV z{_fmt(r.hrv_z_score, '+.2f')} · "
            f"RHR z{_fmt(r.rhr_z_score, '+.2f')} · "
            f"Sleep {sleep_str} · "
            f"ACWR {_fmt(r.acwr, '.2f')}"
        )

    avg_readiness = sum(r.readiness_score for r in daily_rows if r.readiness_score) / max(1, sum(1 for r in daily_rows if r.readiness_score))

    # Sleep
    sleep_totals = [s.total_min for s in sleep_rows if s.total_min]
    sleep_deep_rem = [(s.deep_min or 0) + (s.rem_min or 0) for s in sleep_rows if s.total_min]
    avg_sleep_h = sum(sleep_totals) / len(sleep_totals) / 60 if sleep_totals else None
    avg_deep_rem_pct = (
        round(sum(sleep_deep_rem) / sum(sleep_totals) * 100) if sleep_totals else None
    )

    # Workouts — split training sessions vs walks/active recovery
    from collections import defaultdict as _dd
    training_sessions = [w for w in workout_rows if (w.activity_type or "") not in LOW_INTENSITY_TYPES]
    walk_sessions     = [w for w in workout_rows if (w.activity_type or "") in LOW_INTENSITY_TYPES]

    from app.workout_categories import classify_gym_session
    workout_lines = []
    workout_type_counts: dict = _dd(int)
    workout_type_mins:   dict = _dd(float)
    rpe_vals = []
    for w in training_sessions:
        wtype = w.activity_type or "Workout"
        workout_type_counts[wtype] += 1
        workout_type_mins[wtype] += (w.duration_sec or 0) / 60
        kind = classify_gym_session(w.activity_type, w.high_intensity_min)
        parts = [f"{wtype} [{kind}]" if kind else wtype]
        if w.duration_sec:
            parts.append(f"{w.duration_sec//60}min")
        if w.active_calories:
            parts.append(f"{round(w.active_calories)} kcal")
        if w.avg_hr:
            parts.append(f"avg HR {round(w.avg_hr)} bpm")
        if w.rpe is not None:
            parts.append(f"RPE {w.rpe:.0f}/10")
            rpe_vals.append(w.rpe)
        workout_lines.append("  " + " · ".join(parts))

    type_summary = ", ".join(
        f"{v}× {k} ({round(workout_type_mins[k])}min)"
        for k, v in sorted(workout_type_counts.items(), key=lambda x: -x[1])
    ) if training_sessions else "None"
    rpe_summary = f"Avg RPE {sum(rpe_vals)/len(rpe_vals):.1f}/10 ({len(rpe_vals)} rated)" if rpe_vals else "No RPE logged"

    # Walk summary
    if walk_sessions:
        total_walk_min = sum((w.duration_sec or 0) / 60 for w in walk_sessions)
        walk_summary = f"{len(walk_sessions)} walks ({round(total_walk_min)} min total)"
    else:
        walk_summary = "None"

    # Latest ATL/CTL
    latest_dm = daily_rows[-1]

    # Weight
    weight_str = "No weight data this week"
    if weight_rows:
        first_w = weight_rows[0].value
        last_w = weight_rows[-1].value
        delta = last_w - first_w
        weight_str = f"{last_w:.1f} lb (change: {delta:+.1f} lb over {len(weight_rows)} readings)"

    # Wellbeing — overall + sub-scores
    wb_str = "No wellbeing entries"
    # Average only scored entries (2026-10-25): an entry can carry sleep/energy/mood with
    # no wellbeing score (10-12), and `or 0` read that as a 0/10 day.
    wb_scores = [r.wellbeing_score for r in wellbeing_rows if r.wellbeing_score is not None]
    if wellbeing_rows:
        avg_wb  = sum(wb_scores) / len(wb_scores) if wb_scores else None
        sq_vals = [r.sleep_quality for r in wellbeing_rows if r.sleep_quality is not None]
        en_vals = [r.energy        for r in wellbeing_rows if r.energy        is not None]
        mo_vals = [r.mood          for r in wellbeing_rows if r.mood          is not None]
        sub = []
        if sq_vals: sub.append(f"sleep qual {sum(sq_vals)/len(sq_vals):.1f}/5")
        if en_vals: sub.append(f"energy {sum(en_vals)/len(en_vals):.1f}/5")
        if mo_vals: sub.append(f"mood {sum(mo_vals)/len(mo_vals):.1f}/5")
        sub_str = f" ({', '.join(sub)})" if sub else ""
        wb_str = (f"Avg {avg_wb:.1f}/10" if avg_wb is not None else "No wellbeing score") + \
                 f"{sub_str} over {len(wellbeing_rows)} entries"

    # Weekly nutrition — calories, protein, macros, goal adherence
    # The summary fires at noon Sunday, so Sunday's food log is incomplete.
    # We compute averages over fully-completed days (Mon–Sat) only, and note
    # any partial Sunday entries separately so the AI isn't misled by a
    # half-empty day dragging the per-day average down.
    if food_rows_week:
        by_day: dict = _dd(list)
        for r in food_rows_week:
            by_day[food_day(r.timestamp)].append(r)

        # Split into complete days (before today) and today's partial entries
        today_dt = local_today()
        complete_days = {d: rows for d, rows in by_day.items()
                         if d < today_dt and d not in nutrition_excluded}
        left_out = sorted(d for d in by_day if d < today_dt and d in nutrition_excluded)
        partial_today = by_day.get(today_dt, [])
        days_before_today = sum(1 for i in range(7) if week_start + timedelta(days=i) < today_dt)
        left_out_note = (
            f"; {len(left_out)} logged day{'s' if len(left_out) != 1 else ''} flagged "
            f"incomplete left out ({', '.join(f'{d:%b %-d}' for d in left_out)})"
            if left_out else ""
        )

        if complete_days:
            days_logged = len(complete_days)
            day_cals  = [sum(r.calories   or 0 for r in rows) for rows in complete_days.values()]
            day_prots = [sum(r.protein_g  or 0 for r in rows) for rows in complete_days.values()]
            day_carbs = [sum(r.carbs_g    or 0 for r in rows) for rows in complete_days.values()]
            day_fats  = [sum(r.fat_g      or 0 for r in rows) for rows in complete_days.values()]
            avg_cal   = sum(day_cals)  / days_logged
            avg_prot  = sum(day_prots) / days_logged
            avg_carbs = sum(day_carbs) / days_logged
            avg_fat   = sum(day_fats)  / days_logged

            cal_goal  = cal_goal_resolved
            cal_goal_str = ""
            if cal_goal_by_day:
                # Each day against ITS OWN goal — in pace mode the target moves with
                # that day's activity, so one scalar would mis-score active days.
                days_on_target = sum(
                    1 for _d, _rows in complete_days.items()
                    if cal_goal_by_day.get(_d)
                    and abs(sum(r.calories or 0 for r in _rows) - cal_goal_by_day[_d])
                        / cal_goal_by_day[_d] <= 0.15
                )
                cal_goal_str = (f" (goal ~{cal_goal} kcal/day, varies with activity; "
                                f"{days_on_target}/{days_logged} days within 15%)")
            prot_goal_str = ""
            if protein_goal_g:
                days_on_prot = sum(1 for p in day_prots if p >= protein_goal_g * 0.9)
                prot_goal_str = f" (goal {protein_goal_g}g; hit {days_on_prot}/{days_logged} days)"

            # Note any partial entries from today (summary fires mid-day)
            partial_note = ""
            if partial_today:
                pt_cal  = sum(r.calories  or 0 for r in partial_today)
                pt_prot = sum(r.protein_g or 0 for r in partial_today)
                partial_note = (
                    f"\n  Today ({today_dt}, partial — {len(partial_today)} entries so far): "
                    f"{round(pt_cal)} kcal · {pt_prot:.0f}g protein [excluded from averages]"
                )

            today_note = "; today excluded, still being logged" if today_dt <= week_end else ""
            weekly_nutrition_str = (
                f"Averages over {days_logged} of {days_before_today} days"
                f"{left_out_note}{today_note}{partial_note}\n"
                f"  Avg calories: {round(avg_cal)} kcal/day{cal_goal_str}\n"
                f"  Avg protein:  {avg_prot:.0f}g/day{prot_goal_str}\n"
                f"  Avg carbs:    {avg_carbs:.0f}g/day · Avg fat: {avg_fat:.0f}g/day"
            )
        else:
            # Only today's entries exist (edge case: first day of tracking)
            weekly_nutrition_str = ("No complete-log days this week before today"
                                    f"{left_out_note}")
    else:
        weekly_nutrition_str = "No meals logged this week"

    # Prior week comparison block
    if prior_daily_rows:
        prior_readiness_vals = [r.readiness_score for r in prior_daily_rows if r.readiness_score is not None]
        prior_avg_readiness = sum(prior_readiness_vals) / len(prior_readiness_vals) if prior_readiness_vals else None
        prior_sleep_totals = [s.total_min for s in prior_sleep_rows if s.total_min]
        prior_avg_sleep_h = sum(prior_sleep_totals) / len(prior_sleep_totals) / 60 if prior_sleep_totals else None
        prior_atl = prior_daily_rows[-1].atl
        prior_week_block = (
            f"PRIOR WEEK ({prior_week_start} to {prior_week_end}):\n"
            f"  Avg readiness: {f'{prior_avg_readiness:.0f}/100' if prior_avg_readiness is not None else 'N/A'}\n"
            f"  Avg sleep: {f'{prior_avg_sleep_h:.1f}h' if prior_avg_sleep_h else 'N/A'} "
            f"({len(prior_sleep_totals)} nights recorded)\n"
            f"  ATL at week end: {_fmt(prior_atl)}\n\n"
        )
    else:
        prior_week_block = "PRIOR WEEK: No data (first week or gap in data)\n\n"

    # Personalized + baseline + anti-repetition enrichment (fail-safe → "")
    from app.services.summary_context import (
        baseline_block, blood_block, day_flags_block, diet_block,
        enforce_gap_direction, goal_trajectory_block, period_series_block, personal_patterns_block,
        prior_weekly_block,
    )
    async with AsyncSessionLocal() as ctx_db:
        patterns_ctx  = await personal_patterns_block(ctx_db)
        baseline_ctx  = await baseline_block(ctx_db, week_start, week_end, 84, "12-WEEK")
        prior_sum_ctx = await prior_weekly_block(ctx_db, week_start, n=2)
        blood_ctx     = await blood_block(ctx_db)
        diet_ctx      = await diet_block(ctx_db, week_start, week_end, 84, "12-WEEK")
        flags_ctx     = await day_flags_block(ctx_db, week_start, week_end)
        goaltraj_ctx  = await goal_trajectory_block(ctx_db, week_end)
        series_ctx    = await period_series_block(ctx_db, "week", week_end)

    user_msg = (
        f"Athlete: {_athlete_profile(user_settings)}\n"
        f"Week: {week_start} to {week_end}\n"
        f"Training goal: {training_goal}\n\n"
        f"{prior_sum_ctx}"
        f"{patterns_ctx}"
        f"{blood_ctx}"
        f"{prior_week_block}"
        f"{baseline_ctx}"
        f"{series_ctx}"
        f"{diet_ctx}"
        f"{flags_ctx}"
        f"{goaltraj_ctx}"
        f"DAILY READINESS (avg {avg_readiness:.0f}/100):\n" + "\n".join(daily_lines) + "\n\n"
        f"SLEEP ({len(sleep_totals)} nights recorded):\n"
        f"  Avg total: {f'{avg_sleep_h:.1f}h' if avg_sleep_h else 'N/A'}, "
        f"avg deep+REM: {f'{avg_deep_rem_pct}%' if avg_deep_rem_pct else 'N/A'}\n\n"
        f"TRAINING SESSIONS ({len(training_sessions)}): {type_summary}\n"
        f"  Split: {_session_split(training_sessions)} — use THIS for lifting-vs-"
        f"conditioning counts; it matches the PER-WEEK series (a Strength label is "
        f"lifting, a HIIT label is split by heart rate)\n"
        f"  {rpe_summary}\n"
        + ("\n".join(workout_lines) if workout_lines else "  None recorded") + "\n\n"
        f"WALKS / ACTIVE RECOVERY: {walk_summary}\n\n"
        f"TRAINING LOAD (end of week): ATL {_fmt(latest_dm.atl)}, "
        f"CTL {_fmt(latest_dm.ctl)}, ACWR {_fmt(latest_dm.acwr, '.2f')}\n\n"
        f"WEIGHT: {weight_str}\n"
        f"WELLBEING: {wb_str}\n"
        f"NUTRITION:\n  {weekly_nutrition_str}"
    )

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        create_kwargs = dict(
            model=MODEL_WEEKLY,
            # Raised from 1100 with the Sonnet 5 bump: thinking tokens come out of this
            # same budget, so a limit sized for the visible answer alone truncates it.
            # 6000 → 10000 (note 88): the 10-19..25 review spent all 6000 thinking and
            # returned nothing. _create_complete retries at 2× if even this runs out.
            max_tokens=10000,
            # + the chart menu (note 66): the model may place `[chart: id]` markers; the
            # app draws them from real data — the model never supplies a number.
            system=[{"type": "text", "text": WEEKLY_SYSTEM_PROMPT + _chart_prompt("weekly")}],
            messages=[{"role": "user", "content": user_msg}],
        )
        msg = await _create_complete(client, create_kwargs, "weekly_summary")
        text = _text_of(msg)
        from app.services.summary_charts import sanitize as _sanitize_charts
        text = _sanitize_charts(text, "weekly")   # unknown / extra chart markers dropped
        await _log_call("weekly_summary", msg.usage.input_tokens, msg.usage.output_tokens, MODEL_WEEKLY)
        from app.services.citations import enforce as _enforce_citations
        text = await _enforce_citations(
            client, create_kwargs, text,
            user_msg.replace(prior_sum_ctx, "") if prior_sum_ctx else user_msg,
            kind="weekly", period_key=week_start.isoformat(), feature="weekly_summary",
            model=MODEL_WEEKLY, postprocess=lambda t: _sanitize_charts(t, "weekly"))
        text = enforce_gap_direction(text, goaltraj_ctx)
    except Exception as exc:
        logger.error("Weekly summary: Claude API failed: %s", exc)
        return None
    if not (text or "").strip():
        # An empty row rendered as a blank summary and suppressed its push (note 88).
        logger.error("Weekly summary: no text even after a retry — nothing stored")
        return None

    # Store
    async with AsyncSessionLocal() as db:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        stmt = sqlite_insert(WeeklySummary).values(
            week_start=week_start,
            week_end=week_end,
            summary_text=text,
            # MODEL_WEEKLY, not MODEL. Storing the DAILY constant here stamped every
            # weekly row `claude-haiku-4-5` no matter what actually ran, which made it
            # impossible to tell that weekly had been raised to Sonnet — the billing log
            # was the only place the truth survived. Fixed 2026-09-08.
            model=MODEL_WEEKLY,
            created_at=datetime.utcnow(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["week_start"],
            set_={"summary_text": text, "model": MODEL_WEEKLY,
                  "created_at": datetime.utcnow()},
        )
        await db.execute(stmt)
        await db.commit()

    logger.info("Weekly summary stored for week %s–%s", week_start, week_end)
    return text


def _month_agg_str(label: str, daily_rows, sleep_rows, food_rows, excluded=frozenset()) -> str:
    """Return a compact aggregate string for a calendar month, used in month-over-month context."""
    if not daily_rows:
        return f"  {label}: no data"
    readiness_vals = [r.readiness_score for r in daily_rows if r.readiness_score is not None]
    avg_r = sum(readiness_vals) / len(readiness_vals) if readiness_vals else None
    hrv_vals = [r.hrv_z_score for r in daily_rows if r.hrv_z_score is not None]
    avg_hrv = sum(hrv_vals) / len(hrv_vals) if hrv_vals else None
    sleep_totals = [s.total_min for s in sleep_rows if s.total_min]
    avg_sleep = sum(sleep_totals) / len(sleep_totals) / 60 if sleep_totals else None
    latest_ctl = next((r.ctl for r in reversed(daily_rows) if r.ctl is not None), None)

    parts = [
        f"avg readiness {avg_r:.0f}/100" if avg_r is not None else "readiness N/A",
        f"avg HRV z{avg_hrv:+.2f}" if avg_hrv is not None else "",
        f"avg sleep {avg_sleep:.1f}h" if avg_sleep else "",
        f"CTL at month end {latest_ctl:.0f}" if latest_ctl is not None else "",
    ]
    if food_rows:
        from collections import defaultdict
        by_day: dict = defaultdict(list)
        for r in food_rows:
            by_day[food_day(r.timestamp)].append(r)
        n_left_out = sum(1 for d in by_day if d in excluded)
        by_day = {d: rows for d, rows in by_day.items() if d not in excluded}
        days_logged = len(by_day)
        if days_logged:
            avg_cal = sum(sum(r.calories or 0 for r in rows) for rows in by_day.values()) / days_logged
            avg_prot = sum(sum(r.protein_g or 0 for r in rows) for rows in by_day.values()) / days_logged
            parts.append(f"nutrition avg {round(avg_cal)} kcal/{avg_prot:.0f}g prot over "
                         f"{days_logged} complete-log days"
                         + (f" ({n_left_out} incomplete left out)" if n_left_out else ""))
    return f"  {label}: " + " · ".join(p for p in parts if p)


async def generate_monthly_summary(target_year: Optional[int] = None, target_month: Optional[int] = None) -> Optional[str]:
    """
    Generate, store, and return a monthly AI summary.
    Defaults to the prior calendar month. Returns None on failure.
    """
    from collections import defaultdict
    from sqlalchemy import select, desc
    from app.config import settings
    from app.database import AsyncSessionLocal
    from app.models import (
        DailyMetric, FoodLog, SleepSession, Workout, UserSettings,
        MonthlySummary, HealthMetric, SubjectiveLog,
    )

    if not settings.anthropic_api_key:
        return None

    async with AsyncSessionLocal() as db:
        user_settings = (await db.execute(select(UserSettings).limit(1))).scalars().first()
        if user_settings is not None and not user_settings.ai_enabled:
            return None

        today = local_today()
        # Default: prior calendar month
        if target_year is None or target_month is None:
            first_of_this_month = today.replace(day=1)
            last_month = first_of_this_month - timedelta(days=1)
            target_year, target_month = last_month.year, last_month.month

        month_start = date(target_year, target_month, 1)
        last_day = calendar.monthrange(target_year, target_month)[1]
        month_end = date(target_year, target_month, last_day)
        year_month = f"{target_year:04d}-{target_month:02d}"

        # Current month data
        daily_rows = (await db.execute(
            select(DailyMetric)
            .where(DailyMetric.date >= month_start)
            .where(DailyMetric.date <= month_end)
            .order_by(DailyMetric.date)
        )).scalars().all()

        sleep_rows = (await db.execute(
            select(SleepSession)
            .where(SleepSession.date >= month_start)
            .where(SleepSession.date <= month_end)
        )).scalars().all()

        from app.timeutils import local_day_bounds_utc
        workout_rows = (await db.execute(
            select(Workout)
            .where(Workout.start_time >= local_day_bounds_utc(month_start)[0])
            .where(Workout.start_time < local_day_bounds_utc(month_end)[1])
            .order_by(Workout.start_time)
        )).scalars().all()

        food_rows = (await db.execute(
            select(FoodLog)
            .where(FoodLog.timestamp >= food_day_start(month_start))
            .where(FoodLog.timestamp < food_day_end(month_end))
            .where((FoodLog.skipped == False) | (FoodLog.skipped.is_(None)))  # noqa: E712
        )).scalars().all()
        # Incomplete-log days out of the averages, as in the trajectory (note 104:
        # a month with a trip read ~20% low with them in).
        from app.services.day_flags import nutrition_excluded_dates
        nutrition_excluded = await nutrition_excluded_dates(db, month_start, month_end)

        weight_rows = (await db.execute(
            select(HealthMetric)
            .where(HealthMetric.metric_type == "weight")
            .where(HealthMetric.timestamp >= local_day_bounds_utc(month_start)[0])
            .where(HealthMetric.timestamp < local_day_bounds_utc(month_end)[1])
            .order_by(HealthMetric.timestamp)
        )).scalars().all()

        wellbeing_rows = (await db.execute(
            select(SubjectiveLog)
            .where(SubjectiveLog.date >= month_start)
            .where(SubjectiveLog.date <= month_end)
        )).scalars().all()

        training_goal = (user_settings.training_goal if user_settings else "building").capitalize()
        # Derived, not the raw column — see the daily builder above.
        protein_goal_g = await resolve_protein_goal(db, user_settings)
        # Per-day, not resolve_calorie_goal()["value"] — that is None in balance/pace
        # mode, so the Nutrition paragraph has been asked to report "calorie adherence
        # vs goals" with no goal to compare against.
        from app.services.energy import effective_calorie_goal
        cal_goal_by_day: dict = {}
        if user_settings:
            _d = month_start
            while _d <= month_end:
                _g = (await effective_calorie_goal(db, user_settings, _d))["goal"]
                if _g:
                    cal_goal_by_day[_d] = _g
                _d += timedelta(days=1)
        # A dynamic goal has no single number; the median in force represents it.
        _cg = sorted(cal_goal_by_day.values())
        cal_goal_resolved = _cg[len(_cg) // 2] if _cg else None

        # Prior 2 months for month-over-month comparison
        prior_month_data = []
        for offset in (1, 2):
            # Walk back `offset` months from target
            pm = month_start - timedelta(days=1)
            for _ in range(offset - 1):
                pm = pm.replace(day=1) - timedelta(days=1)
            pm_start = pm.replace(day=1)
            pm_end = pm
            pm_daily = (await db.execute(
                select(DailyMetric)
                .where(DailyMetric.date >= pm_start)
                .where(DailyMetric.date <= pm_end)
                .order_by(DailyMetric.date)
            )).scalars().all()
            pm_sleep = (await db.execute(
                select(SleepSession)
                .where(SleepSession.date >= pm_start)
                .where(SleepSession.date <= pm_end)
            )).scalars().all()
            pm_food = (await db.execute(
                select(FoodLog)
                .where(FoodLog.timestamp >= food_day_start(pm_start))
                .where(FoodLog.timestamp < food_day_end(pm_end))
                .where((FoodLog.skipped == False) | (FoodLog.skipped.is_(None)))  # noqa: E712
            )).scalars().all()
            pm_excluded = await nutrition_excluded_dates(db, pm_start, pm_end)
            label = pm_start.strftime("%b %Y")
            prior_month_data.append((label, pm_daily, pm_sleep, pm_food, pm_excluded))

    if not daily_rows:
        logger.info("Monthly summary: no data for %s, skipping", year_month)
        return None

    from app.workout_categories import LOW_INTENSITY_TYPES

    # ── Format current month context ───────────────────────────────────────────

    readiness_vals = [r.readiness_score for r in daily_rows if r.readiness_score is not None]
    avg_readiness = sum(readiness_vals) / len(readiness_vals) if readiness_vals else None
    hrv_vals = [r.hrv_z_score for r in daily_rows if r.hrv_z_score is not None]
    avg_hrv_z = sum(hrv_vals) / len(hrv_vals) if hrv_vals else None
    rhr_vals = [r.rhr_z_score for r in daily_rows if r.rhr_z_score is not None]
    avg_rhr_z = sum(rhr_vals) / len(rhr_vals) if rhr_vals else None

    sleep_totals = [s.total_min for s in sleep_rows if s.total_min]
    sleep_deep_rem = [(s.deep_min or 0) + (s.rem_min or 0) for s in sleep_rows if s.total_min]
    avg_sleep_h = sum(sleep_totals) / len(sleep_totals) / 60 if sleep_totals else None
    avg_deep_rem_pct = (
        round(sum(sleep_deep_rem) / sum(sleep_totals) * 100) if sleep_totals else None
    )

    latest_dm = daily_rows[-1]
    first_dm = daily_rows[0]

    # Weight
    weight_str = "No weight data"
    if weight_rows:
        first_w = weight_rows[0].value
        last_w = weight_rows[-1].value
        weight_str = (
            f"Start {first_w:.1f} → End {last_w:.1f} lb "
            f"({last_w - first_w:+.1f} lb over {len(weight_rows)} readings)"
        )

    # Wellbeing
    wb_str = "No wellbeing entries"
    # Scored entries only — an unscored one (10-12) crashed this with a None in the sum,
    # which would have killed the whole October summary (found by a dry run, 2026-10-25).
    wb_scores = [r.wellbeing_score for r in wellbeing_rows if r.wellbeing_score is not None]
    if wb_scores:
        avg_wb = sum(wb_scores) / len(wb_scores)
        wb_str = f"Avg {avg_wb:.1f}/10 over {len(wb_scores)} scored entries"

    # Workouts — per-type breakdown, training vs walks separated
    training_rows_mo = [w for w in workout_rows if (w.activity_type or "") not in LOW_INTENSITY_TYPES]
    walk_rows_mo     = [w for w in workout_rows if (w.activity_type or "") in LOW_INTENSITY_TYPES]

    workout_type_counts: dict = defaultdict(int)
    workout_type_mins:   dict = defaultdict(float)
    workout_type_cals:   dict = defaultdict(float)
    workout_type_rpe:    dict = defaultdict(list)
    total_workout_cals = 0.0
    for w in training_rows_mo:
        wtype = w.activity_type or "Workout"
        workout_type_counts[wtype] += 1
        workout_type_mins[wtype]   += (w.duration_sec or 0) / 60
        workout_type_cals[wtype]   += w.active_calories or 0
        total_workout_cals         += w.active_calories or 0
        if w.rpe is not None:
            workout_type_rpe[wtype].append(w.rpe)

    if training_rows_mo:
        type_lines = []
        for k, v in sorted(workout_type_counts.items(), key=lambda x: -x[1]):
            rpe_list = workout_type_rpe[k]
            rpe_part = f", avg RPE {sum(rpe_list)/len(rpe_list):.1f}" if rpe_list else ""
            type_lines.append(
                f"    {k}: {v} sessions, {round(workout_type_mins[k])}min total"
                f", {round(workout_type_cals[k])} kcal{rpe_part}"
            )
        workout_str = (f"{len(training_rows_mo)} training sessions, {round(total_workout_cals)} kcal total"
                       f" — split {_session_split(training_rows_mo)}\n" + "\n".join(type_lines))
    else:
        workout_str = "No training sessions recorded"

    if walk_rows_mo:
        total_walk_min = sum((w.duration_sec or 0) / 60 for w in walk_rows_mo)
        walk_str = f"{len(walk_rows_mo)} walks ({round(total_walk_min)} min total)"
    else:
        walk_str = "None"

    # Nutrition — per-week consistency + macro averages + goal adherence
    cal_goal  = cal_goal_resolved
    if food_rows:
        by_day: dict = defaultdict(list)
        for r in food_rows:
            by_day[food_day(r.timestamp)].append(r)
        left_out = sorted(d for d in by_day if d in nutrition_excluded)
        by_day = {d: rows for d, rows in by_day.items() if d not in nutrition_excluded}
    if food_rows and by_day:
        days_logged = len(by_day)
        day_cals  = [sum(r.calories  or 0 for r in rows) for rows in by_day.values()]
        day_prots = [sum(r.protein_g or 0 for r in rows) for rows in by_day.values()]
        day_carbs = [sum(r.carbs_g   or 0 for r in rows) for rows in by_day.values()]
        day_fats  = [sum(r.fat_g     or 0 for r in rows) for rows in by_day.values()]
        avg_cal  = sum(day_cals)  / days_logged
        avg_prot = sum(day_prots) / days_logged
        avg_carbs= sum(day_carbs) / days_logged
        avg_fat  = sum(day_fats)  / days_logged

        cal_goal_str = ""
        if cal_goal_by_day:
            # Per-day goal: in pace mode it moves with each day's activity.
            days_on_cal = sum(
                1 for _d, _rows in by_day.items()
                if cal_goal_by_day.get(_d)
                and abs(sum(r.calories or 0 for r in _rows) - cal_goal_by_day[_d])
                    / cal_goal_by_day[_d] <= 0.15
            )
            cal_goal_str = (f" (goal ~{cal_goal}/day, varies with activity; "
                            f"on-target {days_on_cal}/{days_logged} logged days)")
        prot_goal_str = ""
        if protein_goal_g:
            days_on_prot = sum(1 for p in day_prots if p >= protein_goal_g * 0.9)
            prot_goal_str = f" (goal {protein_goal_g}g; hit {days_on_prot}/{days_logged} logged days)"

        # Per-week logging consistency
        week_consistency = []
        wk = month_start
        while wk <= month_end:
            wk_end = min(wk + timedelta(days=6), month_end)
            logged = sum(1 for d in by_day if wk <= d <= wk_end)
            days_in_wk = (wk_end - wk).days + 1
            week_consistency.append(f"{logged}/{days_in_wk}d")
            wk = wk_end + timedelta(days=1)
        consistency_str = "  Complete-log days per week: " + ", ".join(week_consistency)

        left_out_note = (
            f"; {len(left_out)} logged day{'s' if len(left_out) != 1 else ''} flagged "
            f"incomplete left out of every figure here" if left_out else ""
        )
        nutrition_str = (
            f"Averages over {days_logged} of {last_day} days (days with a complete log)"
            f"{left_out_note}\n"
            f"  Avg calories: {round(avg_cal)} kcal/day{cal_goal_str}\n"
            f"  Avg protein:  {avg_prot:.0f}g/day{prot_goal_str}\n"
            f"  Avg carbs:    {avg_carbs:.0f}g/day · Avg fat: {avg_fat:.0f}g/day\n"
            f"{consistency_str}"
        )
    elif food_rows:
        nutrition_str = f"No complete-log days ({len(left_out)} logged days flagged incomplete)"
    else:
        nutrition_str = "No meals logged"

    # Month-over-month comparison block
    prior_blocks = [_month_agg_str(label, dr, sr, fr, ex) for label, dr, sr, fr, ex in prior_month_data]
    prior_section = (
        "PRIOR MONTHS (most recent first):\n" + "\n".join(prior_blocks) + "\n\n"
        if any(d for _, d, _, _, _ in prior_month_data) else ""
    )

    # Readiness distribution
    high_days = sum(1 for v in readiness_vals if v >= 75)
    mod_days  = sum(1 for v in readiness_vals if 50 <= v < 75)
    low_days  = sum(1 for v in readiness_vals if v < 50)

    # Personalized + baseline + anti-repetition enrichment (fail-safe → "")
    from app.services.summary_context import (
        baseline_block, blood_block, day_flags_block, diet_block,
        enforce_gap_direction, goal_trajectory_block, intake_block, period_series_block, personal_patterns_block,
        prior_monthly_block,
    )
    async with AsyncSessionLocal() as ctx_db:
        patterns_ctx  = await personal_patterns_block(ctx_db)
        baseline_ctx  = await baseline_block(ctx_db, month_start, month_end, 90, "PRIOR-90-DAY")
        prior_sum_ctx = await prior_monthly_block(ctx_db, year_month, n=2)
        blood_ctx     = await blood_block(ctx_db)
        diet_ctx      = await diet_block(ctx_db, month_start, month_end, 90, "PRIOR-90-DAY")
        flags_ctx     = await day_flags_block(ctx_db, month_start, month_end)
        goaltraj_ctx  = await goal_trajectory_block(ctx_db, month_end, weeks=8)
        series_ctx    = await period_series_block(ctx_db, "month", month_end)
        intake_ctx    = await intake_block(ctx_db, month_start, month_end)

    user_msg = (
        f"Athlete: {_athlete_profile(user_settings)}\n"
        f"Month: {month_start.strftime('%B %Y')} ({month_start} to {month_end})\n"
        f"Training goal: {training_goal}\n\n"
        f"{prior_sum_ctx}"
        f"{patterns_ctx}"
        f"{blood_ctx}"
        f"{prior_section}"
        f"{baseline_ctx}"
        f"{series_ctx}"
        f"{diet_ctx}"
        f"{flags_ctx}"
        f"{goaltraj_ctx}"
        f"READINESS ({len(readiness_vals)} days with data):\n"
        f"  Avg {_fmt(avg_readiness)}/100 · High {high_days}d · Moderate {mod_days}d · Low/Rest {low_days}d\n"
        f"  CTL: {_fmt(first_dm.ctl)} → {_fmt(latest_dm.ctl)} (start → end of month)\n"
        f"  ACWR range: min {_fmt(min((r.acwr for r in daily_rows if r.acwr), default=None))}"
        f" max {_fmt(max((r.acwr for r in daily_rows if r.acwr), default=None))}\n\n"
        f"HRV / RHR:\n"
        f"  Avg HRV z-score: {_fmt(avg_hrv_z, '+.2f')}\n"
        f"  Avg RHR z-score: {_fmt(avg_rhr_z, '+.2f')}\n\n"
        f"SLEEP ({len(sleep_totals)} nights recorded):\n"
        f"  Avg total: {f'{avg_sleep_h:.1f}h' if avg_sleep_h else 'N/A'}, "
        f"avg deep+REM: {f'{avg_deep_rem_pct}%' if avg_deep_rem_pct else 'N/A'}\n\n"
        f"TRAINING SESSIONS:\n  {workout_str}\n\n"
        f"WALKS / ACTIVE RECOVERY: {walk_str}\n\n"
        f"WEIGHT: {weight_str}\n"
        f"WELLBEING: {wb_str}\n"
        f"NUTRITION:\n  {nutrition_str}\n\n"
        f"{intake_ctx}"
    )

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        create_kwargs = dict(
            model=MODEL_MONTHLY,
            # Raised from 1200 — see the weekly call. Opus thinks more, not less.
            max_tokens=8000,
            system=[{"type": "text", "text": MONTHLY_SYSTEM_PROMPT + _chart_prompt("monthly")}],
            messages=[{"role": "user", "content": user_msg}],
        )
        msg = await _create_complete(client, create_kwargs, "monthly_summary")
        text = _text_of(msg)
        from app.services.summary_charts import sanitize as _sanitize_charts
        text = _sanitize_charts(text, "monthly")   # unknown / extra chart markers dropped
        await _log_call("monthly_summary", msg.usage.input_tokens, msg.usage.output_tokens, MODEL_MONTHLY)
        from app.services.citations import enforce as _enforce_citations
        text = await _enforce_citations(
            client, create_kwargs, text,
            user_msg.replace(prior_sum_ctx, "") if prior_sum_ctx else user_msg,
            kind="monthly", period_key=year_month, feature="monthly_summary",
            model=MODEL_MONTHLY, postprocess=lambda t: _sanitize_charts(t, "monthly"))
        text = enforce_gap_direction(text, goaltraj_ctx)
    except Exception as exc:
        logger.error("Monthly summary: Claude API failed: %s", exc)
        return None
    if not (text or "").strip():
        logger.error("Monthly summary: no text even after a retry — nothing stored")
        return None

    # Store
    async with AsyncSessionLocal() as db:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        stmt = sqlite_insert(MonthlySummary).values(
            year_month=year_month,
            month_start=month_start,
            month_end=month_end,
            summary_text=text,
            # MODEL_MONTHLY, not MODEL — same bug as the weekly store above.
            model=MODEL_MONTHLY,
            created_at=datetime.utcnow(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["year_month"],
            set_={"summary_text": text, "model": MODEL_MONTHLY,
                  "created_at": datetime.utcnow()},
        )
        await db.execute(stmt)
        await db.commit()

    logger.info("Monthly summary stored for %s", year_month)
    return text
