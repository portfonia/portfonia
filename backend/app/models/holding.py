from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.encryption import EncryptedDecimal, EncryptedString
from app.models.account import Account
from app.models.base import Base
from app.models.user import User
from app.schemas.holdings import VALID_ASSET_TYPES, VALID_CURRENCIES, VALID_PRICING_MODES
from app.services.asset_class_config import VALID_ASSET_CLASSES
from app.services.markets import VALID_HOLDING_MARKETS


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    # Sorted here (not left to call sites) so every CheckConstraint's SQL
    # text is deterministic regardless of the source constant's declaration
    # order — PR #114 review: pricing_mode/asset_type previously used
    # declaration order here while currency/asset_class were sorted,
    # producing SQL text that wouldn't match the (also-sorted) migration's
    # DDL and could trip `alembic revision --autogenerate` as spurious drift.
    quoted = ", ".join(f"'{v}'" for v in sorted(values))
    return f"{column} IN ({quoted})"


class Holding(Base):
    __tablename__ = "holdings"

    # Domain CHECK constraints (issue #25) — mirrors migration 6cd7544f63cf.
    # Values come from the same sources of truth the migration uses, so the
    # ORM model can't quietly drift from the real DB schema.
    #
    # `name=` here is the bare token, not the full "ck_holdings_<x>" — Base's
    # naming_convention ("ck": "ck_%(table_name)s_%(constraint_name)s") re-
    # renders whatever name is passed, so a pre-rendered full name doubles
    # the prefix. Verified: Holding.__table__.constraints showed
    # "ck_holdings_ck_holdings_pricing_mode" before this fix.
    __table_args__ = (
        CheckConstraint(_in_list_sql("pricing_mode", VALID_PRICING_MODES), name="pricing_mode"),
        CheckConstraint(_in_list_sql("asset_type", VALID_ASSET_TYPES), name="asset_type"),
        CheckConstraint(_in_list_sql("currency", tuple(VALID_CURRENCIES)), name="currency"),
        CheckConstraint(
            _in_list_sql("asset_class", tuple(VALID_ASSET_CLASSES)), name="asset_class"
        ),
        # Issue #311: closed set including Other as a legitimate fallback.
        # NULL still means "not declared" (derive at compute time). Bare
        # token "market" renders ck_holdings_market via naming_convention.
        CheckConstraint(
            "(market IS NULL) OR " + _in_list_sql("market", tuple(VALID_HOLDING_MARKETS)),
            name="market",
        ),
        # Composite, not a single-column FK on account_id alone (review, PR
        # #247): a single-column FK only guarantees the account exists, not
        # that it belongs to the same user as this holding. Postgres MATCH
        # SIMPLE (the default) skips the check entirely when either column
        # is NULL, so account_id=NULL still passes trivially — matches the
        # "no broker -> no account" rule.
        ForeignKeyConstraint(
            ["account_id", "user_id"],
            ["accounts.id", "accounts.user_id"],
            ondelete="RESTRICT",
            name="fk_holdings_account_id_user_id_accounts",
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
    # Encrypted at rest (issue #31) — identity/amount fields that reveal what
    # the user holds and how much. See app/core/encryption.py for the key
    # scope decision (system-wide key, not per-user). NOT encrypted:
    # asset_type/asset_class/sector/market/currency/pricing_mode/position —
    # classification buckets, not individually identifying, and needed
    # queryable for SQL-level NULL/equality filters elsewhere in this file's
    # callers (see EncryptedString docstring).
    name: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    pricing_mode: Mapped[str] = mapped_column(Text, nullable=False)
    ticker: Mapped[str | None] = mapped_column(EncryptedString)
    fund_code: Mapped[str | None] = mapped_column(EncryptedString)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    shares: Mapped[Decimal | None] = mapped_column(EncryptedDecimal)
    avg_cost: Mapped[Decimal | None] = mapped_column(EncryptedDecimal)
    current_value: Mapped[Decimal | None] = mapped_column(EncryptedDecimal)
    asset_type: Mapped[str | None] = mapped_column(Text)
    # Economic-exposure classification, geography-first (STOCK / EQUITY_US_BROAD /
    # EQUITY_US_TECH / EQUITY_DM / EQUITY_CN / EQUITY_EM / EQUITY_BROAD /
    # COMMODITY / BOND_FUND / CASH_EQUIV). Set by the upload parser from a ticker
    # lookup table; asset_type retains the LLM-parsed product-form value. This is
    # the primary classification dimension for §1/distribution/§4.1 in reports —
    # sector below is retained only for forward-event holding-relevance mapping.
    asset_class: Mapped[str] = mapped_column(Text, nullable=False, server_default="STOCK")
    sector: Mapped[str | None] = mapped_column(Text)  # GICS-style; forward-event mapping only
    # User-declared market bucket (closed set — see VALID_HOLDING_MARKETS).
    # NULL = not declared → derived from ticker at compute time.
    market: Mapped[str | None] = mapped_column(Text)
    # Explicit not-processed flag (issue #311). False when the ticker does
    # not resolve into a scheduled capture bucket. Never infer this from
    # market == "Other" — Other is a legitimate stored value.
    capture_supported: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), default=True
    )
    # Row order in the uploaded file, so reports can mirror the user's layout.
    position: Mapped[int | None] = mapped_column(Integer)
    broker: Mapped[str | None] = mapped_column(EncryptedString)
    account: Mapped[str | None] = mapped_column(EncryptedString)
    portfolio: Mapped[str | None] = mapped_column(EncryptedString)
    # Normalized pointer (issue #129 checkpoint B7, design §9.2) — additive,
    # not a replacement for the three text columns above. NULL for any
    # holding whose `broker` is NULL (accounts.broker is NOT NULL, so a
    # broker-less holding has no account to point at; report §1 already
    # buckets those into "Other"). Nothing in this codebase reads this
    # column yet — it exists for stage C's inline entry form.
    # FK declared via the composite ForeignKeyConstraint in __table_args__
    # above (account_id, user_id) -> (accounts.id, accounts.user_id), not
    # inline here.
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    notes: Mapped[str | None] = mapped_column(EncryptedString)
    market_price: Mapped[Decimal | None] = mapped_column(EncryptedDecimal)
    price_as_of: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    price_fetched_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_manual_update: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Not for query-time navigation. Declared solely so SQLAlchemy's
    # unit-of-work knows about the FK dependency: without a relationship(),
    # the ORM flush process has no way to know `holdings` must be inserted
    # after `users`/`accounts` (it does NOT infer this from a bare
    # ForeignKey() column — verified empirically once the FKs below started
    # rejecting existing test fixtures that add a User and its Holdings in
    # one flush). `viewonly=False` (the default) is required — a viewonly
    # relationship is excluded from unit-of-work dependency processing.
    # `lazy="raise"` (review, PR #247): forces an accidental `.user`/
    # `.account_ref` access to fail loudly instead of emitting a hidden
    # SELECT (an N+1 risk on any list of holdings). `passive_deletes=True`:
    # a `session.delete(holding)` must not have the ORM try to load/null
    # relationships and fight the DB's own RESTRICT.
    user: Mapped[User] = relationship(lazy="raise", passive_deletes=True)
    # `overlaps="user"`: the composite FK (account_id, user_id) makes
    # SQLAlchemy think this relationship and `user` above might both try to
    # write `holdings.user_id` — neither ever does (both are lazy="raise",
    # never assigned to; user_id/account_id are always set directly as
    # plain columns), so this silences a real but inapplicable warning
    # rather than papering over an actual write conflict.
    account_ref: Mapped[Account | None] = relationship(
        lazy="raise", passive_deletes=True, overlaps="user"
    )
