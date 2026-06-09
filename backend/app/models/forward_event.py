from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ForwardEvent(Base):
    """Scheduled forward events for the forward-calendar block (#1).

    Two kinds, both US-only (China forward intel is out of scope):
    - ``macro``    — official scheduled data releases (FRED release dates) plus
                     hardcoded FOMC meeting dates. ``ticker`` is "" (not a position).
    - ``earnings`` — a held company's next reported earnings date. ``ticker`` is
                     the symbol so the report can map it to that exact holding.

    These are *scheduled dates*, not predictions: the report frames them as "X is
    scheduled for Y, your Z has exposure, watch W" and never forecasts an outcome.
    Idempotent on ``(event_type, name, ticker, scheduled_date)`` — ``ticker``
    defaults to "" (not NULL) so the unique key dedups macro rows too. Catch-up
    re-runs upsert rather than duplicate. Retention pruning is left to a later ring.
    """

    __tablename__ = "forward_events"
    __table_args__ = (
        UniqueConstraint(
            "event_type", "name", "ticker", "scheduled_date", name="uq_forward_events_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)  # macro / earnings
    name: Mapped[str] = mapped_column(Text, nullable=False)  # release/company name
    ticker: Mapped[str] = mapped_column(Text, server_default=text("''"), nullable=False)
    scheduled_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)  # fred / fomc / yfinance
    captured_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
