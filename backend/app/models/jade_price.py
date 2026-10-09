"""Shared total-return history for Jade tools (issue #714)."""

from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class JadePriceSeries(Base):
    __tablename__ = "jade_price_series"
    series_key: Mapped[str] = mapped_column(Text, primary_key=True)
    last_attempt_on: Mapped[date | None] = mapped_column(Date)
    last_success_on: Mapped[date | None] = mapped_column(Date)
    unusable_reason: Mapped[str | None] = mapped_column(Text)


class JadePricePoint(Base):
    __tablename__ = "jade_price_points"
    series_key: Mapped[str] = mapped_column(
        Text, ForeignKey("jade_price_series.series_key", ondelete="CASCADE"), primary_key=True
    )
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    close: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    raw_close: Mapped[Decimal | None] = mapped_column(Numeric)
