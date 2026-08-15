from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TickerIntel(Base):
    """L1 shared ticker-level intel cache (issue #128, Ring 1 A2 — design doc
    §4.4, Hermes/Portfonia/Docs/Ring 1-A design.md).

    One LLM analysis per (identifier, trade_date, prompt_version), computed
    once and reused across every user who holds the identifier that day —
    the point of this table is that a multi-user fan-out never re-analyzes
    the same ticker's window move twice. `prompt_version` is part of the
    unique key (not just an audit column) so a prompt revision doesn't
    silently keep serving an older cache entry's wording under a new prompt
    contract.

    `analysis` is safe to leave unencrypted: its content is a shared,
    per-security factual briefing built ONLY from public window-move/news/
    technical facts (see app/services/ticker_intel.py's L1Facts — no
    per-user data ever reaches the prompt that produces it). The one
    residual disclosure this table accepts is that a row's mere existence
    reveals "some user in the system holds this identifier today" — not
    attributable to any specific user, judged acceptable (design doc §4.4).
    """

    __tablename__ = "ticker_intel"
    __table_args__ = (
        UniqueConstraint(
            "identifier",
            "trade_date",
            "prompt_version",
            name="uq_ticker_intel_identifier_date_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    identifier: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    analysis: Mapped[str] = mapped_column(Text, nullable=False)
    facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
