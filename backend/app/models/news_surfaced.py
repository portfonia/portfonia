from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.user import User


class NewsSurfaced(Base):
    """Dedup ledger: which news items have already appeared in a DONE-status report,
    scoped PER USER.

    Fixes H-DEBT-3 (issue #30): window selection used to be
    `published_at > start, <= end`, so a news item published inside a window
    but not ingested until after that window's `period_end` fell through BOTH
    the window it belongs to (not yet ingested when selected) and the next one
    (excluded as "already before this window's start") — a permanent miss.
    `load_news_window` (app/services/window_data.py) now selects by
    `published_at <= end` with no lower bound, relying on this table instead:
    once a news item has appeared in a report that reached success/
    needs_review/skipped, it's marked here and never selected again.

    PR #139 review: `news` is a GLOBAL capture-layer store (no `user_id`
    column), but reports are per-user with independent watermarks/windows —
    two different users' report streams can both legitimately need to see the
    same news item for the first time. Uniqueness is (user_id, news_id), not
    news_id alone, or User B's report would silently never surface an item
    already shown to User A.
    """

    __tablename__ = "news_surfaced"
    __table_args__ = (UniqueConstraint("user_id", "news_id", name="uq_news_surfaced_user_news"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    news_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    report_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    surfaced_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    # Purely for unit-of-work flush ordering (issue #129 B7) — not for query
    # navigation. See Holding.user's docstring comment for why this is
    # necessary at all.
    user: Mapped[User] = relationship()
