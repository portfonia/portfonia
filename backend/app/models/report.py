from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import CheckConstraint, Date, ForeignKey, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.user import User

# issue #104: same closed set as email_verifications.purpose (account_email /
# delivery_email) — recipient_purpose only ever records which of those two
# fields the send actually used, never ops_manual (reports are never sent to
# an Ops-probed address).
VALID_REPORT_RECIPIENT_PURPOSES = ("account_email", "delivery_email")


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in sorted(values))
    return f"{column} IN ({quoted})"


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
        # Nullable column: a NULL value never trips a SQL CHECK (only FALSE
        # does), so this only constrains rows that actually recorded a
        # purpose — same pattern as email_verifications.purpose.
        CheckConstraint(
            _in_list_sql("recipient_purpose", VALID_REPORT_RECIPIENT_PURPOSES),
            name="recipient_purpose",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    report_type: Mapped[str] = mapped_column(Text, nullable=False)
    # H-DEBT-1: identifies WHICH trigger produced this report (e.g. "manual" vs
    # "after_close" for the M/W/F 17:00 ET cadence), so two distinct triggers on
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
    # issue #45: Resend's own delivery id, so a sent report can be
    # cross-referenced against Resend's delivery/bounce/complaint webhooks.
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    # issue #104 (Ring 1-Email Validation.md, 2026-09-03 section): the REAL
    # address/purpose a send actually used, written atomically alongside
    # email_sent_at/provider_message_id. Deliberately not re-derived from
    # recipient_email_with_purpose() after the fact — the user may have
    # changed their delivery address between send and any later read, and
    # poll_report_delivery needs the address this SPECIFIC send reached.
    recipient_email: Mapped[str | None] = mapped_column(Text)
    recipient_purpose: Mapped[str | None] = mapped_column(Text)
    generated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    # ADR-002: the intel/price window this report covered. period_start = the
    # previous report's period_end (watermark); period_end = this run's cutoff.
    period_start: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    # Purely for unit-of-work flush ordering (issue #129 B7) — not for query
    # navigation. See Holding.user's docstring comment for why this is
    # necessary at all.
    user: Mapped[User] = relationship(lazy="raise", passive_deletes=True)
