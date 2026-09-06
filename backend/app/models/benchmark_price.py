"""Daily benchmark index closes for the Portfolio Performance chart
(issue #360 Phase 1, D9). Unrelated to holdings — a plain price time series,
analogous to `price_snapshots` but keyed by `index_code` instead of ticker
and with no market-session-node dimension (one close per calendar day).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, Numeric, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# `nasdaq` = Nasdaq Composite, not the Nasdaq-100 (D9 — explicit, since both
# are common "Nasdaq" shorthands in practice).
VALID_BENCHMARK_INDEX_CODES = ("sp500", "dow30", "nasdaq")


class BenchmarkPrice(Base):
    __tablename__ = "benchmark_prices"
    __table_args__ = (
        UniqueConstraint("index_code", "price_date", name="uq_benchmark_prices_index_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    index_code: Mapped[str] = mapped_column(Text, nullable=False)
    price_date: Mapped[date] = mapped_column(Date, nullable=False)
    close_price: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    # Source currency of close_price (yfinance's `^GSPC`/`^DJI`/`^IXIC` are
    # all USD-denominated today; stored rather than assumed so a future
    # non-USD benchmark source doesn't require a schema change).
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'USD'"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
