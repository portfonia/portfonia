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
from typing import Any, ClassVar

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import Text
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.operators import OperatorType
from sqlalchemy.types import TypeDecorator

from app.core.config import get_settings


class HoldingsDecryptionError(RuntimeError):
    """Raised when a stored holdings value can't be decrypted with the
    configured key(s). Never includes the token/ciphertext — only ops-facing
    context, since the message may end up in logs or an error tracker.
    """


def _build_fernet() -> MultiFernet:
    settings = get_settings()
    try:
        keys = [Fernet(settings.HOLDINGS_ENCRYPTION_KEY.get_secret_value().encode())]
        prev = settings.HOLDINGS_ENCRYPTION_KEY_PREV
        # A blank env value (HOLDINGS_ENCRYPTION_KEY_PREV=) still reaches here
        # as SecretStr(""), not None — `is not None` alone would treat it as
        # "set" and Fernet(b"") raises, taking down every encrypt/decrypt path
        # including rows encrypted only with the current key.
        if prev is not None and prev.get_secret_value():
            keys.append(Fernet(prev.get_secret_value().encode()))
    except ValueError as exc:
        raise HoldingsDecryptionError(
            "HOLDINGS_ENCRYPTION_KEY / HOLDINGS_ENCRYPTION_KEY_PREV is not a "
            "well-formed Fernet key — check the deployed .env value."
        ) from exc
    return MultiFernet(keys)


def encrypt_value(plaintext: str) -> str:
    """Encrypt a plaintext string, returning a Fernet token as str.

    Exposed as a standalone function (not just via the TypeDecorators below)
    so the Alembic data migration can encrypt existing rows with the exact
    same key-loading logic, rather than reimplementing it.
    """
    return _build_fernet().encrypt(plaintext.encode()).decode()


def decrypt_value(token: str) -> str:
    try:
        return _build_fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        # Common causes: wrong HOLDINGS_ENCRYPTION_KEY for this environment,
        # this migration (379fdb627ee8) not applied yet (value is still
        # plaintext, not a Fernet token), or a downgrade that left plaintext
        # in the DB while the app still runs the encrypted TypeDecorator.
        raise HoldingsDecryptionError(
            "Failed to decrypt a holdings value — the configured key doesn't "
            "match, or migration 379fdb627ee8 isn't applied against this "
            "database. Never logs the token itself."
        ) from exc


class _NoEqualityComparator(TypeDecorator.Comparator):  # type: ignore[type-arg]
    """Blocks SQL-level equality/comparison on encrypted columns.

    Fernet encrypts with a fresh random IV every call, so two encryptions of
    the same plaintext never produce the same ciphertext — a SQL `==`,
    `.in_()`, `.like()`, etc. against an encrypted column silently matches
    zero rows instead of erroring. Every current query only needs
    `.is_(None)`/`.isnot(None)` (NULL-ness, not value), which this keeps
    working since NULL never gets encrypted (see the TypeDecorator
    docstrings below) — everything else must fetch rows and filter/sort in
    Python, as `sorted_holdings()` in `app/services/holding_ordering.py`
    already does.

    `col == None` / `col != None` are also let through despite `eq`/`ne` not
    being in `_ALLOWED`: SQLAlchemy's base `operate()` already rewrites these
    to `IS NULL`/`IS NOT NULL` before any SQL is emitted (verified against
    the installed SQLAlchemy — `other == (None,)` never reaches the database
    as a literal equality), so blocking them would only produce a confusing
    error for something that was never actually unsafe (PR #111 re-review).
    """

    _ALLOWED: ClassVar[set[OperatorType]] = {
        operators.is_,
        operators.isnot,
        operators.is_distinct_from,
        operators.isnot_distinct_from,
    }
    _EQUALITY_OPS: ClassVar[set[OperatorType]] = {operators.eq, operators.ne}

    def _is_none_check(self, op: OperatorType, other: tuple[Any, ...]) -> bool:
        return op in self._EQUALITY_OPS and other == (None,)

    def operate(self, op: OperatorType, *other: Any, **kwargs: Any) -> ColumnElement[Any]:
        if op not in self._ALLOWED and not self._is_none_check(op, other):
            raise NotImplementedError(
                f"{op.__name__} is not supported on encrypted columns — Fernet "
                "ciphertext changes on every encryption call, so SQL-level "
                "equality/comparison always misses stored rows. Fetch rows and "
                "filter in Python instead."
            )
        return super().operate(op, *other, **kwargs)

    def reverse_operate(self, op: OperatorType, other: Any, **kwargs: Any) -> ColumnElement[Any]:
        if op not in self._ALLOWED and not self._is_none_check(op, (other,)):
            raise NotImplementedError(
                f"{op.__name__} is not supported on encrypted columns — Fernet "
                "ciphertext changes on every encryption call, so SQL-level "
                "equality/comparison always misses stored rows. Fetch rows and "
                "filter in Python instead."
            )
        return super().reverse_operate(op, other, **kwargs)


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
    comparator_factory = _NoEqualityComparator

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
    comparator_factory = _NoEqualityComparator

    def process_bind_param(self, value: Decimal | None, dialect: object) -> str | None:
        if value is None:
            return None
        return encrypt_value(str(value))

    def process_result_value(self, value: str | None, dialect: object) -> Decimal | None:
        if value is None:
            return None
        plaintext = decrypt_value(value)
        try:
            return Decimal(plaintext)
        except InvalidOperation as exc:
            # Deliberately omit the value — it's a decrypted holdings amount,
            # not safe to put in logs/error trackers either.
            raise ValueError("Decrypted holdings value is not a valid Decimal") from exc
