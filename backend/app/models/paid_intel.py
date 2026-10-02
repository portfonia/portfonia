"""URL-free article bodies, owning units and paid-call accounting."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class IntelArticle(Base):
    __tablename__ = "intel_articles"
    __table_args__ = (
        UniqueConstraint("url_key", "provider", name="uq_intel_articles_key_provider"),
        CheckConstraint("provider IN ('tavily','parallel')", name="provider"),
        CheckConstraint("status IN ('accepted','rejected','failed')", name="status"),
        CheckConstraint("(status = 'accepted') = (record IS NOT NULL)", name="record_status"),
        Index("ix_intel_articles_status_fetched_at", "status", "fetched_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    slot_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("intel_slot_runs.id"), nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    url_key: Mapped[str] = mapped_column(Text, nullable=False)
    news_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("news.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    record: Mapped[dict[str, object] | None] = mapped_column(JSONB(none_as_null=True))
    body_chars: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class IntelArticleLink(Base):
    __tablename__ = "intel_article_links"
    __table_args__ = (
        UniqueConstraint(
            "article_id",
            "identifier",
            "theme",
            name="uq_intel_article_links_key",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("(identifier IS NOT NULL) <> (theme IS NOT NULL)", name="owner"),
        CheckConstraint("role IN ('mover','quiet','macro')", name="role"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    article_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("intel_articles.id", ondelete="CASCADE"), nullable=False
    )
    identifier: Mapped[str | None] = mapped_column(Text)
    theme: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, nullable=False)


class PaidApiUsage(Base):
    __tablename__ = "paid_api_usage"
    __table_args__ = (
        CheckConstraint("provider IN ('tavily','parallel')", name="provider"),
        CheckConstraint("operation IN ('search','extract')", name="operation"),
        Index("ix_paid_api_usage_provider_created_at", "provider", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    slot_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("intel_slot_runs.id", ondelete="SET NULL")
    )
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    units: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
