from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Integer, Numeric, Text, func, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Holding(Base):
    __tablename__ = "holdings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    pricing_mode: Mapped[str] = mapped_column(Text, nullable=False)
    ticker: Mapped[str | None] = mapped_column(Text)
    fund_code: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    shares: Mapped[Decimal | None] = mapped_column(Numeric)
    avg_cost: Mapped[Decimal | None] = mapped_column(Numeric)
    current_value: Mapped[Decimal | None] = mapped_column(Numeric)
    asset_type: Mapped[str | None] = mapped_column(Text)
    # Economic classification (STOCK / EQUITY_BROAD / EQUITY_REGION / EQUITY_SECTOR /
    # COMMODITY / BOND_FUND / CASH_EQUIV).  Set by the upload parser from a ticker
    # lookup table; asset_type retains the LLM-parsed product-form value.
    asset_class: Mapped[str] = mapped_column(Text, nullable=False, server_default="STOCK")
    sector: Mapped[str | None] = mapped_column(Text)  # unified 12-class, §6.4
    # User-declared market bucket (US / HK / A-Share / Other), preserved from the
    # upload. NULL = not declared → derived from ticker at compute time.
    market: Mapped[str | None] = mapped_column(Text)
    # Row order in the uploaded file, so reports can mirror the user's layout.
    position: Mapped[int | None] = mapped_column(Integer)
    broker: Mapped[str | None] = mapped_column(Text)
    account: Mapped[str | None] = mapped_column(Text)
    portfolio: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    market_price: Mapped[Decimal | None] = mapped_column(Numeric)
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
