"""Stateless HMAC-signed unsubscribe tokens (issue #257, design doc §3.7).

Signed payload: ``email-unsubscribe-v1:{user_id}:{purpose}:{email}:{expires_at}``.
``expires_at`` is unix seconds, send-time + 7 days, embedded in the
signature the same way ``altcha_challenge.create_forgot_password_challenge``
embeds ``expires`` — verify recomputes the HMAC and checks expiry; no DB
row is stored for the token itself.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from app.core.config import get_settings

TOKEN_PREFIX = "email-unsubscribe-v1"
TOKEN_TTL = timedelta(days=7)
UnsubscribePurpose = Literal["account_email", "delivery_email"]
_VALID_PURPOSES = frozenset({"account_email", "delivery_email"})


@dataclass(frozen=True)
class UnsubscribeClaims:
    user_id: UUID
    purpose: UnsubscribePurpose
    email: str
    expires_at: datetime


def _hmac_key() -> bytes:
    return get_settings().APP_SECRET_KEY.get_secret_value().encode()


def _sign(payload: str) -> str:
    return hmac.new(_hmac_key(), payload.encode(), hashlib.sha256).hexdigest()


def _encode(payload: str, digest: str) -> str:
    raw = f"{payload}.{digest}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(token: str) -> tuple[str, str] | None:
    if not token:
        return None
    pad = "=" * ((4 - len(token) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(token + pad).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if "." not in raw:
        return None
    payload, digest = raw.rsplit(".", 1)
    return payload, digest


def create_token(
    *,
    user_id: UUID,
    purpose: UnsubscribePurpose,
    email: str,
    now: datetime | None = None,
) -> str:
    if purpose not in _VALID_PURPOSES:
        raise ValueError(f"unsupported unsubscribe purpose: {purpose}")
    moment = now if now is not None else datetime.now(UTC)
    expires_at = moment + TOKEN_TTL
    payload = f"{TOKEN_PREFIX}:{user_id}:{purpose}:{email}:{int(expires_at.timestamp())}"
    return _encode(payload, _sign(payload))


def verify_token(token: str, *, now: datetime | None = None) -> UnsubscribeClaims | None:
    """Return the decoded claims, or None for anything malformed, tampered,
    or expired — never raises, matching ``verify_forgot_password_solution``.

    The outer ``except Exception`` is load-bearing: Python 3.12's
    ``hmac.compare_digest`` raises ``TypeError`` when a decoded digest
    half is non-ASCII, and GET/POST /unsubscribe are unauthenticated
    (PR #279 review). HMAC is still compared before any claims parse.
    """
    try:
        return _verify_token(token, now=now)
    except Exception:
        return None


def _verify_token(token: str, *, now: datetime | None) -> UnsubscribeClaims | None:
    decoded = _decode(token)
    if decoded is None:
        return None
    payload, digest = decoded
    expected = _sign(payload)
    if not hmac.compare_digest(digest, expected):
        return None
    parts = payload.split(":")
    if len(parts) < 5 or parts[0] != TOKEN_PREFIX:
        return None
    purpose = parts[2]
    email = ":".join(parts[3:-1])
    if purpose not in _VALID_PURPOSES or not email:
        return None
    try:
        user_id = UUID(parts[1])
        expires_unix = int(parts[-1])
    except ValueError:
        return None
    expires_at = datetime.fromtimestamp(expires_unix, tz=UTC)
    moment = now if now is not None else datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    if moment > expires_at:
        return None
    return UnsubscribeClaims(
        user_id=user_id,
        purpose=purpose,  # type: ignore[arg-type]
        email=email,
        expires_at=expires_at,
    )
