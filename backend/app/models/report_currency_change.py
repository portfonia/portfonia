"""Append-only log of report/base currency preference changes (issue #372).

Does not rewrite historical `portfolio_value_snapshots.base_currency`.
Those rows keep the capture-time currency from issue #367.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.schemas.holdings import VALID_CURRENCIES

VALID_REPORT_CURRENCY_CHANGE_SOURCES = ("admin", "self")


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in sorted(values))
    return f"{column} IN ({quoted})"


class ReportCurrencyChange(Base):
    """One successful change of `users.base_currency`.

    `user_id` is ON DELETE CASCADE: this is an ops audit of a preference,
    not a financial record that should block purge (same class as
    `portfolio_value_snapshots`). `actor_user_id` is SET NULL so an
    unrelated actor disappearing cannot delete another user's history.
    """

    __tablename__ = "report_currency_changes"
    __table_args__ = (
        CheckConstraint(_in_list_sql("old_currency", tuple(VALID_CURRENCIES)), name="old_currency"),
        CheckConstraint(_in_list_sql("new_currency", tuple(VALID_CURRENCIES)), name="new_currency"),
        CheckConstraint(
            _in_list_sql("source", VALID_REPORT_CURRENCY_CHANGE_SOURCES), name="source"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    old_currency: Mapped[str] = mapped_column(Text, nullable=False)
    new_currency: Mapped[str] = mapped_column(Text, nullable=False)
    # clock_timestamp() (not now()): tests share one outer transaction, and
    # now() is constant for that transaction — two sequential changes would
    # otherwise sort by UUID, not insertion order.
    changed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=text("clock_timestamp()"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
