"""Dedicated Vigil contextual Fernet — data key (issue #454, P2.1) and
notification key (issue #456, P3.1).

Deliberately NOT app.core.encryption.encrypt_value: that helper always
selects HOLDINGS_ENCRYPTION_KEY and has no concept of a bound context. E1
requires fully independent key families, and Vigil's Fernet payloads must
reject a ciphertext copied into a different row/table/vault (Fernet itself
has no AAD parameter — this module's JSON envelope substitutes for one). See
Vigil Concept & Design.md appendix C.2 for the full reasoning: same
technique (Fernet + current/PREV via MultiFernet, TypeDecorator-style field
wrapping) as holdings encryption, but separate keys and a separate module so
the two systems' failure domains never overlap and neither implementation
change can silently affect the other.

Two independent key families, both random Fernet keys, neither derived from
the other: VIGIL_ENCRYPTION_KEY/_PREV ("data", persistent metadata/envelope
— `encrypt_field`/`decrypt_field`) and VIGIL_NOTIFICATION_KEY/_PREV
("notification", the short-lived mail outbox payload — #456's
`encrypt_notification_field`/`decrypt_notification_field`). A leaked data
key must not expose a pending mail body/token, and vice versa.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from pydantic import SecretStr

from app.core.config import get_settings

_ENVELOPE_VERSION = 1
_ENVELOPE_KEYS = frozenset({"version", "purpose", "table", "row_id", "vault_id", "value"})


class VigilCryptoError(RuntimeError):
    """Raised for a missing/malformed Vigil key, an undecryptable token, an
    unrecognized/malformed envelope, or a context mismatch (wrong
    purpose/table/row_id/vault_id) between the stored ciphertext and the
    caller's expected identity. Never includes the token or decrypted
    value — this may end up in logs or an error tracker.
    """


def _build_fernet_for(
    key: SecretStr | None, prev: SecretStr | None, *, key_name: str
) -> MultiFernet:
    if key is None or not key.get_secret_value():
        raise VigilCryptoError(f"{key_name} is not configured — Vigil crypto is unavailable.")
    try:
        keys = [Fernet(key.get_secret_value().encode())]
        # A blank env value still reaches here as SecretStr(""), not None —
        # `is not None` alone would treat it as "set" and Fernet(b"") raises,
        # taking down every encrypt/decrypt path (mirrors
        # app.core.encryption._build_fernet's HOLDINGS_ENCRYPTION_KEY_PREV
        # handling).
        if prev is not None and prev.get_secret_value():
            keys.append(Fernet(prev.get_secret_value().encode()))
    except ValueError as exc:
        raise VigilCryptoError(
            f"{key_name} / {key_name}_PREV is not a well-formed Fernet key — "
            "check the deployed .env value."
        ) from exc
    return MultiFernet(keys)


def _build_fernet() -> MultiFernet:
    settings = get_settings()
    return _build_fernet_for(
        settings.VIGIL_ENCRYPTION_KEY,
        settings.VIGIL_ENCRYPTION_KEY_PREV,
        key_name="VIGIL_ENCRYPTION_KEY",
    )


def _build_notification_fernet() -> MultiFernet:
    settings = get_settings()
    return _build_fernet_for(
        settings.VIGIL_NOTIFICATION_KEY,
        settings.VIGIL_NOTIFICATION_KEY_PREV,
        key_name="VIGIL_NOTIFICATION_KEY",
    )


def _encrypt_envelope(envelope: dict[str, Any], fernet: MultiFernet) -> str:
    payload = json.dumps(envelope, separators=(",", ":"), sort_keys=True)
    return fernet.encrypt(payload.encode()).decode()


def _decrypt_envelope(token: str, fernet: MultiFernet) -> dict[str, Any]:
    try:
        raw = fernet.decrypt(token.encode())
    except InvalidToken as exc:
        raise VigilCryptoError(
            "Failed to decrypt a Vigil value — wrong key, or the ciphertext "
            "is not a Vigil envelope."
        ) from exc
    try:
        envelope = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VigilCryptoError("Vigil ciphertext decrypted to a malformed envelope.") from exc
    if not isinstance(envelope, dict):
        raise VigilCryptoError("Vigil envelope is not a JSON object.")
    return envelope


def _validate_envelope_context(
    envelope: dict[str, Any], *, purpose: str, table: str, row_id: UUID, vault_id: UUID
) -> str:
    unknown = set(envelope) - _ENVELOPE_KEYS
    if unknown:
        raise VigilCryptoError(f"Vigil envelope has unrecognized field(s): {sorted(unknown)}")
    missing = _ENVELOPE_KEYS - set(envelope)
    if missing:
        raise VigilCryptoError(f"Vigil envelope is missing field(s): {sorted(missing)}")

    if envelope["version"] != _ENVELOPE_VERSION:
        raise VigilCryptoError(f"Unsupported Vigil envelope version: {envelope['version']!r}")
    if envelope["purpose"] != purpose:
        raise VigilCryptoError("Vigil envelope purpose mismatch.")
    if envelope["table"] != table:
        raise VigilCryptoError("Vigil envelope table mismatch.")
    if envelope["row_id"] != str(row_id):
        raise VigilCryptoError("Vigil envelope row_id mismatch — possible cross-row ciphertext.")
    if envelope["vault_id"] != str(vault_id):
        raise VigilCryptoError(
            "Vigil envelope vault_id mismatch — possible cross-vault ciphertext."
        )

    value = envelope["value"]
    if not isinstance(value, str):
        raise VigilCryptoError("Vigil envelope value is not a string.")
    return value


def encrypt_field(value: str, *, purpose: str, table: str, row_id: UUID, vault_id: UUID) -> str:
    """Encrypt `value` under the data key, in a context envelope binding it
    to exactly this (purpose, table, row_id, vault_id). `value` must
    already be a string — callers JSON-encode/base64url-encode richer
    structures themselves before calling this."""
    envelope = {
        "version": _ENVELOPE_VERSION,
        "purpose": purpose,
        "table": table,
        "row_id": str(row_id),
        "vault_id": str(vault_id),
        "value": value,
    }
    return _encrypt_envelope(envelope, _build_fernet())


def decrypt_field(token: str, *, purpose: str, table: str, row_id: UUID, vault_id: UUID) -> str:
    """Decrypt `token` under the data key, then reject it unless its
    envelope's context exactly matches the caller's expected (purpose,
    table, row_id, vault_id) — this is what stops a ciphertext copied onto
    a different row from silently decrypting (A02/A04)."""
    envelope = _decrypt_envelope(token, _build_fernet())
    return _validate_envelope_context(
        envelope, purpose=purpose, table=table, row_id=row_id, vault_id=vault_id
    )


def encrypt_notification_field(
    value: str, *, purpose: str, table: str, row_id: UUID, vault_id: UUID
) -> str:
    """Same contextual envelope as `encrypt_field`, but under the
    independent notification key (#456) — used only for the short-lived
    outbox mail payload, never for persistent Vigil metadata."""
    envelope = {
        "version": _ENVELOPE_VERSION,
        "purpose": purpose,
        "table": table,
        "row_id": str(row_id),
        "vault_id": str(vault_id),
        "value": value,
    }
    return _encrypt_envelope(envelope, _build_notification_fernet())


def decrypt_notification_field(
    token: str, *, purpose: str, table: str, row_id: UUID, vault_id: UUID
) -> str:
    """Notification-key counterpart to `decrypt_field`."""
    envelope = _decrypt_envelope(token, _build_notification_fernet())
    return _validate_envelope_context(
        envelope, purpose=purpose, table=table, row_id=row_id, vault_id=vault_id
    )
