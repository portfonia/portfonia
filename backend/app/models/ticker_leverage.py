from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Numeric, Text, func
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.holding import _in_list_sql
from app.services.asset_class_config import VALID_ASSET_CLASSES

VALID_LEVERAGE_DIRECTIONS: tuple[str, ...] = ("bull", "bear")


class TickerLeverageOverride(Base):
    """System-wide leveraged-product multiplier, keyed by normalized ticker (issue #87).

    Not per-user — same sharing model as ``ticker_themes`` and
    ``asset_class_thresholds.yml``: one record applies to every holding that
    references the ticker, regardless of owner. ``ticker`` must already be
    normalized+uppercased via the same helper the FX-pair/asset_class
    lookups use (``app.services.instrument_symbols.intelligence_identifier``
    — see the issue #204 mechanism note) before it reaches this table; an
    un-normalized PK would silently split one ticker's override across two
    rows the way an un-normalized PSH/PSH.L pair once did, before issue
    #417 removed that ticker-specific override.

    Threshold multiples are applied at read time by ``window_data.py`` (anomaly thresholds
    widened) and ``portfolio_calculator.py`` (§4.1 single-holding
    concentration thresholds tightened). Since #839, registration also drives
    ``Holding.asset_class`` and underlying earnings exposure.
    """

    __tablename__ = "ticker_leverage_overrides"

    __table_args__ = (
        CheckConstraint(
            "asset_class IS NULL OR "
            + _in_list_sql("asset_class", tuple(sorted(VALID_ASSET_CLASSES))),
            name="asset_class",
        ),
        CheckConstraint("leverage_multiple > 0", name="leverage_multiple_positive"),
        CheckConstraint("direction IS NULL OR direction IN ('bear', 'bull')", name="direction"),
    )

    ticker: Mapped[str] = mapped_column(Text, primary_key=True)
    leverage_multiple: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    # Documentation/audit only (issue #87 design comment) — does not
    # currently change the threshold math in window_data.py/
    # portfolio_calculator.py, both of which use leverage_multiple alone.
    direction: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    asset_class: Mapped[str | None] = mapped_column(Text)
    underlying_ticker: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
