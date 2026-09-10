"""Durable per-(user, day) capture outbox (issue #373, Scenario 2).

The daily capture freezes the intended per-holding payload here *before* it
applies that payload to `portfolio_value_snapshots` +
`portfolio_snapshot_batches`. A day that was computed but never published is
therefore recoverable by replaying this payload, instead of recomputing the
composition from *today's* holdings — which is the mini composition-replay
`Hermes/Portfonia/Docs/Portfolio_Data_Retention.md` §2.4 rules out once the
book has moved.

Lifecycle: `computed` (payload frozen, not yet published) -> `applied` (live
rows and the `complete` batch committed in one transaction with this
transition). `failed` means the payload can no longer be trusted (checksum or
decrypt mismatch) — recovery skips such a row and logs instead of retrying a
corrupt payload forever.

`payload` is Fernet-encrypted (same key as the live rows): it carries the same
denormalized `ticker` / `broker` / `account` / `portfolio` fields those rows
encrypt individually, so storing it in clear text would weaken the at-rest
posture a DB dump sees.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, ForeignKey, Index, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encryption import EncryptedString
from app.models.base import Base

OUTBOX_STATUS_VALUES = ("computed", "applied", "failed")


class PortfolioSnapshotOutbox(Base):
    __tablename__ = "portfolio_snapshot_outbox"
    __table_args__ = (
        UniqueConstraint("user_id", "snapshot_date", name="uq_portfolio_snapshot_outbox_user_date"),
        # Recovery scans by status over a date window; the unique key above
        # only serves point lookups.
        Index("ix_portfolio_snapshot_outbox_status_date", "status", "snapshot_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # CASCADE — derived capture evidence, same rationale as
    # PortfolioValueSnapshot.user_id: a user purge must not be blocked by it.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'computed'"))
    # JSON list of frozen row dicts, one per holding (see
    # app/services/snapshot_outbox.py for the codec).
    payload: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    checksum: Mapped[str] = mapped_column(Text, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    applied_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
