from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encryption import EncryptedDecimal, EncryptedString
from app.models.base import Base
from app.schemas.holdings import VALID_ASSET_TYPES, VALID_CURRENCIES, VALID_PRICING_MODES
from app.services.asset_class_config import VALID_ASSET_CLASSES


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    # Sorted here (not left to call sites) so every CheckConstraint's SQL
    # text is deterministic regardless of the source constant's declaration
    # order — PR #114 review: pricing_mode/asset_type previously used
    # declaration order here while currency/asset_class were sorted,
    # producing SQL text that wouldn't match the (also-sorted) migration's
    # DDL and could trip `alembic revision --autogenerate` as spurious drift.
    quoted = ", ".join(f"'{v}'" for v in sorted(values))
    return f"{column} IN ({quoted})"


class Holding(Base):
    __tablename__ = "holdings"

    # Domain CHECK constraints (issue #25) — mirrors migration 6cd7544f63cf.
    # Values come from the same sources of truth the migration uses, so the
    # ORM model can't quietly drift from the real DB schema.
    #
    # `name=` here is the bare token, not the full "ck_holdings_<x>" — Base's
    # naming_convention ("ck": "ck_%(table_name)s_%(constraint_name)s") re-
    # renders whatever name is passed, so a pre-rendered full name doubles
    # the prefix. Verified: Holding.__table__.constraints showed
    # "ck_holdings_ck_holdings_pricing_mode" before this fix.
    __table_args__ = (
        CheckConstraint(_in_list_sql("pricing_mode", VALID_PRICING_MODES), name="pricing_mode"),
        CheckConstraint(_in_list_sql("asset_type", VALID_ASSET_TYPES), name="asset_type"),
        CheckConstraint(_in_list_sql("currency", tuple(VALID_CURRENCIES)), name="currency"),
        CheckConstraint(
            _in_list_sql("asset_class", tuple(VALID_ASSET_CLASSES)), name="asset_class"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Encrypted at rest (issue #31) — identity/amount fields that reveal what
    # the user holds and how much. See app/core/encryption.py for the key
    # scope decision (system-wide key, not per-user). NOT encrypted:
    # asset_type/asset_class/sector/market/currency/pricing_mode/position —
    # classification buckets, not individually identifying, and needed
    # queryable for SQL-level NULL/equality filters elsewhere in this file's
    # callers (see EncryptedString docstring).
    name: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    pricing_mode: Mapped[str] = mapped_column(Text, nullable=False)
    ticker: Mapped[str | None] = mapped_column(EncryptedString)
    fund_code: Mapped[str | None] = mapped_column(EncryptedString)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    shares: Mapped[Decimal | None] = mapped_column(EncryptedDecimal)
    avg_cost: Mapped[Decimal | None] = mapped_column(EncryptedDecimal)
    current_value: Mapped[Decimal | None] = mapped_column(EncryptedDecimal)
    asset_type: Mapped[str | None] = mapped_column(Text)
    # Economic-exposure classification, geography-first (STOCK / EQUITY_US_BROAD /
    # EQUITY_US_TECH / EQUITY_DM / EQUITY_CN / EQUITY_EM / EQUITY_BROAD /
    # COMMODITY / BOND_FUND / CASH_EQUIV). Set by the upload parser from a ticker
    # lookup table; asset_type retains the LLM-parsed product-form value. This is
    # the primary classification dimension for §1/distribution/§4.1 in reports —
    # sector below is retained only for forward-event holding-relevance mapping.
    asset_class: Mapped[str] = mapped_column(Text, nullable=False, server_default="STOCK")
    sector: Mapped[str | None] = mapped_column(Text)  # GICS-style; forward-event mapping only
    # User-declared market bucket (US / HK / A-Share / Other), preserved from the
    # upload. NULL = not declared → derived from ticker at compute time.
    market: Mapped[str | None] = mapped_column(Text)
    # Row order in the uploaded file, so reports can mirror the user's layout.
    position: Mapped[int | None] = mapped_column(Integer)
    broker: Mapped[str | None] = mapped_column(EncryptedString)
    account: Mapped[str | None] = mapped_column(EncryptedString)
    portfolio: Mapped[str | None] = mapped_column(EncryptedString)
    notes: Mapped[str | None] = mapped_column(EncryptedString)
    market_price: Mapped[Decimal | None] = mapped_column(EncryptedDecimal)
    price_as_of: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    price_fetched_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_manual_update: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
