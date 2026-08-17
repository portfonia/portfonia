"""Tests for the structured LLM failure taxonomy (#55)."""

from __future__ import annotations

import json

import httpx
import openai
import pytest
from pydantic import BaseModel, ValidationError

from app.services.llm_errors import (
    LLMCallError,
    LLMEmptyResponseError,
    LLMErrorCode,
    LLMInvalidJSONError,
    classify,
    is_retryable,
    policy_for,
)


def _status_error(status: int) -> openai.APIStatusError:
    """Build the SDK error the client would raise for a given HTTP status."""
    request = httpx.Request("POST", "https://openrouter.test/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return openai.APIStatusError("upstream said no", response=response, body=None)


def test_every_code_has_a_policy() -> None:
    """A new code without a policy entry would KeyError at raise time."""
    for code in LLMErrorCode:
        assert isinstance(policy_for(code).retryable, bool)
        assert isinstance(policy_for(code).fallbackable, bool)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, LLMErrorCode.RATE_LIMIT),
        (401, LLMErrorCode.AUTH),
        (403, LLMErrorCode.AUTH),
        (400, LLMErrorCode.BAD_REQUEST),
        (404, LLMErrorCode.BAD_REQUEST),
        (422, LLMErrorCode.BAD_REQUEST),
        (500, LLMErrorCode.SERVER_ERROR),
        (502, LLMErrorCode.SERVER_ERROR),
        (503, LLMErrorCode.SERVER_ERROR),
        # OpenRouter's "your request timed out" — transient, not malformed.
        # Not mapped to APITimeoutError by the SDK (PR #161 review finding).
        (408, LLMErrorCode.SERVER_ERROR),
    ],
)
def test_status_errors_classify_by_code_not_subclass(status: int, expected: LLMErrorCode) -> None:
    """Classification reads status_code, so an SDK version that stops mapping
    a status to its own subclass still lands on the right verdict."""
    assert classify(_status_error(status)) == expected


def test_concrete_sdk_subclasses_agree_with_status_classification() -> None:
    request = httpx.Request("POST", "https://openrouter.test/x")
    rate_limit = openai.RateLimitError(
        "429", response=httpx.Response(429, request=request), body=None
    )
    auth = openai.AuthenticationError(
        "401", response=httpx.Response(401, request=request), body=None
    )
    server = openai.InternalServerError(
        "500", response=httpx.Response(500, request=request), body=None
    )
    assert classify(rate_limit) == LLMErrorCode.RATE_LIMIT
    assert classify(auth) == LLMErrorCode.AUTH
    assert classify(server) == LLMErrorCode.SERVER_ERROR


def test_timeout_classifies_as_connection() -> None:
    """APITimeoutError subclasses APIConnectionError — the isinstance order in
    classify() must not let the base class swallow it into something else."""
    request = httpx.Request("POST", "https://openrouter.test/x")
    assert classify(openai.APITimeoutError(request=request)) == LLMErrorCode.CONNECTION
    assert classify(openai.APIConnectionError(request=request)) == LLMErrorCode.CONNECTION


def test_json_and_schema_failures_classify() -> None:
    try:
        json.loads("not json {")
    except json.JSONDecodeError as exc:
        assert classify(exc) == LLMErrorCode.INVALID_JSON
    else:  # pragma: no cover - json.loads must raise here
        pytest.fail("expected JSONDecodeError")

    class _M(BaseModel):
        x: int

    try:
        _M.model_validate({"x": "not an int"})
    except ValidationError as exc:
        assert classify(exc) == LLMErrorCode.SCHEMA_VALIDATION_FAILED
    else:  # pragma: no cover
        pytest.fail("expected ValidationError")


def test_unrecognized_exception_is_unknown_and_not_retried() -> None:
    """A programming error must not be mistaken for a transient fault and
    retried — UNKNOWN deliberately defaults to non-retryable."""
    assert classify(TypeError("bug")) == LLMErrorCode.UNKNOWN
    assert is_retryable(TypeError("bug")) is False


def test_our_own_errors_report_their_own_code() -> None:
    empty = LLMEmptyResponseError("no choices")
    invalid = LLMInvalidJSONError("bad body")
    assert classify(empty) == LLMErrorCode.EMPTY_RESPONSE
    assert classify(invalid) == LLMErrorCode.INVALID_JSON
    assert empty.retryable is True
    assert invalid.retryable is True


def test_llm_call_error_is_a_runtime_error() -> None:
    """routers/reports.py and holdings_tasks.py both branch on RuntimeError to
    turn a generation failure into a 502 / a failed upload job — a contract
    that predates this taxonomy and must not be broken by it."""
    assert isinstance(LLMCallError(LLMErrorCode.UNKNOWN, "x"), RuntimeError)
    assert isinstance(LLMEmptyResponseError("x"), RuntimeError)


def test_client_side_faults_are_never_retryable() -> None:
    """Our bad key / malformed request reproduces identically on retry, so
    retrying only burns budget and buries the real cause."""
    assert is_retryable(_status_error(401)) is False
    assert is_retryable(_status_error(400)) is False


def test_transient_faults_are_retryable() -> None:
    request = httpx.Request("POST", "https://openrouter.test/x")
    assert is_retryable(_status_error(429)) is True
    assert is_retryable(_status_error(503)) is True
    assert is_retryable(openai.APIConnectionError(request=request)) is True
    assert is_retryable(LLMEmptyResponseError("x")) is True


def test_connection_faults_are_not_fallbackable() -> None:
    """The failing hop is ours-to-OpenRouter, before any upstream model is
    chosen — rerouting to a different model cannot fix it, unlike a 429 or a
    provider 5xx."""
    request = httpx.Request("POST", "https://openrouter.test/x")
    assert policy_for(classify(openai.APIConnectionError(request=request))).fallbackable is False
    assert policy_for(classify(_status_error(429))).fallbackable is True
    assert policy_for(classify(_status_error(500))).fallbackable is True
