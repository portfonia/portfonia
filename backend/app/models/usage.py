"""Content-free spend and host samples (#886)."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class LlmUsage(Base):
    __tablename__ = "llm_usage"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('report_pass2','report_regenerate','translation','holdings_parse','intel_classifier','intel_name_check')",
            name="purpose",
        ),
        CheckConstraint("trigger_source IN ('user','ops','scheduled')", name="trigger_source"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), index=True
    )
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    purpose: Mapped[str] = mapped_column(Text)
    trigger_source: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))


class ExternalCallDaily(Base):
    __tablename__ = "external_call_daily"
    __table_args__ = (
        CheckConstraint(
            "provider IN ('resend','finnhub','massive','twelvedata','yfinance','eastmoney','sina','tencent','google_news','sec','rss','fred')",
            name="provider",
        ),
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    calls: Mapped[int] = mapped_column(BigInteger)


class HostSample(Base):
    __tablename__ = "host_samples"
    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    load_15m: Mapped[float] = mapped_column(Double)
    mem_total_bytes: Mapped[int] = mapped_column(BigInteger)
    mem_available_bytes: Mapped[int] = mapped_column(BigInteger)
    swap_total_bytes: Mapped[int] = mapped_column(BigInteger)
    swap_free_bytes: Mapped[int] = mapped_column(BigInteger)
    pswpin: Mapped[int] = mapped_column(BigInteger)
    pswpout: Mapped[int] = mapped_column(BigInteger)
    oom_kill: Mapped[int] = mapped_column(BigInteger)
    disk_used_bytes: Mapped[int] = mapped_column(BigInteger)
    disk_avail_bytes: Mapped[int] = mapped_column(BigInteger)
    pg_bytes: Mapped[int] = mapped_column(BigInteger)
    swappiness: Mapped[int] = mapped_column(SmallInteger)
    redis_used_memory_bytes: Mapped[int | None] = mapped_column(BigInteger)
