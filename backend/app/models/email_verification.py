from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

VALID_EMAIL_VERIFICATION_PURPOSES = ("account_email", "delivery_email", "ops_manual")
VALID_EMAIL_VERIFICATION_STATUSES = (
    "pending",
    "verified",
    "expired",
    "superseded",
    "undeliverable",
)


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in sorted(values))
    return f"{column} IN ({quoted})"


class EmailVerification(Base):
    """Generic email-verification record (Ring 1-Email Validation design doc, issue #260).

    Append-only history, not the hot-path source of truth for "is this
    address usable right now" — that's `users.email_verified_at` /
    `users.delivery_email_verified_at`, written alongside this row's
    `status` transition to `verified` (see `app/services/email_verification.py`).
    `token_hash` is `sha256(token)`; the plaintext token exists only in
    memory long enough to build the verification email, matching
    `invites.token_hash`'s discipline.
    """

    __tablename__ = "email_verifications"
    __table_args__ = (
        CheckConstraint(_in_list_sql("purpose", VALID_EMAIL_VERIFICATION_PURPOSES), name="purpose"),
        CheckConstraint(_in_list_sql("status", VALID_EMAIL_VERIFICATION_STATUSES), name="status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # NULL only for an Ops API probe not scoped to a known user (purpose=ops_manual).
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_sent_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    resend_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # Resend's own delivery id — same pattern as reports.provider_message_id
    # (issue #45) — used to poll GET /emails/{id} for a bounce/complaint
    # signal (design doc §3.3 step 6).
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
