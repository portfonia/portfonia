"""Per-holding daily value snapshot for the Portfolio Performance chart
(issue #360 Phase 1).

Deliberately denormalized and NOT FK-joined to the live `holdings` row
(design §3.1, decisions comment on #360): a later edit/delete on a holding
must never corrupt the readability of historical snapshots. `holding_id` is
a soft, nullable reference (no FK) used only for day-to-day quantity
alignment when computing approximate TWR (D3 amendment) — never queried by
JOIN against `holdings`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, ForeignKey, Numeric, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encryption import EncryptedDecimal, EncryptedString
from app.models.base import Base

# Closed set mirroring app/services/portfolio_history.py's DataQuality
# semantics — kept as plain strings here (not a DB CHECK constraint) since
# this table has no ORM-level domain-validation precedent to mirror and the
# writer is the only producer of these values (issue #360 Phase 1 design
# review call: holdings.py's CHECK-constraint pattern exists because user
# input reaches those columns directly; every row here is server-computed).
DATA_QUALITY_VALUES = ("ok", "approx_backfill", "approx_fx", "insufficient")


class PortfolioValueSnapshot(Base):
    __tablename__ = "portfolio_value_snapshots"
    __table_args__ = (
        # Idempotency key: one row per holding per user per day. `holding_id`
        # is nullable, and Postgres treats NULL as distinct in a UNIQUE
        # constraint — every writer in this codebase always populates it
        # (issue #360 Phase 1 scope: only the daily task and the backfill
        # script write here, both starting from a live or once-live Holding
        # row), so a NULL-holding_id row is not a real write path today.
        # A DB-level fallback unique key on the denormalized identity columns
        # (ticker/fund_code/broker/account/portfolio) was considered and
        # dropped: those columns are Fernet-encrypted (issue #31), and
        # ciphertext is different on every encryption call — a UNIQUE
        # constraint over them can never actually detect a duplicate at the
        # SQL level (same reason EncryptedString blocks SQL-level equality
        # entirely, see app/core/encryption.py).
        UniqueConstraint(
            "user_id", "snapshot_date", "holding_id", name="uq_portfolio_value_snapshots_holding"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # CASCADE (not RESTRICT like holdings/reports/etc. — issue #129 B7): this
    # is derived historical time-series data, not an audited financial
    # record. Design decision "Cascade-delete on user purge" — a user purge
    # must not be blocked by their own performance history existing, and no
    # separate service-layer delete step is needed (app/services/user_purge.py
    # is intentionally left untouched by this migration).
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Soft reference — NOT a ForeignKey (see module docstring).
    holding_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    # Denormalized snapshot-time labels (issue #360 D8) — encrypted at rest,
    # same columns/encryption choice as Holding (issue #31).
    ticker: Mapped[str | None] = mapped_column(EncryptedString)
    fund_code: Mapped[str | None] = mapped_column(EncryptedString)
    market: Mapped[str | None] = mapped_column(Text)
    broker: Mapped[str | None] = mapped_column(EncryptedString)
    account: Mapped[str | None] = mapped_column(EncryptedString)
    portfolio: Mapped[str | None] = mapped_column(EncryptedString)

    asset_class: Mapped[str | None] = mapped_column(Text)
    pricing_mode: Mapped[str | None] = mapped_column(Text)
    capture_supported: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    currency: Mapped[str] = mapped_column(Text, nullable=False)

    # Listed/auto quantity when applicable; null for cash/wmf/manual rows
    # (D3/D5 amendments — used for day-to-day qty alignment in the
    # approximate TWR calc, see app/services/portfolio_history.py).
    shares: Mapped[Decimal | None] = mapped_column(EncryptedDecimal)
    # Local-currency value for cash/wmf/manual rows; null when N/A.
    current_value: Mapped[Decimal | None] = mapped_column(EncryptedDecimal)

    market_value: Mapped[Decimal | None] = mapped_column(Numeric)
    market_value_base: Mapped[Decimal | None] = mapped_column(Numeric)
    cost_basis_base: Mapped[Decimal | None] = mapped_column(Numeric)

    fx_rate_used: Mapped[Decimal | None] = mapped_column(Numeric)
    price_as_of: Mapped[date | None] = mapped_column(Date)
    fx_as_of: Mapped[date | None] = mapped_column(Date)

    is_backfilled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    is_fx_fallback: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    data_quality: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'ok'"))

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
