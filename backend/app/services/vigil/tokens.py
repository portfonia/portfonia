"""Link tokens and public-action nonces (issue #458, Vigil R0 P2.3).

Link tokens are 32 random bytes, base64url 43 chars; only SHA-256 hex is
stored. Public nonces are HMAC-signed with the notification-family HKDF
subkey (`vigil-public-nonce-v1`) and consumed by inserting `jti` into
`vigil_consumed_nonces` in the same transaction as the action.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.vigil import VigilActionToken, VigilConsumedNonce
from app.services.vigil.crypto import (
    HKDF_PUBLIC_NONCE_INFO,
    VigilCryptoError,
    derive_notification_subkeys,
)

LINK_TOKEN_BYTES = 32
DRILL_TTL = timedelta(hours=48)
NONCE_TTL = timedelta(minutes=3)
_NONCE_VERSION = 1


class VigilNonceError(ValueError):
    """Expired, malformed, wrong-purpose, or already-consumed nonce."""


def db_now(session: Session) -> datetime:
    """PostgreSQL `clock_timestamp()` after the caller has taken locks."""
    now = session.scalar(select(func.clock_timestamp()))
    if now is None:
        raise RuntimeError("clock_timestamp() returned null")
    current = cast(datetime, now)
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def hash_link_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def mint_link_token() -> tuple[str, str]:
    """Return (presented token, sha256 hex). Never persist the presented form."""
    raw = secrets.token_bytes(LINK_TOKEN_BYTES)
    token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return token, hash_link_token(token)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def rfc3339_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_rfc3339_z(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class SignedNonce:
    jti: UUID
    token_hash: str
    action: str
    object_id: UUID
    expires_at: datetime
    compact: str


def mint_signed_nonce(
    *, token_hash: str, action: str, object_id: UUID, now: datetime
) -> SignedNonce:
    expires_at = now + NONCE_TTL
    jti = uuid.uuid4()
    payload = {
        "v": _NONCE_VERSION,
        "jti": str(jti),
        "token_hash": token_hash,
        "action": action,
        "object_id": str(object_id),
        "exp": rfc3339_z(expires_at),
    }
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    subkey = derive_notification_subkeys(HKDF_PUBLIC_NONCE_INFO)[0]
    signature = hmac.new(subkey, canonical.encode("utf-8"), hashlib.sha256).digest()
    compact = f"{_b64url(canonical.encode('utf-8'))}.{_b64url(signature)}"
    return SignedNonce(
        jti=jti,
        token_hash=token_hash,
        action=action,
        object_id=object_id,
        expires_at=expires_at,
        compact=compact,
    )


def verify_signed_nonce(
    compact: str, *, token_hash: str, action: str, object_id: UUID, now: datetime
) -> SignedNonce:
    try:
        payload_b64, sig_b64 = compact.split(".", 1)
        canonical = _b64url_decode(payload_b64)
        signature = _b64url_decode(sig_b64)
    except (ValueError, TypeError) as exc:
        raise VigilNonceError("malformed nonce") from exc

    matched = False
    try:
        subkeys = derive_notification_subkeys(HKDF_PUBLIC_NONCE_INFO)
    except VigilCryptoError as exc:
        raise VigilNonceError("nonce crypto unavailable") from exc
    for subkey in subkeys:
        expected = hmac.new(subkey, canonical, hashlib.sha256).digest()
        if hmac.compare_digest(expected, signature):
            matched = True
            break
    if not matched:
        raise VigilNonceError("nonce signature mismatch")

    try:
        payload = json.loads(canonical.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VigilNonceError("malformed nonce") from exc
    if not isinstance(payload, dict) or payload.get("v") != _NONCE_VERSION:
        raise VigilNonceError("malformed nonce")
    if payload.get("token_hash") != token_hash:
        raise VigilNonceError("nonce token mismatch")
    if payload.get("action") != action:
        raise VigilNonceError("wrong-purpose nonce")
    if payload.get("object_id") != str(object_id):
        raise VigilNonceError("nonce object mismatch")
    try:
        jti = UUID(str(payload["jti"]))
        expires_at = _parse_rfc3339_z(str(payload["exp"]))
    except (KeyError, ValueError) as exc:
        raise VigilNonceError("malformed nonce") from exc
    if now >= expires_at:
        raise VigilNonceError("expired nonce")
    return SignedNonce(
        jti=jti,
        token_hash=token_hash,
        action=action,
        object_id=object_id,
        expires_at=expires_at,
        compact=compact,
    )


def consume_nonce(session: Session, nonce: SignedNonce, *, now: datetime) -> None:
    """Insert jti. Duplicate PK is already-consumed, not a boolean check."""
    session.add(
        VigilConsumedNonce(
            jti=nonce.jti,
            token_hash=nonce.token_hash,
            action=nonce.action,
            expires_at=nonce.expires_at,
            used_at=now,
        )
    )
    try:
        session.flush()
    except IntegrityError as exc:
        raise VigilNonceError("nonce already consumed") from exc


def invalidate_open_drill_tokens(
    session: Session,
    *,
    vault_id: UUID,
    now: datetime,
    config_id: UUID | None = None,
    object_id: UUID | None = None,
) -> list[UUID]:
    """Invalidate pending drill tokens. Caller holds User-then-vault locks."""
    stmt = select(VigilActionToken).where(
        VigilActionToken.vault_id == vault_id,
        VigilActionToken.purpose == "drill",
        VigilActionToken.confirmed_at.is_(None),
        VigilActionToken.used_at.is_(None),
        VigilActionToken.invalidated_at.is_(None),
    )
    if config_id is not None:
        stmt = stmt.where(VigilActionToken.config_id == config_id)
    if object_id is not None:
        stmt = stmt.where(VigilActionToken.object_id == object_id)
    rows = list(session.scalars(stmt.order_by(VigilActionToken.id).with_for_update()).all())
    ids: list[UUID] = []
    for row in rows:
        row.invalidated_at = now
        ids.append(row.id)
    if rows:
        session.flush()
    return ids
