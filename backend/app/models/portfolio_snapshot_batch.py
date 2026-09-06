"""Per-(user, day) batch-completeness marker for the Portfolio Performance
snapshot pipeline (issue #360 Phase 1).

`GET /portfolio/performance` must never read a day whose price/FX
dependencies had not finished capturing when the daily snapshot task ran —
a half-complete day would silently understate that day's value. This table
is the single source of truth `app/services/portfolio_history.py` writes
and `app/services/portfolio_performance.py` reads to decide which days are
safe to expose.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, ForeignKey, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

BATCH_STATUS_VALUES = ("pending", "complete", "skipped_deps")


class PortfolioSnapshotBatch(Base):
    __tablename__ = "portfolio_snapshot_batches"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "snapshot_date", name="uq_portfolio_snapshot_batches_user_date"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # CASCADE — same rationale as PortfolioValueSnapshot.user_id (derived
    # data, no separate service-layer purge step needed).
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
