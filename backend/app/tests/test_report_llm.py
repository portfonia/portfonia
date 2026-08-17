"""Tests for report_llm.py (OpenRouter transport + retry).

Split out of test_report_generator.py (#37) — moved verbatim along with the
`app.services.report_generator.*` patch targets updated to
`app.services.report_llm.*`, the module `_call_llm` now actually lives in.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest

from app.services import report_llm as rl
from app.services.llm_errors import LLMEmptyResponseError
from app.services.llm_retry_config import LLMRetryConfig

_REQUEST = httpx.Request("POST", "https://openrouter.test/v1/chat/completions")


def _fake_llm_response(content: str | None) -> MagicMock:
    """Shape a minimal OpenAI-style chat completion response."""
    resp = MagicMock()
    resp.model = "fake/model"
    if content is None:
        resp.choices = None
        return resp
    choice = MagicMock()
    choice.finish_reason = "stop"
    choice.message.content = content
    resp.choices = [choice]
    resp.usage = MagicMock(prompt_tokens=1, completion_tokens=1, total_tokens=2, cost=0.0)
    return resp


def test_call_llm_raises_on_empty_choices() -> None:
    client = MagicMock()
    client.chat.completions.create.return_value = _fake_llm_response(None)
    with patch("app.services.report_llm.time.sleep"), pytest.raises(LLMEmptyResponseError):
        rl._call_llm(client, "m", "sys", "user")


def test_call_llm_retries_a_malformed_empty_200_then_succeeds() -> None:
    """EMPTY_RESPONSE is classified retryable, so it must actually be retried
    in-process (#55). Before the taxonomy the choices guard sat AFTER the
    retry loop, so a malformed 200 always escalated straight to the outer
    Celery retry — a 5-minute delay for a fault that clears immediately,
    contradicting its own classification."""
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        _fake_llm_response(None),
        _fake_llm_response("ok"),
    ]
    with patch("app.services.report_llm.time.sleep") as sleep:
        out = rl._call_llm(client, "m", "sys", "user")
    assert out == "ok"
    assert client.chat.completions.create.call_count == 2
    sleep.assert_called_once()


def test_call_llm_retries_server_errors() -> None:
    """A provider 5xx is an APIStatusError, not an APIConnectionError, so the
    pre-#55 loop let it through unretried despite being exactly the kind of
    transient upstream fault a short backoff absorbs."""
    client = MagicMock()
    err = openai.InternalServerError(
        "502", response=httpx.Response(502, request=_REQUEST), body=None
    )
    client.chat.completions.create.side_effect = [err, _fake_llm_response("ok")]
    with patch("app.services.report_llm.time.sleep") as sleep:
        out = rl._call_llm(client, "m", "sys", "user")
    assert out == "ok"
    assert client.chat.completions.create.call_count == 2
    sleep.assert_called_once()


def test_call_llm_raises_and_retries_on_blank_message_content() -> None:
    """A 200 with message.content=None/blank is the same "nothing usable"
    shape as empty choices — before the fix `content or ""` silently
    swallowed it, so a caller relying on non-empty output only found out
    several steps later, after EMPTY_RESPONSE's own retry budget was never
    spent (PR #161 review)."""
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        _fake_llm_response("   "),  # whitespace-only
        _fake_llm_response("ok"),
    ]
    with patch("app.services.report_llm.time.sleep") as sleep:
        out = rl._call_llm(client, "m", "sys", "user")
    assert out == "ok"
    assert client.chat.completions.create.call_count == 2
    sleep.assert_called_once()


def test_call_llm_allow_empty_content_opts_out_of_the_guard() -> None:
    """report_translation.py's _translate_chunk runs its own truncation
    retry-then-fallback logic keyed on getting a (possibly empty) string
    back — it must not have that replaced by a raised exception."""
    client = MagicMock()
    client.chat.completions.create.return_value = _fake_llm_response("")
    out = rl._call_llm(client, "m", "sys", "user", allow_empty_content=True)
    assert out == ""


def test_call_llm_retries_408_request_timeout() -> None:
    """OpenRouter's 408 arrives as a bare APIStatusError, not APITimeoutError
    (PR #161 review) — must land in a retry group, not BAD_REQUEST."""
    client = MagicMock()
    err = openai.APIStatusError("408", response=httpx.Response(408, request=_REQUEST), body=None)
    client.chat.completions.create.side_effect = [err, _fake_llm_response("ok")]
    with patch("app.services.report_llm.time.sleep") as sleep:
        out = rl._call_llm(client, "m", "sys", "user")
    assert out == "ok"
    assert client.chat.completions.create.call_count == 2
    sleep.assert_called_once()


def test_call_llm_does_not_retry_auth_failures() -> None:
    """A bad key reproduces identically; retrying it just delays the real
    diagnosis behind two backoff waits."""
    client = MagicMock()
    err = openai.AuthenticationError(
        "401", response=httpx.Response(401, request=_REQUEST), body=None
    )
    client.chat.completions.create.side_effect = err
    with (
        patch("app.services.report_llm.time.sleep") as sleep,
        pytest.raises(openai.AuthenticationError),
    ):
        rl._call_llm(client, "m", "sys", "user")
    assert client.chat.completions.create.call_count == 1
    sleep.assert_not_called()


def test_call_llm_mixed_error_kinds_surface_the_real_cause() -> None:
    """Each retry group draws from its own budget, and the loop bound is their
    sum. The pre-#55 loop shared one `max(len(a), len(b)) + 1` bound across two
    independent per-type counters, so an alternating run (429, connection, 429)
    fell out of the loop with `resp` never assigned and died on `resp.choices`
    with a bare AttributeError — discarding the actual cause."""
    client = MagicMock()
    rate_limit = openai.RateLimitError(
        "429", response=httpx.Response(429, request=_REQUEST), body=None
    )
    connection = openai.APIConnectionError(request=_REQUEST)
    client.chat.completions.create.side_effect = [
        rate_limit,
        connection,
        rate_limit,
        connection,
        rate_limit,
    ]
    with (
        patch("app.services.report_llm.time.sleep"),
        pytest.raises(openai.RateLimitError),
    ):
        rl._call_llm(client, "m", "sys", "user")
    # 2 upstream + 2 connection backoffs, then the 5th call re-raises for real.
    assert client.chat.completions.create.call_count == 5


def test_call_llm_does_not_swallow_programming_errors() -> None:
    """An unclassifiable exception is UNKNOWN, hence non-retryable — it must
    propagate unchanged rather than be retried as if it were transient."""
    client = MagicMock()
    client.chat.completions.create.side_effect = TypeError("bug in our own code")
    with (
        patch("app.services.report_llm.time.sleep") as sleep,
        pytest.raises(TypeError, match="bug in our own code"),
    ):
        rl._call_llm(client, "m", "sys", "user")
    assert client.chat.completions.create.call_count == 1
    sleep.assert_not_called()


def test_call_llm_retries_on_rate_limit_then_succeeds() -> None:
    client = MagicMock()
    err = openai.RateLimitError("rate limited", response=MagicMock(status_code=429), body=None)
    client.chat.completions.create.side_effect = [err, _fake_llm_response("ok")]
    with patch("app.services.report_llm.time.sleep") as sleep:
        out = rl._call_llm(client, "m", "sys", "user")
    assert out == "ok"
    assert client.chat.completions.create.call_count == 2
    sleep.assert_called_once()  # one backoff before the successful retry


def test_call_llm_reraises_after_exhausting_rate_limit_retries() -> None:
    client = MagicMock()
    err = openai.RateLimitError("rate limited", response=MagicMock(status_code=429), body=None)
    client.chat.completions.create.side_effect = err
    with patch("app.services.report_llm.time.sleep"), pytest.raises(openai.RateLimitError):
        rl._call_llm(client, "m", "sys", "user")
    assert client.chat.completions.create.call_count == 3  # initial + 2 backoff retries


def test_call_llm_wiring_respects_custom_retry_config() -> None:
    """#38 wiring: the retry budget must actually come from
    load_llm_retry_config(), not a leftover hardcoded default. A test that
    only ever exercises the shipped (5, 15) sequence would still pass even
    if _call_llm silently ignored the loader entirely (PR #142 review,
    suggestion 2) — this one uses a *different* sequence so it can only pass
    if the loaded config is what actually drives the retry loop."""
    client = MagicMock()
    err = openai.RateLimitError("rate limited", response=MagicMock(status_code=429), body=None)
    client.chat.completions.create.side_effect = err
    custom_config = LLMRetryConfig(ratelimit_backoff_seconds=(1.0,), connect_backoff_seconds=())
    with (
        patch("app.services.report_llm.load_llm_retry_config", return_value=custom_config),
        patch("app.services.report_llm.time.sleep") as sleep,
        pytest.raises(openai.RateLimitError),
    ):
        rl._call_llm(client, "m", "sys", "user")
    assert client.chat.completions.create.call_count == 2  # initial + 1 backoff retry, not 3
    sleep.assert_called_once_with(1.0)


def test_call_llm_wiring_empty_sequence_fails_immediately() -> None:
    """An admin-set [] (documented as intentional fail-fast) must actually
    skip in-process retry entirely, not just be tolerated by the loader."""
    client = MagicMock()
    err = openai.RateLimitError("rate limited", response=MagicMock(status_code=429), body=None)
    client.chat.completions.create.side_effect = err
    empty_config = LLMRetryConfig(ratelimit_backoff_seconds=(), connect_backoff_seconds=())
    with (
        patch("app.services.report_llm.load_llm_retry_config", return_value=empty_config),
        patch("app.services.report_llm.time.sleep") as sleep,
        pytest.raises(openai.RateLimitError),
    ):
        rl._call_llm(client, "m", "sys", "user")
    assert client.chat.completions.create.call_count == 1  # no in-process retry at all
    sleep.assert_not_called()


def test_call_llm_default_keeps_marketplace_pin_and_deny() -> None:
    """PR #79 review: everything NOT opting into the BYOK exception (Pass 2,
    regenerate, and any future caller that doesn't pass the override kwargs)
    must keep data_collection=deny, the OPENROUTER_PROVIDER_ORDER marketplace
    pin, AND allow_fallbacks as configured — issue #78 only carves out Pass 1
    / translation via explicit kwargs, never by default.

    Uses a patched settings object (PR #81 review) rather than real
    .env.local values: OPENROUTER_PROVIDER_ORDER can be empty there, which
    made the order assertion conditional and let allow_fallbacks go
    unchecked entirely — this test's name promised more than it verified.
    """
    fake_settings = MagicMock()
    fake_settings.OPENROUTER_PROVIDER_ORDER = "DigitalOcean, Venice"
    fake_settings.OPENROUTER_ALLOW_FALLBACKS = True
    fake_settings.OPENROUTER_DATA_COLLECTION = "deny"

    client = MagicMock()
    client.chat.completions.create.return_value = _fake_llm_response("ok")
    with patch("app.services.report_llm.get_settings", return_value=fake_settings):
        rl._call_llm(client, "m", "sys", "user")

    kwargs = client.chat.completions.create.call_args.kwargs
    provider = kwargs["extra_body"]["provider"]
    assert provider["data_collection"] == "deny"
    assert provider["order"] == ["DigitalOcean", "Venice"]
    assert provider["allow_fallbacks"] is True
    assert "reasoning" not in kwargs.get("extra_body", {})


def test_call_llm_raises_if_data_collection_disabled_without_hard_pin() -> None:
    """PR #81 review: enforce_data_collection=False and allow_fallbacks=False
    are a required pair, enforced at runtime — a caller passing the former
    without the latter must fail loudly instead of silently reopening the
    PR #79 marketplace-fallback gap on a compliance-exempted call."""
    client = MagicMock()
    with pytest.raises(ValueError, match="allow_fallbacks=False"):
        rl._call_llm(client, "m", "sys", "user", enforce_data_collection=False)
    client.chat.completions.create.assert_not_called()


def test_call_llm_keeps_explicit_allow_fallbacks_even_when_sole_provider_key() -> None:
    """PR #81 review: an explicitly-passed allow_fallbacks must reach
    extra_body even when it would otherwise be the only key in `provider` —
    previously `if provider.keys() - {"allow_fallbacks"}` silently dropped the
    entire provider dict in that case, stripping a deliberate hard pin from
    the actual request with no error."""
    fake_settings = MagicMock()
    fake_settings.OPENROUTER_PROVIDER_ORDER = ""
    fake_settings.OPENROUTER_ALLOW_FALLBACKS = True
    fake_settings.OPENROUTER_DATA_COLLECTION = ""
    client = MagicMock()
    client.chat.completions.create.return_value = _fake_llm_response("ok")
    with patch("app.services.report_llm.get_settings", return_value=fake_settings):
        rl._call_llm(client, "m", "sys", "user", pin_provider=False, allow_fallbacks=False)

    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["extra_body"]["provider"] == {"allow_fallbacks": False}
