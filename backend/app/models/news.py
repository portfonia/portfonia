from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class News(Base):
    """Persisted news items (ADR-002 capture layer).

    A long-term knowledge base — the substrate for future mempalace vector / KG
    enrichment — and the cross-run dedup source (by ``url_hash``). Stores the RSS
    summary, not the full article body. Retention: 1 year.
    """

    __tablename__ = "news"
    __table_args__ = (UniqueConstraint("url_hash", name="uq_news_url_hash"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    url_hash: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
