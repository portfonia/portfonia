"""Append-only credit balance history."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Identity,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

BUCKETS = ("cash", "gift")
REASONS = (
    "recharge",
    "invite_rebate",
    "signup_grant",
    "admin_adjustment",
    "subscription",
    "qa",
    "refund",
)
ACTOR_TYPES = ("system", "admin")


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


class CreditLedgerEntry(Base):
    __tablename__ = "credit_ledger"
    __table_args__ = (
        CheckConstraint(_in_list_sql("bucket", BUCKETS), name="bucket"),
        CheckConstraint("amount <> 0", name="amount_nonzero"),
        CheckConstraint("balance_after >= 0", name="balance_after"),
        CheckConstraint(_in_list_sql("reason", REASONS), name="reason"),
        CheckConstraint(_in_list_sql("actor_type", ACTOR_TYPES), name="actor_type"),
        UniqueConstraint("idempotency_key", "bucket"),
        Index("ix_credit_ledger_user_id_id", "user_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    bucket: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(Text)
    user_deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
