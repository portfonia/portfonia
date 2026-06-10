from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "report_date",
            "report_type",
            "session_node",
            name="uq_reports_user_date_type_session",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    report_type: Mapped[str] = mapped_column(Text, nullable=False)
    # H-DEBT-1: identifies WHICH trigger produced this report (e.g. "manual" vs
    # "after_close" for the M/W/F 16:30 ET cadence), so two distinct triggers on
    # the same calendar day don't collide on the dedup key. A redelivered Celery
    # task passes the same session_node, preserving redelivery dedup.
    session_node: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    disclaimer_version: Mapped[str | None] = mapped_column(Text)
    report_md: Mapped[str | None] = mapped_column(Text)
    report_html: Mapped[str | None] = mapped_column(Text)
    # SENSITIVE: full portfolio snapshot (names, tickers, market values, ratios).
    # Stored as plaintext JSONB and never exposed via ReportOut. Keep it out of
    # any API schema/log; encrypt at rest before Ring 1 (see CLAUDE.md Data Handling).
    report_inputs: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    email_sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    generated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    # ADR-002: the intel/price window this report covered. period_start = the
    # previous report's period_end (watermark); period_end = this run's cutoff.
    period_start: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
