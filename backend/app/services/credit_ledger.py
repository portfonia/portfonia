"""The sole writer for credit balances and ledger rows."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.credit_ledger import ACTOR_TYPES, BUCKETS, CreditLedgerEntry
from app.models.user import User
from app.services.invites import _normalize_email

CONSUMPTION_REASONS = ("subscription", "qa")
_REASON_RULES = {
    "recharge": ({"cash"}, "+"),
    "invite_rebate": ({"cash"}, "+"),
    "signup_grant": ({"gift"}, "+"),
    "admin_adjustment": ({"gift"}, "±"),
    "subscription": ({"cash", "gift"}, "-"),
    "qa": ({"cash", "gift"}, "-"),
}


class InsufficientCredits(Exception):
    """The requested debit would make a balance negative."""


class IdempotencyConflict(Exception):
    """A key was already used for another operation."""


@dataclass(frozen=True)
class LedgerWrite:
    entries: list[CreditLedgerEntry]
    replayed: bool


def _lock_user(session: Session, user_id: uuid.UUID) -> User:
    user = session.execute(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if user is None:
        raise LookupError(f"user {user_id} not found")
    return user


def _rows_for_key(session: Session, key: str) -> list[CreditLedgerEntry]:
    return list(
        session.execute(
            select(CreditLedgerEntry)
            .where(CreditLedgerEntry.idempotency_key == key)
            .order_by(CreditLedgerEntry.id)
        ).scalars()
    )


def _valid_amount(amount: Decimal) -> bool:
    exponent = amount.as_tuple().exponent
    return (
        amount.is_finite()
        and amount != 0
        and isinstance(exponent, int)
        and exponent >= -2
        and abs(amount) < Decimal("10000000000")
    )


def _post(
    session: Session,
    user: User,
    *,
    bucket: str,
    amount: Decimal,
    reason: str,
    actor_type: str,
    idempotency_key: str,
    note: str | None = None,
    reference: str | None = None,
) -> CreditLedgerEntry:
    if (
        not _valid_amount(amount)
        or reason not in _REASON_RULES
        or bucket not in BUCKETS
        or actor_type not in ACTOR_TYPES
    ):
        raise ValueError("invalid credit ledger write")
    allowed_buckets, sign = _REASON_RULES[reason]
    if (
        bucket not in allowed_buckets
        or (sign == "+" and amount < 0)
        or (sign == "-" and amount > 0)
    ):
        raise ValueError("invalid bucket or sign for reason")
    column = f"credit_{bucket}_balance"
    balance_after = getattr(user, column) + amount
    if balance_after < 0:
        raise InsufficientCredits
    setattr(user, column, balance_after)
    entry = CreditLedgerEntry(
        user_id=user.id,
        bucket=bucket,
        amount=amount,
        balance_after=balance_after,
        reason=reason,
        actor_type=actor_type,
        idempotency_key=idempotency_key,
        note=note,
        reference=reference,
    )
    session.add(entry)
    session.flush()
    return entry


def signup_grant_key(email: str) -> str:
    normalized = _normalize_email(email)
    if normalized is None:
        raise ValueError("email must not be blank")
    return "signup_grant:" + hashlib.sha256(normalized.encode()).hexdigest()


def grant_signup_credits(
    session: Session, user: User, *, note: str | None = None
) -> CreditLedgerEntry | None:
    amount = get_settings().SIGNUP_GRANT_CREDITS
    if amount == 0:
        return None
    locked = _lock_user(session, user.id)
    key = signup_grant_key(user.email)
    if _rows_for_key(session, key):
        return None
    return _post(
        session,
        locked,
        bucket="gift",
        amount=amount,
        reason="signup_grant",
        actor_type="system",
        idempotency_key=key,
        note=note,
    )


def adjust_by_admin(
    session: Session,
    *,
    user_id: uuid.UUID,
    amount: Decimal,
    note: str,
    idempotency_key: str,
    reference: str | None = None,
) -> LedgerWrite:
    stored_key = "admin_adjustment:" + idempotency_key
    user = _lock_user(session, user_id)
    existing = _rows_for_key(session, stored_key)
    if existing:
        if (
            any(row.user_id != user_id or row.reason != "admin_adjustment" for row in existing)
            or sum((row.amount for row in existing), Decimal("0")) != amount
        ):
            raise IdempotencyConflict
        return LedgerWrite(existing, replayed=True)
    entry = _post(
        session,
        user,
        bucket="gift",
        amount=amount,
        reason="admin_adjustment",
        actor_type="admin",
        idempotency_key=stored_key,
        note=note,
        reference=reference,
    )
    return LedgerWrite([entry], replayed=False)


def consume_credits(
    session: Session,
    *,
    user_id: uuid.UUID,
    amount: Decimal,
    reason: str,
    idempotency_key: str,
    reference: str | None = None,
    note: str | None = None,
) -> LedgerWrite:
    if amount <= 0 or not _valid_amount(amount) or reason not in CONSUMPTION_REASONS:
        raise ValueError("invalid consumption")
    user = _lock_user(session, user_id)
    existing = _rows_for_key(session, idempotency_key)
    if existing:
        if (
            any(row.user_id != user_id or row.reason != reason for row in existing)
            or sum((row.amount for row in existing), Decimal("0")) != -amount
        ):
            raise IdempotencyConflict
        return LedgerWrite(existing, replayed=True)
    if user.credit_gift_balance + user.credit_cash_balance < amount:
        raise InsufficientCredits
    gift_part = min(user.credit_gift_balance, amount)
    cash_part = amount - gift_part
    entries = []
    if gift_part > 0:
        entries.append(
            _post(
                session,
                user,
                bucket="gift",
                amount=-gift_part,
                reason=reason,
                actor_type="system",
                idempotency_key=idempotency_key,
                note=note,
                reference=reference,
            )
        )
    if cash_part > 0:
        entries.append(
            _post(
                session,
                user,
                bucket="cash",
                amount=-cash_part,
                reason=reason,
                actor_type="system",
                idempotency_key=idempotency_key,
                note=note,
                reference=reference,
            )
        )
    return LedgerWrite(entries, replayed=False)
