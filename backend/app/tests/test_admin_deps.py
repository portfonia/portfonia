"""Unit tests for the B2 ops API auth mechanism (issue #129 Ring 1 stage B).

`require_ops_token` (app/core/deps.py) is deliberately independent of
`current_principal`/the user auth system — see Ring 1-B design.md §4.3. These
tests exercise it directly (no TestClient, no DB) since it has no dependency
on either. Router-level wiring (every /admin/* route actually requiring it)
is covered separately in test_admin_router.py.
"""

from __future__ import annotations

import secrets as secrets_module
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError

from app.core.config import get_settings
from app.core.deps import require_ops_token


def _primary_token() -> str:
    return get_settings().ADMIN_API_TOKEN.get_secret_value()


def test_missing_authorization_header_rejected() -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_ops_token(authorization=None)
    assert exc_info.value.status_code == 401


def test_non_bearer_scheme_rejected() -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_ops_token(authorization=f"Basic {_primary_token()}")
    assert exc_info.value.status_code == 401


def test_bearer_with_no_token_rejected() -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_ops_token(authorization="Bearer ")
    assert exc_info.value.status_code == 401


def test_wrong_token_rejected() -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_ops_token(authorization="Bearer not-the-real-token")
    assert exc_info.value.status_code == 401


def test_correct_primary_token_accepted() -> None:
    require_ops_token(authorization=f"Bearer {_primary_token()}")  # must not raise


def test_prev_token_accepted_during_rotation_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rotation window: the old value moves to _PREV and must still work,
    mirroring HOLDINGS_ENCRYPTION_KEY/_PREV's MultiFernet behavior."""
    settings = get_settings()
    old_token = _primary_token()
    monkeypatch.setattr(settings, "ADMIN_API_TOKEN", SecretStr("brand-new-current-token"))
    monkeypatch.setattr(settings, "ADMIN_API_TOKEN_PREV", SecretStr(old_token))

    require_ops_token(authorization=f"Bearer {old_token}")  # must not raise
    require_ops_token(authorization="Bearer brand-new-current-token")  # must not raise


def test_unset_prev_token_never_matches_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank/unset _PREV must not be treated as a valid empty-string credential."""
    settings = get_settings()
    monkeypatch.setattr(settings, "ADMIN_API_TOKEN_PREV", None)
    with pytest.raises(HTTPException) as exc_info:
        require_ops_token(authorization="Bearer ")
    assert exc_info.value.status_code == 401


def test_comparison_uses_compare_digest_not_eq(monkeypatch: pytest.MonkeyPatch) -> None:
    """Locks the timing-safe comparison — a plain `==` must never be reintroduced."""
    spy = MagicMock(wraps=secrets_module.compare_digest)
    monkeypatch.setattr("app.core.deps.secrets.compare_digest", spy)

    require_ops_token(authorization=f"Bearer {_primary_token()}")

    assert spy.called


def test_non_ascii_bearer_token_rejected_not_raised() -> None:
    """`secrets.compare_digest` raises TypeError on non-ASCII str pairs — a
    crafted header must not turn into an unhandled exception (PR #177 review
    round 2: reproduced as a real 500 through the full TestClient stack,
    which also reset the consecutive-401 anti-flood counter)."""
    with pytest.raises(HTTPException) as exc_info:
        require_ops_token(authorization="Bearer café")
    assert exc_info.value.status_code == 401


def test_every_candidate_compared_no_short_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    """`any(...)` on a generator short-circuits at the first match — during a
    rotation window that means matching the primary token costs one
    `compare_digest` call while matching `_PREV` (or missing entirely) costs
    two, a timing side-channel revealing which candidate matched (PR #177
    review round 2, reproduced by counting real calls)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "ADMIN_API_TOKEN_PREV", SecretStr("some-other-token"))

    spy = MagicMock(wraps=secrets_module.compare_digest)
    monkeypatch.setattr("app.core.deps.secrets.compare_digest", spy)

    require_ops_token(authorization=f"Bearer {_primary_token()}")

    assert spy.call_count == 2


def test_admin_api_token_blank_fails_at_settings_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """The required token has no unset state — blank must fail at boot, not first use
    (same discipline as HOLDINGS_ENCRYPTION_KEY, PR #111 re-review)."""
    monkeypatch.setenv("ADMIN_API_TOKEN", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_admin_api_token_prev_blank_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unlike the primary token, a blank _PREV means "not rotating", not a misconfiguration."""
    monkeypatch.setenv("ADMIN_API_TOKEN_PREV", "")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.ADMIN_API_TOKEN_PREV is None or (
            settings.ADMIN_API_TOKEN_PREV.get_secret_value() == ""
        )
        # Either representation must fail to authenticate an empty credential.
        with pytest.raises(HTTPException):
            require_ops_token(authorization="Bearer ")
    finally:
        get_settings.cache_clear()


def test_admin_api_token_strips_leading_trailing_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stray space in .env must not silently produce a token that never
    matches a real client's Authorization header (PR #177 review round 2)."""
    monkeypatch.setenv("ADMIN_API_TOKEN", "  padded-token  ")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.ADMIN_API_TOKEN.get_secret_value() == "padded-token"
    finally:
        get_settings.cache_clear()


def test_admin_api_token_whitespace_only_fails_at_settings_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whitespace-only is just as blank as empty-string for the required token."""
    monkeypatch.setenv("ADMIN_API_TOKEN", "   ")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_admin_api_token_prev_whitespace_only_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitespace-only _PREV must not become a live `" "`-matching credential
    (PR #177 review round 2: `_bearer_token("Bearer  ")` returns `" "`, which
    would otherwise authenticate against a whitespace-only _PREV)."""
    monkeypatch.setenv("ADMIN_API_TOKEN_PREV", "   ")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.ADMIN_API_TOKEN_PREV is None
        with pytest.raises(HTTPException):
            require_ops_token(authorization="Bearer  ")
    finally:
        get_settings.cache_clear()
