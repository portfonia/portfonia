"""HMAC-signed unsubscribe tokens (issue #257, design doc §3.7).

Stateless: create/verify round-trip against APP_SECRET_KEY, no DB.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.services.unsubscribe_token import create_token, verify_token

_UID = uuid.UUID("00000000-0000-0000-0000-0000000000e7")
_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def test_round_trip_returns_signed_fields() -> None:
    token = create_token(
        user_id=_UID,
        purpose="delivery_email",
        email="reports@example.com",
        now=_NOW,
    )
    claims = verify_token(token, now=_NOW)

    assert claims is not None
    assert claims.user_id == _UID
    assert claims.purpose == "delivery_email"
    assert claims.email == "reports@example.com"
    assert claims.expires_at == _NOW + timedelta(days=7)


def test_account_email_purpose_round_trips() -> None:
    token = create_token(user_id=_UID, purpose="account_email", email="acct@example.com", now=_NOW)
    claims = verify_token(token, now=_NOW)
    assert claims is not None
    assert claims.purpose == "account_email"


def test_tampered_token_is_rejected() -> None:
    token = create_token(user_id=_UID, purpose="account_email", email="a@example.com", now=_NOW)
    # Flip a character in the middle so we don't just hit padding issues.
    mid = len(token) // 2
    flipped = token[:mid] + ("A" if token[mid] != "A" else "B") + token[mid + 1 :]

    assert verify_token(flipped, now=_NOW) is None


def test_expired_token_is_rejected() -> None:
    token = create_token(user_id=_UID, purpose="account_email", email="a@example.com", now=_NOW)
    assert verify_token(token, now=_NOW + timedelta(days=7, seconds=1)) is None


def test_token_valid_at_exact_expiry_instant() -> None:
    """Expiry is exclusive of the instant after expires_at; equal is still valid."""
    token = create_token(user_id=_UID, purpose="account_email", email="a@example.com", now=_NOW)
    assert verify_token(token, now=_NOW + timedelta(days=7)) is not None


def test_garbage_and_empty_tokens_return_none() -> None:
    assert verify_token("", now=_NOW) is None
    assert verify_token("not-a-token", now=_NOW) is None
    assert verify_token("%%%", now=_NOW) is None
