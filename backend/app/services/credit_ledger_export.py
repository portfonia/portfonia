"""In-memory CSV exports for the credit read path."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.credit_ledger import CreditLedgerEntry
from app.models.user import User


def _amount(value: Decimal) -> str:
    return f"{value:.2f}"


def _timestamp(value: datetime | None) -> str:
    return value.astimezone(ET).isoformat() if value is not None else ""


def build_ledger_csv(session: Session) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        (
            "id",
            "created_at",
            "user_id",
            "email",
            "bucket",
            "amount",
            "balance_after",
            "reason",
            "actor_type",
            "actor_id",
            "idempotency_key",
            "reference",
            "note",
            "user_deleted_at",
        )
    )
    rows = session.execute(
        select(CreditLedgerEntry, User.email)
        .outerjoin(User, CreditLedgerEntry.user_id == User.id)
        .order_by(CreditLedgerEntry.id)
    )
    for entry, email in rows:
        writer.writerow(
            (
                entry.id,
                _timestamp(entry.created_at),
                entry.user_id,
                email or "",
                entry.bucket,
                _amount(entry.amount),
                _amount(entry.balance_after),
                entry.reason,
                entry.actor_type,
                entry.actor_id or "",
                entry.idempotency_key,
                entry.reference or "",
                entry.note or "",
                _timestamp(entry.user_deleted_at),
            )
        )
    return output.getvalue()


def build_balances_csv(session: Session) -> str:
    sums = (
        select(
            CreditLedgerEntry.user_id.label("user_id"),
            func.sum(
                case((CreditLedgerEntry.bucket == "cash", CreditLedgerEntry.amount), else_=0)
            ).label("cash_sum"),
            func.sum(
                case((CreditLedgerEntry.bucket == "gift", CreditLedgerEntry.amount), else_=0)
            ).label("gift_sum"),
        )
        .group_by(CreditLedgerEntry.user_id)
        .subquery()
    )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        (
            "user_id",
            "email",
            "status",
            "cash_balance",
            "gift_balance",
            "total_balance",
            "ledger_cash_sum",
            "ledger_gift_sum",
            "consistent",
        )
    )
    rows = session.execute(
        select(User, func.coalesce(sums.c.cash_sum, 0), func.coalesce(sums.c.gift_sum, 0))
        .outerjoin(sums, User.id == sums.c.user_id)
        .order_by(User.email)
    )
    for user, cash_sum, gift_sum in rows:
        writer.writerow(
            (
                user.id,
                user.email,
                user.status,
                _amount(user.credit_cash_balance),
                _amount(user.credit_gift_balance),
                _amount(user.credit_cash_balance + user.credit_gift_balance),
                _amount(cash_sum),
                _amount(gift_sum),
                str(
                    user.credit_cash_balance == cash_sum and user.credit_gift_balance == gift_sum
                ).lower(),
            )
        )
    return output.getvalue()
