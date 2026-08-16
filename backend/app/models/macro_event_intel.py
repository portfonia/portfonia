from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MacroEventIntel(Base):
    """L2 shared macro-event intel cache (issue #128, Ring 1 A3 — design doc
    §5.4, Hermes/Portfonia/Docs/Ring 1-A design.md).

    One LLM inference per (event_key, trade_date, prompt_version), reused
    across every user whose report touches that event that day. `event_key`
    is prefixed by source so the two event vocabularies share one table
    without colliding: `theme:<macro theme name>` (a `macro_detector`
    ThemeHit) and `fwd:<forward_events.id>` (a scheduled calendar row,
    already uniquely keyed by `uq_forward_events_key`).

    `prompt_version` is part of the unique key, not just an audit column —
    same reason as `ticker_intel`: a prompt revision must not keep serving
    an older entry's wording (or, here, an older entry's CLASSIFICATION)
    under a new contract.

    `analysis` is NULLABLE with the same meaning `ticker_intel` gives it: a
    NULL row is an "attempted, no usable result" marker, written when the
    LLM call failed, its JSON could not be parsed, or the compliance scan
    blocked the output — so a systematically failing/blocked event is
    attempted at most once per day rather than once per user in the fan-out.

    `affected_asset_classes` / `affected_sectors` hold the STRUCTURED half of
    the inference, already filtered to the closed taxonomies
    (`asset_class_config.VALID_ASSET_CLASSES` /
    `sector_taxonomy.VALID_SECTORS`) — an out-of-taxonomy label the model
    invented never reaches this table, so nothing downstream has to
    re-validate before intersecting it with a user's portfolio.
    `affected_sectors` exists for the forward-event holding-relevance
    mapping that already runs on `sector` (`report_sections._forward_exposure`)
    — this is CLAUDE.md's one sanctioned use of `sector`, and A3 does not
    widen it: the per-user exposure mapping added here reads asset_class
    only.

    Contents are safe unencrypted for the same reason `ticker_intel`'s are:
    the analysis is built ONLY from public macro-theme headlines and
    published calendar facts, with no per-user data in the prompt. The one
    residual disclosure — a `fwd:` row for an earnings event reveals that
    some user holds that ticker — is the same accepted, non-attributable
    residual recorded for `ticker_intel` (design doc §4.4).
    """

    __tablename__ = "macro_event_intel"
    __table_args__ = (
        UniqueConstraint(
            "event_key",
            "trade_date",
            "prompt_version",
            name="uq_macro_event_intel_key_date_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    event_key: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    analysis: Mapped[str | None] = mapped_column(Text, nullable=True)
    affected_asset_classes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    affected_sectors: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
