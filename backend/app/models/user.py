from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Text, func, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encryption import EncryptedString
from app.models.base import Base

VALID_USER_STATUSES = ("active", "deleted", "suspended")
VALID_AUTH_PROVIDERS = ("supabase",)
VALID_REPORT_CADENCES = ("mwf", "weekly")


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in sorted(values))
    return f"{column} IN ({quoted})"


class User(Base):
    """Portfonia account row. PK is ours, not the Auth provider's subject.

    Ring 1-B design.md §6.3: keeping our own UUID lets the production
    DEV_USER_ID bind in place (no UPDATE of holdings/reports) and keeps
    the auth provider replaceable. `is_admin` is a reserved column —
    Ring 1 code must not read it (decision point 12).
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(_in_list_sql("status", VALID_USER_STATUSES), name="status"),
        CheckConstraint(_in_list_sql("auth_provider", VALID_AUTH_PROVIDERS), name="auth_provider"),
        CheckConstraint(
            _in_list_sql("report_cadence", VALID_REPORT_CADENCES), name="report_cadence"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    auth_provider: Mapped[str] = mapped_column(Text, nullable=False)
    auth_subject: Mapped[str | None] = mapped_column(Text, unique=True)
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    display_name: Mapped[str | None] = mapped_column(EncryptedString)
    locale: Mapped[str] = mapped_column(Text, nullable=False)
    base_currency: Mapped[str] = mapped_column(Text, nullable=False)
    report_cadence: Mapped[str] = mapped_column(Text, nullable=False)
    delivery_email: Mapped[str | None] = mapped_column(Text)
    # Denormalized hot-path fields (issue #260, Ring 1-Email Validation design
    # doc §3.2) — set when the corresponding EmailVerification transitions to
    # `verified`. Intended to be cleared on a value change or (future, #257)
    # unsubscribe (round-4 review: no clearing path exists yet — nothing in
    # this PR writes `NULL` back into either column, so a future address
    # change via a non-verification path could leave a stale verified
    # timestamp next to a new, unverified value). Report-send gating is a
    # separate, not-yet-landed consumer of these; nothing reads them today.
    email_verified_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    delivery_email_verified_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    # Audit only (issue #220/#221). NULL means "registered before the ToS
    # gate existed" — never surfaced as a fixable gap; #221's signup flow is
    # the only writer, not implemented yet as of this column landing.
    tos_accepted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    invited_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    last_login_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
