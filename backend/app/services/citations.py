"""Citation check for the written summaries (2026-10-21, replaces note 55's manual audit).

Note 55's audits found briefings quoting statistics that were not in their inputs: a wrong
ρ ("−0.38" for a real −0.31), a half-invented finding, a reversed direction. The model is
handed every finding it may cite, with its ρ and n, in the prompt — so the rule is
mechanical: **every ρ and every n in the output must appear in the input.** Checked here
after generation; on a miss the model gets ONE rewrite request naming the bad numbers, and
the better of the two answers is kept. Every check is recorded in `citation_checks`, which
Data Health reports, so a recurring problem is visible without reading briefings.

What it deliberately does NOT do: judge prose ("later bedtime → lower energy") — the
direction_phrase() wording in the inputs is the guard there — or police numbers other than
ρ and n (sleep hours, grams), which the model reads straight off labelled input lines.

WORDED CITATIONS (check_words, 2026-10-28, note 55 audit). The daily briefing stopped
quoting ρ altogether ("Confirmed: water→mood link", "low water linked to worse next-day
Energy"), so the numeric check saw 0 citations in every briefing while an UNCONFIRMED finding
went out as "New:" (10-26). Same principle — the prompt's own finding lines are the ground
truth — applied to words: an output line that links an input to an outcome must have a
finding line in the inputs naming both; "confirmed" needs a confirmed one; "New:"/"Firming:"
needs a WHAT'S NEW line; a CONFOUNDED finding must be called that. Matching is by keyword on
both sides with the SAME vocabulary, so a paraphrase the inputs also use passes.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# "ρ=+0.32", "ρ −0.42", "ρ−0.42", "rho = 0.3"; minus may be -, − (U+2212) or – (U+2013).
_RHO_RE = re.compile(r"(?:ρ|\brho\b)\s*[=≈:]?\s*([+\-−–]?)\s*(\d?\.\d+)", re.I)
_N_RE = re.compile(r"\bn\s*=\s*(\d+)")
_TOL = 0.006                    # inputs carry 2 decimals; allow the output's rounding


def _rhos(text: str) -> list[tuple[Optional[int], float, str]]:
    out = []
    for m in _RHO_RE.finditer(text or ""):
        sign = m.group(1)
        mag = float(m.group(2))
        s = None if not sign else (1 if sign == "+" else -1)
        out.append((s, mag, m.group(0).strip()))
    return out


def check(output: str, inputs: str) -> list[str]:
    """Quoted statistics in `output` that do not appear in `inputs` (empty = clean).

    A signed ρ must match an input ρ in sign and value; an unsigned one ("ρ 0.42", the
    direction given in words) must match some input in magnitude. Every n must exist."""
    have = _rhos(inputs)
    have_n = {int(x) for x in _N_RE.findall(inputs or "")}
    bad = []
    for s, mag, raw in _rhos(output):
        ok = any(abs(mag - hm) <= _TOL and (s is None or hs is None or s == hs)
                 for hs, hm, _ in have)
        if not ok:
            bad.append(raw)
    for n in _N_RE.findall(output or ""):
        if int(n) not in have_n:
            bad.append(f"n={n}")
    return sorted(set(bad))


def n_quoted(output: str) -> int:
    return len(_rhos(output)) + len(_N_RE.findall(output or "")) + len(_worded(output))


# ── Worded citations ───────────────────────────────────────────────────────────
# Outcome vocabulary is explicit (few, and "sleep" is both an input and an outcome).
_OUTCOME_TERMS = {
    "HRV": ("hrv",), "RHR": ("rhr", "resting hr", "resting heart"),
    "Respiratory rate": ("respiratory", "resp rate"), "Breathing disturbances": ("breathing",),
    "Energy": ("energy",), "Mood": ("mood",), "Sleep quality": ("sleep quality",),
    "Stress": ("stress",), "Day quality": ("day quality",),
    "Early-night HR": ("early-night", "early night"), "Wrist temp": ("wrist",),
    "Walking HR": ("walking hr",),
}
_LINK_CUES = ("→", "->", "linked", " link", "tracks", "associated", "pattern", "finding",
              "lever", "predicts", "goes with", "consistent with")
_STOP = {"more", "less", "time", "your", "daily", "from", "with", "still", "board", "what",
         "when", "higher", "lower", "better", "worse", "intake", "dose", "hour", "clock",
         "total", "min", "minutes", "days", "late", "first", "before", "screen", "use",
         "avoiding", "taking", "nights", "how", "fast", "you", "get", "outside", "falls"}
_EXTRA_INPUT_TERMS = {"alcohol": ("drink", "drinks", "sdu"), "calorie": ("kcal", "calories"),
                      "phone": ("screen time",)}


def _input_terms() -> list[str]:
    from app.services.correlations import INPUTS
    # Single-word outcome terms only: "sleep" (sleep quality) and "walking" (walking HR)
    # are also inputs.
    outcome_words = {t for ts in _OUTCOME_TERMS.values() for t in ts if " " not in t}
    terms: set[str] = set()
    for spec in INPUTS.values():
        for w in re.findall(r"[a-z][a-z0-9\-]+", f"{spec['label']} {spec['subject']}".lower()):
            if len(w) >= 4 and w not in _STOP and w not in outcome_words:
                terms.add(w)
    for k, extra in _EXTRA_INPUT_TERMS.items():
        if k in terms or any(t.startswith(k) for t in terms):
            terms.update(extra)
    return sorted(terms)


def _hits(text: str, terms) -> set[str]:
    t = text.lower()
    return {x for x in terms if re.search(rf"(?<![a-z]){re.escape(x)}(?![a-z])", t)}


def _outcomes_in(text: str) -> set[str]:
    return {o for o, ts in _OUTCOME_TERMS.items() if _hits(text, ts)}


def _worded(output: str) -> list[tuple[str, set, set]]:
    """(line, input terms, outcomes) for each output line that cites an input→outcome link."""
    terms = _input_terms()
    out = []
    for line in re.split(r"\n|•", output or ""):
        low = line.lower()
        if not any(c in low for c in _LINK_CUES):
            continue
        ins, outs = _hits(line, terms), _outcomes_in(line)
        if ins and outs:
            out.append((line.strip(), ins, outs))
    return out


def _input_findings(inputs: str) -> list[dict]:
    """Finding lines in the prompt, each with the tier its section or own text gives it."""
    terms = _input_terms()
    found, section, tier = [], None, None
    for line in (inputs or "").splitlines():
        low = line.lower()
        if not line.strip():
            section = tier = None
            continue
        if "what's new" in low:
            section, tier = "news", None
        elif "personal patterns" in low:
            section, tier = "patterns", None
        elif section == "patterns" and "confirmed levers" in low:
            tier = "confirmed"
        elif section == "patterns" and low.startswith("suggestive"):
            tier = "suggestive"
        elif section == "patterns" and "confounded associations" in low:
            tier = "confounded"
        elif section == "patterns" and "diagnostic" in low:
            tier = None
        if "→" not in line and "->" not in line:
            continue
        line_tier = ("confounded" if "confounded" in low else
                     "confirmed" if "confirmed" in low else
                     "suggestive" if "suggestive" in low else tier)
        found.append({"ins": _hits(line, terms), "outs": _outcomes_in(line),
                      "tier": line_tier, "news": section == "news"})
    return found


def check_words(output: str, inputs: str) -> list[str]:
    """Problems with findings cited in WORDS (empty = clean). See the module docstring."""
    findings = _input_findings(inputs)
    bad = []
    for line, ins, outs in _worded(output):
        low = line.lower()
        for o in outs:
            sup = [f for f in findings if o in f["outs"] and f["ins"] & ins]
            name = f"“{line[:70]}”"
            if not sup:
                bad.append(f"{name}: no finding in the data links these")
                continue
            tiers = {f["tier"] for f in sup}
            if re.search(r"\bconfirmed\b|\brobust\b|\bsolid\b", low) and "confirmed" not in tiers \
                    and tiers <= {"suggestive", "confounded"}:
                bad.append(f"{name}: called confirmed, but the data lists it as {'/'.join(sorted(tiers))}")
            if re.match(r"^\s*(new|firming)\b", low) and not any(f["news"] for f in sup):
                bad.append(f"{name}: labelled New/Firming, but it is not in WHAT'S NEW")
            if tiers == {"confounded"} and "confound" not in low:
                bad.append(f"{name}: a CONFOUNDED finding cited without saying so")
    return sorted(set(bad))


CORRECTION = (
    "Automatic check: your summary quotes {bad}, which do not appear anywhere in the data "
    "you were given. Rewrite the whole summary, otherwise unchanged, quoting ONLY "
    "correlation values (ρ) and sample sizes (n) exactly as they appear in the data above "
    "— or describe that finding without numbers. Output only the rewritten summary."
)
WORD_CORRECTION = (
    "Automatic check: these statements about your findings do not match the data you were "
    "given — {bad}. Rewrite the whole summary, otherwise unchanged: state each finding with "
    "the status the data gives it (confirmed / suggestive / CONFOUNDED, and New only if it is "
    "in WHAT'S NEW), or drop it. Output only the rewritten summary."
)


def _all_problems(text: str, inputs: str) -> tuple[list[str], list[str]]:
    return check(text, inputs), check_words(text, inputs)


async def enforce(client, create_kwargs: dict, text: str, inputs: str, *,
                  kind: str, period_key: str, feature: str, model: str,
                  postprocess: Optional[Callable[[str], str]] = None) -> str:
    """Check `text`; on a miss ask once for a rewrite; record the outcome. Never raises —
    a failed check or retry keeps the original text (and is logged)."""
    from app.services.ai_summaries import _log_call, _text_of
    report = {"n_quoted": n_quoted(text), "bad_first": [], "retried": False, "bad_final": []}
    try:
        nums, words = _all_problems(text, inputs)
        bad = nums + words
        report["bad_first"] = bad
        report["bad_final"] = bad
        if bad:
            logger.warning("Citation check (%s %s): %s not supported by inputs — asking for a "
                           "rewrite", kind, period_key, bad)
            ask = "\n\n".join(x for x in (
                CORRECTION.format(bad=", ".join(nums)) if nums else "",
                WORD_CORRECTION.format(bad="; ".join(words)) if words else "") if x)
            msgs = list(create_kwargs["messages"]) + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": ask},
            ]
            msg2 = await client.messages.create(**{**create_kwargs, "messages": msgs})
            await _log_call(feature, msg2.usage.input_tokens, msg2.usage.output_tokens, model)
            text2 = _text_of(msg2)
            if postprocess:
                text2 = postprocess(text2)
            bad2 = sum(_all_problems(text2, inputs), [])
            report.update(retried=True, bad_final=bad2, n_quoted=n_quoted(text2))
            if text2.strip() and len(bad2) < len(bad):
                text = text2
            else:
                report["bad_final"] = bad      # kept the original
            if report["bad_final"]:
                logger.warning("Citation check (%s %s): still unsupported after rewrite: %s",
                               kind, period_key, report["bad_final"])
    except Exception as exc:
        logger.error("Citation check (%s %s) failed: %s", kind, period_key, exc)
        report["error"] = str(exc)[:200]
    await _record(kind, period_key, report)
    return text


async def _record(kind: str, period_key: str, report: dict) -> None:
    try:
        from app.database import AsyncSessionLocal
        from app.models import CitationCheck
        async with AsyncSessionLocal() as db:
            db.add(CitationCheck(kind=kind, period_key=period_key, checked_at=datetime.utcnow(),
                                 n_quoted=report.get("n_quoted", 0),
                                 bad_first=json.dumps(report.get("bad_first", [])),
                                 retried=bool(report.get("retried")),
                                 bad_final=json.dumps(report.get("bad_final", [])),
                                 error=report.get("error")))
            await db.commit()
    except Exception as exc:
        logger.error("Citation check: recording failed: %s", exc)
