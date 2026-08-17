"""Structured LLM failure taxonomy (#55).

Every LLM call in this codebase can fail in ways that want *different*
handling — a 429 wants a backoff, a malformed-JSON body wants an immediate
same-model retry, a bad API key wants neither. Before this module both call
sites decided that by catching concrete exception types ad hoc, so the
decision lived in whichever ``except`` clause happened to be written first:
``holding_parser.parse()`` caught ``openai.OpenAIError`` wholesale and
retried *everything* (burning one of only two attempts on an
unauthenticated key), while ``report_llm._call_llm`` special-cased exactly
two classes and let the rest — 5xx included — through unretried.

This module owns one job: **classifying** a raised exception into a code
that states, by itself, whether retrying or switching model could help.
It deliberately does NOT own retry *policy* (how many attempts, how long to
wait). The two call sites have order-of-magnitude different budgets and
must keep their own loops:

- ``holding_parser.parse()`` runs inside an interactive upload bounded by a
  45s SLA (``holdings_tasks._SLA_SECONDS``), with each attempt capped at
  ``_PARSE_ATTEMPT_TIMEOUT_SECONDS`` (20s). It cannot afford ANY backoff
  sleep — ``llm_retry.yml``'s 30s/90s connection waits alone would blow the
  hard time limit and get the worker SIGKILLed.
- ``report_llm._call_llm`` runs inside a Celery report task with minutes of
  headroom and uses exactly those configured backoff sequences.

Sharing the taxonomy while keeping the loops separate is the point. Do not
"unify" the retry loops themselves.

Reference (issue #55): ZhuLinsen/daily_stock_analysis's
``generation_backend.py`` carries the same idea (an error that states its
own ``retryable``/``fallbackable``); its 990-line local-CLI backend is
deliberately not borrowed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

import openai
from pydantic import ValidationError


class LLMErrorCode(StrEnum):
    """What kind of failure this was, independent of which SDK raised it."""

    RATE_LIMIT = "rate_limit"
    CONNECTION = "connection"
    SERVER_ERROR = "server_error"
    EMPTY_RESPONSE = "empty_response"
    INVALID_JSON = "invalid_json"
    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"
    AUTH = "auth"
    BAD_REQUEST = "bad_request"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ErrorPolicy:
    """What the *error itself* says about how to react to it.

    ``retryable``: could an identical call to the same model plausibly
    succeed? (Transport faults, provider hiccups, and non-deterministic
    output-format misses — yes. Our own bad credentials or malformed
    request — no, an identical call reproduces it exactly.)

    ``fallbackable``: could routing the same call to a *different*
    model/provider plausibly succeed? Currently informational only — no
    call site has a second-tier model to escalate to (holdings parsing runs
    one model twice since #84; ``_BYOK_PROVIDER_ORDER`` is a compliance
    hard pin that by definition must not fall back). Kept because it is
    half of the classification's meaning and the eventual consumer is
    obvious; a consumer must NOT be invented for it speculatively.
    """

    retryable: bool
    fallbackable: bool


_POLICIES: dict[LLMErrorCode, ErrorPolicy] = {
    # Transient by nature — the same call later, or on another provider, works.
    LLMErrorCode.RATE_LIMIT: ErrorPolicy(retryable=True, fallbackable=True),
    # Our side of the wire to OpenRouter. Retrying helps; picking a different
    # upstream model does not (the failing hop is before provider selection).
    LLMErrorCode.CONNECTION: ErrorPolicy(retryable=True, fallbackable=False),
    LLMErrorCode.SERVER_ERROR: ErrorPolicy(retryable=True, fallbackable=True),
    # A 200 carrying no usable choices — observed from a specific provider
    # (see LLMEmptyResponseError); both retry and reroute are plausible fixes.
    LLMErrorCode.EMPTY_RESPONSE: ErrorPolicy(retryable=True, fallbackable=True),
    # Model failed to honour the requested output shape. Non-deterministic
    # even at temperature=0, so an identical retry is the primary remedy.
    LLMErrorCode.INVALID_JSON: ErrorPolicy(retryable=True, fallbackable=True),
    LLMErrorCode.SCHEMA_VALIDATION_FAILED: ErrorPolicy(retryable=True, fallbackable=True),
    # Ours to fix, not the provider's — an identical call reproduces these
    # exactly, so retrying only burns budget and delays the real diagnosis.
    LLMErrorCode.AUTH: ErrorPolicy(retryable=False, fallbackable=False),
    LLMErrorCode.BAD_REQUEST: ErrorPolicy(retryable=False, fallbackable=False),
    # Conservative default: an unclassified failure is assumed non-transient
    # rather than retried blindly. Every openai.APIStatusError IS classified
    # below (by status code), so reaching UNKNOWN means something genuinely
    # outside the known surface.
    LLMErrorCode.UNKNOWN: ErrorPolicy(retryable=False, fallbackable=False),
}


def policy_for(code: LLMErrorCode) -> ErrorPolicy:
    return _POLICIES[code]


class LLMCallError(RuntimeError):
    """An LLM call failure that carries its own retry/fallback verdict.

    Subclasses ``RuntimeError`` deliberately: ``routers/reports.py`` and
    ``tasks/holdings_tasks.py`` already branch on ``RuntimeError`` to turn a
    generation failure into a 502 / a ``failed`` upload job, and that
    contract predates this taxonomy.
    """

    def __init__(self, code: LLMErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        policy = policy_for(code)
        self.retryable = policy.retryable
        self.fallbackable = policy.fallbackable


class LLMEmptyResponseError(LLMCallError):
    """A 200 response from OpenRouter carried no usable choices.

    Observed once as a malformed 200 (``choices=None``) from a low-cost model
    via DigitalOcean/Venice — ``resp.choices[0]`` then raised a bare,
    unclassified TypeError. Raising a named error lets callers (and Celery's
    retry) treat it as a transient provider fault rather than a code bug.
    (I-DEBT-2)
    """

    def __init__(self, message: str) -> None:
        super().__init__(LLMErrorCode.EMPTY_RESPONSE, message)


class LLMInvalidJSONError(LLMCallError):
    """The model returned a 200 whose body was not parseable JSON."""

    def __init__(self, message: str) -> None:
        super().__init__(LLMErrorCode.INVALID_JSON, message)


def _classify_status(exc: openai.APIStatusError) -> LLMErrorCode:
    """Map an HTTP-status-bearing SDK error, by status code.

    Done by status rather than by exception subclass so that every
    ``APIStatusError`` lands somewhere deliberate — a future SDK version
    adding a new subclass (or OpenRouter returning a status the SDK maps to
    the base class) still gets the right verdict instead of falling to
    UNKNOWN.
    """
    status = exc.status_code
    if status == 429:
        return LLMErrorCode.RATE_LIMIT
    if status in (401, 403):
        return LLMErrorCode.AUTH
    if status >= 500:
        return LLMErrorCode.SERVER_ERROR
    if status >= 400:
        return LLMErrorCode.BAD_REQUEST
    return LLMErrorCode.UNKNOWN


def classify(exc: BaseException) -> LLMErrorCode:
    """Classify a raised exception into an :class:`LLMErrorCode`.

    Order matters: the most specific types are matched first, since the
    openai SDK's hierarchy nests (``APITimeoutError`` is an
    ``APIConnectionError``; every status error is an ``APIError``).
    """
    if isinstance(exc, LLMCallError):
        return exc.code
    # APITimeoutError subclasses APIConnectionError — both are the same
    # "never got a usable response over the wire" case, both retryable.
    if isinstance(exc, openai.APIConnectionError):
        return LLMErrorCode.CONNECTION
    if isinstance(exc, openai.APIStatusError):
        return _classify_status(exc)
    # The SDK could not parse what the server sent back — same practical
    # shape as an empty/garbled 200, and equally worth one more attempt.
    if isinstance(exc, openai.APIResponseValidationError):
        return LLMErrorCode.EMPTY_RESPONSE
    if isinstance(exc, json.JSONDecodeError):
        return LLMErrorCode.INVALID_JSON
    if isinstance(exc, ValidationError):
        return LLMErrorCode.SCHEMA_VALIDATION_FAILED
    return LLMErrorCode.UNKNOWN


def is_retryable(exc: BaseException) -> bool:
    """Shorthand for ``policy_for(classify(exc)).retryable``."""
    return policy_for(classify(exc)).retryable
