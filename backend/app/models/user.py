from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Date, Numeric, SmallInteger, Text, func, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encryption import EncryptedString
from app.models.base import Base
from app.schemas.holdings import VALID_CURRENCIES

VALID_USER_STATUSES = ("active", "deleted", "suspended")
VALID_AUTH_PROVIDERS = ("supabase",)
VALID_REPORT_CADENCES = ("daily", "mwf", "none", "weekly")
VALID_SUBSCRIPTION_STATUSES = ("active", "cancelled", "expired", "inactive")
VALID_SUBSCRIPTION_TYPES = ("daily", "jade", "mwf", "weekly")
# Report language codes (issues #308 and #582), separate from UI locales.
# zh always means Simplified Chinese; zh-Hant uses shared source text and
# conversion. See the per-user report language mechanism documentation.
VALID_REPORT_LANGUAGES = ("en", "zh", "zh-Hant")


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in sorted(values))
    return f"{column} IN ({quoted})"


class User(Base):
    """Portfonia account row. PK is ours, not the Auth provider's subject.

    Ring 1-B design.md §6.3: our own UUID preserves historical account
    bindings without updating holdings/reports and keeps Auth replaceable.
    The ADMIN_ID root actor has no persistent account (issue #672); the
    historical binding migration only exercises that id in a throwaway test. `is_admin` is a reserved column —
    Ring 1 code must not read it (decision point 12).
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(_in_list_sql("status", VALID_USER_STATUSES), name="status"),
        CheckConstraint(_in_list_sql("auth_provider", VALID_AUTH_PROVIDERS), name="auth_provider"),
        CheckConstraint(
            _in_list_sql("report_cadence", VALID_REPORT_CADENCES), name="report_cadence"
        ),
        CheckConstraint(_in_list_sql("locale", VALID_REPORT_LANGUAGES), name="locale"),
        CheckConstraint(
            _in_list_sql("base_currency", tuple(VALID_CURRENCIES)), name="base_currency"
        ),
        CheckConstraint(
            _in_list_sql("subscription_status", VALID_SUBSCRIPTION_STATUSES),
            name="subscription_status",
        ),
        CheckConstraint(
            "subscription_type IS NULL OR "
            + _in_list_sql("subscription_type", VALID_SUBSCRIPTION_TYPES),
            name="subscription_type",
        ),
        CheckConstraint("subscription_anchor_day BETWEEN 1 AND 31", name="subscription_anchor_day"),
        CheckConstraint("credit_gift_balance >= 0", name="credit_gift_balance"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    auth_provider: Mapped[str] = mapped_column(Text, nullable=False)
    auth_subject: Mapped[str | None] = mapped_column(Text, unique=True)
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    credit_cash_balance: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, server_default=text("0")
    )
    credit_gift_balance: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, server_default=text("0")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    display_name: Mapped[str | None] = mapped_column(EncryptedString)
    locale: Mapped[str] = mapped_column(Text, nullable=False)
    base_currency: Mapped[str] = mapped_column(Text, nullable=False)
    report_cadence: Mapped[str] = mapped_column(Text, nullable=False)
    subscription_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'inactive'")
    )
    subscription_type: Mapped[str | None] = mapped_column(Text)
    subscription_expires_on: Mapped[date | None] = mapped_column(Date)
    subscription_period_start: Mapped[date | None] = mapped_column(Date)
    subscription_anchor_day: Mapped[int | None] = mapped_column(SmallInteger)
    subscription_cancel_pending: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    subscription_adjusted_on: Mapped[date | None] = mapped_column(Date)
    delivery_email: Mapped[str | None] = mapped_column(Text)
    # Denormalized hot-path fields (issue #260, Ring 1-Email Validation design
    # doc §3.2) — set when the corresponding EmailVerification transitions to
    # `verified`, cleared on unsubscribe (issue #257 / design doc §3.7) for
    # the matching address only. Report-send gating is a separate consumer
    # (issue #276); the current readers are GET /me (issue #269) and the
    # unsubscribe confirm path.
    email_verified_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    delivery_email_verified_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    # Audit only (issue #220/#221). NULL means "registered before the ToS
    # gate existed" — never surfaced as a fixable gap; #221's signup flow is
    # the only writer, not implemented yet as of this column landing.
    tos_accepted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    invited_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    grand_invited_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    last_login_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
