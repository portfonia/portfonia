"""Field-level encryption for holdings data at rest (issue #31).

Scope decision (2026-08-09): a single system-wide key, not per-user. The
threat this protects against is disk/DB-dump theft on a production host —
independent of who is logged in — not app-server compromise (an attacker
holding valid app DB credentials can still decrypt, since decryption happens
app-side). Per-user key isolation is a materially bigger design (key
wrapping, rotation, loss-of-access recovery) with no concrete driver yet;
building it speculatively now would be the YAGNI violation CLAUDE.md warns
against. Revisit only when there's an actual multi-tenant isolation
requirement, not just because a user system exists.

Uses Fernet (AES-128-CBC + HMAC, authenticated) rather than raw AES-GCM:
Fernet manages IV/nonce generation internally, removing a whole class of
misuse (nonce reuse) that hand-rolled AEAD invites for a use case that's
field-sized values, not high-throughput streaming.

Key rotation path (no schema change needed): set HOLDINGS_ENCRYPTION_KEY to
a newly generated key and move the previous value to
HOLDINGS_ENCRYPTION_KEY_PREV. MultiFernet always encrypts with the first
(current) key and tries every configured key on decrypt, so already-stored
ciphertext keeps decrypting during the rotation window. Drop
HOLDINGS_ENCRYPTION_KEY_PREV once a re-encryption pass has touched every row
(out of scope here — no such pass exists yet; rotation today only protects
reads, not a bulk re-encrypt).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from cryptography.fernet import Fernet, MultiFernet
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.core.config import get_settings


def _build_fernet() -> MultiFernet:
    settings = get_settings()
    keys = [Fernet(settings.HOLDINGS_ENCRYPTION_KEY.get_secret_value().encode())]
    if settings.HOLDINGS_ENCRYPTION_KEY_PREV is not None:
        keys.append(Fernet(settings.HOLDINGS_ENCRYPTION_KEY_PREV.get_secret_value().encode()))
    return MultiFernet(keys)


def encrypt_value(plaintext: str) -> str:
    """Encrypt a plaintext string, returning a Fernet token as str.

    Exposed as a standalone function (not just via the TypeDecorators below)
    so the Alembic data migration can encrypt existing rows with the exact
    same key-loading logic, rather than reimplementing it.
    """
    return _build_fernet().encrypt(plaintext.encode()).decode()


def decrypt_value(token: str) -> str:
    return _build_fernet().decrypt(token.encode()).decode()


class EncryptedString(TypeDecorator[str]):
    """Text column encrypted at rest; transparent str in Python.

    NULL passes through unencrypted — a NULL holding field means "not
    provided", not sensitive data, and encrypting it would break every
    existing ``.is_not(None)`` / ``.isnot(None)`` filter in the codebase
    (price_fetcher.py, price_capture.py, price_anomaly_detector.py,
    fund_nav_fetcher.py, window_data.py all filter Holding.ticker /
    Holding.fund_code this way — NULL-ness must stay visible at the SQL
    level even though the value itself does not).
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: object) -> str | None:
        if value is None:
            return None
        return encrypt_value(value)

    def process_result_value(self, value: str | None, dialect: object) -> str | None:
        if value is None:
            return None
        return decrypt_value(value)


class EncryptedDecimal(TypeDecorator[Decimal]):
    """Numeric column encrypted at rest; transparent Decimal in Python.

    Stored as ciphertext over the decimal's str() form. Same NULL-passthrough
    rationale as EncryptedString.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: object) -> str | None:
        if value is None:
            return None
        return encrypt_value(str(value))

    def process_result_value(self, value: str | None, dialect: object) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(decrypt_value(value))
        except InvalidOperation as exc:
            raise ValueError(f"Decrypted value is not a valid Decimal: {value!r}") from exc
