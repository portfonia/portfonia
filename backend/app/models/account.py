from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.encryption import EncryptedString
from app.models.base import Base
from app.models.user import User


class Account(Base):
    """Normalized broker/account/portfolio (issue #129 checkpoint B7,
    Ring 1-B design.md §9.2 — decision point 5).

    `Holding.broker`/`.account`/`.portfolio` (free-text, encrypted, in use
    since Ring 0) are NOT replaced by this table — report §1's broker
    grouping and the upload parser still read those three columns directly.
    This table exists to give stage C's upcoming inline entry form a stable
    id to reference; `Holding.account_id` is an additive pointer, not a
    migration off the text columns.

    Currency deliberately stays on `Holding`, not here (§2.4/§9.2): the
    original spec's "account = 账户的本位币" assumption doesn't match
    reality — a single broker/account routinely holds more than one
    currency (e.g. IBKR: USD equities + HKD equities in the same account).
    """

    __tablename__ = "accounts"
    __table_args__ = (
        # Lets `holdings.account_id` carry a composite FK to (accounts.id,
        # accounts.user_id) — review finding, PR #247: a single-column FK on
        # account_id alone only guarantees the account exists, not that it
        # belongs to the same user as the holding. `id` alone is already
        # unique (it's the PK); this constraint exists solely so Postgres
        # has a unique target to reference for the pair.
        UniqueConstraint("id", "user_id", name="uq_accounts_id_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    broker: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    account: Mapped[str | None] = mapped_column(EncryptedString)
    portfolio: Mapped[str | None] = mapped_column(EncryptedString)
    archived_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Purely for unit-of-work flush ordering (issue #129 B7) — not for query
    # navigation. See Holding.user's docstring comment for why this is
    # necessary at all. `lazy="raise"` (review, PR #247): forces any
    # accidental `.user` access to fail loudly instead of emitting a hidden
    # SELECT. `passive_deletes=True`: a `session.delete(account)` must not
    # have the ORM try to load/null-out relationships and fight the DB's
    # own RESTRICT — RESTRICT is the actual enforcement mechanism here.
    user: Mapped[User] = relationship(lazy="raise", passive_deletes=True)
