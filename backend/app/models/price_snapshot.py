from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, Numeric, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PriceSnapshot(Base):
    """Historical price points captured at market-session nodes (ADR-002).

    Close node stores daily OHLCV; intraday nodes (pre_open / open / after_close)
    store a best-effort ``last`` (nullable when yfinance has no intraday data).
    Idempotent on ``(ticker, market, session_node, trade_date)`` so catch-up
    re-runs upsert rather than duplicate. Retention: 1 year. FX is NOT stored
    here — it stays in ``fx_rates``.
    """

    __tablename__ = "price_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "ticker", "market", "session_node", "trade_date", name="uq_price_snapshots_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    ticker: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)  # US / HK / A-Share
    session_node: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # pre_open/open/close/after_close
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[Decimal | None] = mapped_column(Numeric)
    high: Mapped[Decimal | None] = mapped_column(Numeric)
    low: Mapped[Decimal | None] = mapped_column(Numeric)
    close: Mapped[Decimal | None] = mapped_column(Numeric)
    last: Mapped[Decimal | None] = mapped_column(Numeric)  # intraday spot
    volume: Mapped[Decimal | None] = mapped_column(Numeric)
    source: Mapped[str] = mapped_column(Text, server_default=text("'yfinance'"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
