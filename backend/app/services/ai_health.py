"""Why is AI unavailable, and since when?

Every AI call in this app catches broadly and returns None. That is the right
failure posture — a parse must never take down the modal — but it throws away the
one thing needed to fix the problem, and the UI then guesses. On 2026-09-04 the
Anthropic credit balance ran out and the app said:

    "AI parsing unavailable — check that your API key is set and AI is enabled
     in Settings"

Both of those were fine. The key was valid, AI was enabled, and the actual cause
(a 400 saying the credit balance was too low) was visible only by hand-running a
script inside the container. The message did not merely fail to help, it pointed
at two things that were not broken.

So this module records the LAST OUTCOME of every AI call and classifies failures
into causes that differ in what the user should do:

    credit      balance exhausted -> buy credits. Nothing in the app can fix it.
    auth        key rejected -> replace ANTHROPIC_API_KEY in backend/.env
    rate_limit  429 -> wait; usually self-resolves
    upstream    5xx / overloaded -> wait; not your fault
    network     could not reach the API at all -> check the box's connectivity
    disabled    ai_enabled is off, or no key configured. Not a failure.
    bad_request some other 400 — a real bug in a request we built
    unknown     anything unclassified, message preserved verbatim

DELIBERATELY IN-MEMORY. This is the health of a live dependency, and a restart is
exactly when it should be re-checked rather than remembered — a persisted "down"
that outlives the outage is worse than no signal. The cost is that the state
resets on deploy, which is acceptable: the next call repopulates it in seconds.

Follows the freshness module's rule — REPORT, never gate. Nothing here blocks a
call; a caller that wants to try anyway always may, and should, because the
recorded state may be stale by the time it is read.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Ordered most-specific first: a credit failure is also a BadRequestError, so the
# message test has to run before the generic 400 branch.
CAUSE_LABELS = {
    "ok":          "Working",
    "credit":      "Out of API credit",
    "auth":        "API key rejected",
    "rate_limit":  "Rate limited",
    "upstream":    "Anthropic is having trouble",
    "network":     "Cannot reach the API",
    "bad_request": "Bad request (app bug)",
    "disabled":    "AI is switched off",
    "unknown":     "Unavailable",
}

# What the person reading this can actually DO. "Nothing" is a legitimate answer
# and is better than inventing a remedy — the old message invented two.
CAUSE_ACTIONS = {
    "credit":      "Add credits at console.anthropic.com → Plans & Billing. "
                   "A Claude Pro/Max subscription does NOT cover API usage.",
    "auth":        "Replace ANTHROPIC_API_KEY in backend/.env on the server, then redeploy.",
    "rate_limit":  "Wait a minute and try again.",
    "upstream":    "Anthropic-side. Try again shortly.",
    "network":     "Check the server's outbound connectivity and DNS.",
    "bad_request": "Not fixable from here — the request itself is malformed.",
    "disabled":    "Turn AI on in Settings.",
    "unknown":     "Try again; if it persists the detail below is the raw error.",
}


def classify(exc: BaseException) -> tuple[str, str]:
    """(cause, human-readable detail) for one exception from the Anthropic SDK.

    Matches on the SDK's typed exception classes by NAME rather than importing
    them, so this module stays importable when `anthropic` is absent and does not
    break if the SDK reorganises its exception hierarchy.
    """
    name = type(exc).__name__
    msg = str(exc)
    low = msg.lower()

    # Credit exhaustion arrives as a 400, not a 402, and the ONLY thing separating
    # it from a genuine bad request is this sentence. Check it before the 400 branch.
    if "credit balance is too low" in low or "purchase credits" in low:
        return "credit", "Anthropic reports the credit balance is too low."
    if name in ("AuthenticationError", "PermissionDeniedError") or "invalid x-api-key" in low:
        return "auth", "Anthropic rejected the API key."
    if name == "RateLimitError" or " 429" in msg:
        return "rate_limit", "Rate limited by the API."
    if name in ("InternalServerError", "APIStatusError") and (" 5" in msg or "overloaded" in low):
        return "upstream", "Anthropic returned a server error."
    if name in ("APIConnectionError", "APITimeoutError") or "connection" in low:
        return "network", "Could not reach the Anthropic API."
    if name == "BadRequestError":
        return "bad_request", msg[:300]
    return "unknown", msg[:300]


@dataclass
class _State:
    last_ok_at: Optional[datetime] = None
    last_fail_at: Optional[datetime] = None
    cause: str = "ok"
    detail: str = ""
    consecutive_failures: int = 0
    # Which feature most recently failed, purely so the UI can say "food parsing"
    # rather than the useless "an AI call".
    last_feature: str = ""
    notified_cause: str = field(default="", repr=False)


_state = _State()


def record_success(feature: str) -> None:
    _state.last_ok_at = datetime.utcnow()
    _state.cause = "ok"
    _state.detail = ""
    _state.consecutive_failures = 0
    _state.last_feature = feature
    _state.notified_cause = ""


def record_failure(feature: str, exc: BaseException) -> str:
    cause, detail = classify(exc)
    _state.last_fail_at = datetime.utcnow()
    _state.cause = cause
    _state.detail = detail
    _state.consecutive_failures += 1
    _state.last_feature = feature
    logger.error("AI call failed (%s): cause=%s detail=%s", feature, cause, detail)
    return cause


def record_disabled(reason: str) -> None:
    """No key, or AI switched off. Not a failure — do not inflate the failure count
    or the UI will report an outage for a setting the user chose."""
    _state.cause = "disabled"
    _state.detail = reason
    _state.last_feature = ""


def snapshot() -> dict:
    """Current state for the status endpoint.

    `state` is deliberately coarser than `cause`:
      ok        — a call has succeeded and nothing has failed since
      degraded  — failures are happening but may be transient (rate limit, upstream,
                  network). Worth showing; not worth alarming about.
      down      — nothing will work until a human acts (credit, auth).
      disabled  — switched off on purpose.
      unknown   — no call has been made yet since boot, so there is nothing to claim.
    """
    c = _state.cause
    if c == "disabled":
        state = "disabled"
    elif c == "ok":
        state = "ok" if _state.last_ok_at else "unknown"
    elif c in ("credit", "auth"):
        state = "down"
    else:
        state = "degraded"

    stale_for = None
    if state in ("down", "degraded") and _state.last_ok_at:
        stale_for = round((datetime.utcnow() - _state.last_ok_at).total_seconds() / 60)

    return {
        "state": state,
        "cause": c,
        "label": CAUSE_LABELS.get(c, "Unavailable"),
        "action": CAUSE_ACTIONS.get(c) if state in ("down", "degraded", "disabled") else None,
        "detail": _state.detail or None,
        "feature": _state.last_feature or None,
        "consecutive_failures": _state.consecutive_failures,
        "last_ok_at": _state.last_ok_at.isoformat() if _state.last_ok_at else None,
        "last_failure_at": _state.last_fail_at.isoformat() if _state.last_fail_at else None,
        "minutes_since_last_ok": stale_for,
    }


def should_notify() -> bool:
    """True once per transition into a cause a human must act on.

    Only `credit` and `auth` qualify: those do not self-resolve, and everything
    else would be paging about a blip. Latched on the cause so a run of failures
    sends one push, not one per parse — the failure mode that trains you to mute
    the topic that also carries the alerts you care about.
    """
    if _state.cause not in ("credit", "auth"):
        return False
    if _state.notified_cause == _state.cause:
        return False
    _state.notified_cause = _state.cause
    return True
