from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SearchCache(Base):
    """Tavily search-result cache (issue #128, Ring 1 A2 — design doc §4.4).

    Keyed by `(query_hash, trade_date)` — a normalized query executed once
    per calendar day is reused for every subsequent report (any user) that
    proposes the same query, instead of re-billing Tavily for it. This also
    doubles as the authoritative count of REAL Tavily API calls made on a
    given day: `report_search._tavily_used_today` counts rows here rather
    than counting proposed queries per report_inputs, which previously
    charged the daily budget for a query even when it hit cache and made no
    network call at all.
    """

    __tablename__ = "search_cache"
    __table_args__ = (
        UniqueConstraint("query_hash", "trade_date", name="uq_search_cache_query_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    query_hash: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    results: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
