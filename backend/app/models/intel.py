"""Shared collection profiles, links and run evidence (#620)."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class NewsInstrument(Base):
    __tablename__ = "news_instruments"
    __table_args__ = (
        UniqueConstraint("news_id", "identifier", name="uq_news_instruments_key"),
        Index("ix_news_instruments_identifier_created_at", "identifier", "created_at"),
        CheckConstraint(
            "relation IS NULL OR relation IN ('supplier', 'customer', 'competitor', 'input')",
            name="relation",
        ),
        CheckConstraint("(relation IS NULL) = (related_to IS NULL)", name="related_to"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    news_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("news.id", ondelete="CASCADE"), nullable=False
    )
    identifier: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    # NULL = the headline names this instrument; otherwise a related entity's
    # headline kept through the relation table (#681).
    relation: Mapped[str | None] = mapped_column(Text)
    related_to: Mapped[str | None] = mapped_column(Text)


class InstrumentProfile(Base):
    __tablename__ = "instrument_profiles"
    identifier: Mapped[str] = mapped_column(Text, primary_key=True)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    name_en: Mapped[str | None] = mapped_column(Text)
    name_zh: Mapped[str | None] = mapped_column(Text)
    aliases: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    name_source: Mapped[str | None] = mapped_column(Text)
    name_resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    news_collected_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class IntelSlotRun(Base):
    __tablename__ = "intel_slot_runs"
    __table_args__ = (
        UniqueConstraint("slot", "run_date", name="uq_intel_slot_key"),
        CheckConstraint("slot IN ('pre_open','post_close')", name="slot"),
        CheckConstraint("status IN ('running','ok','partial','failed')", name="status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    slot: Mapped[str] = mapped_column(Text, nullable=False)
    run_date: Mapped[date] = mapped_column(Date, nullable=False)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    digest_sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class IntelCollectionRun(Base):
    __tablename__ = "intel_collection_runs"
    __table_args__ = (
        CheckConstraint("kind IN ('rss','instrument')", name="kind"),
        CheckConstraint("status IN ('running','ok','partial','failed')", name="status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    slot_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("intel_slot_runs.id"))
    node: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    instruments_total: Mapped[int | None] = mapped_column(Integer)
    instruments_processed: Mapped[int | None] = mapped_column(Integer)
    stats: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    errors: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
