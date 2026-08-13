from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class NewsSurfaced(Base):
    """Dedup ledger: which news items have already appeared in a DONE-status report.

    Fixes H-DEBT-3 (issue #30): window selection used to be
    `published_at > start, <= end`, so a news item published inside a window
    but not ingested until after that window's `period_end` fell through BOTH
    the window it belongs to (not yet ingested when selected) and the next one
    (excluded as "already before this window's start") — a permanent miss.
    `load_news_window` (app/services/window_data.py) now selects by
    `published_at <= end` with no lower bound, relying on this table instead:
    once a news item has appeared in a report that reached success/
    needs_review/skipped, it's marked here and never selected again.
    """

    __tablename__ = "news_surfaced"
    __table_args__ = (UniqueConstraint("news_id", name="uq_news_surfaced_news_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    news_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    report_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    surfaced_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
