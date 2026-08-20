from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, Integer, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CrossNameIntel(Base):
    """L3 day-level cross-identifier synthesis cache (issue #128 quality gate
    — design doc §6.7 item 1, Hermes/Portfonia/Docs/Ring 1-A design.md).

    The third shared layer, and the only one keyed on a DAY rather than on a
    thing: L1 answers "what happened to this identifier", L2 answers "what is
    this event and which classes does it bear on", and neither can express
    "these identifiers moved together today for one mechanism". Pass 2 makes
    that join inside its single per-user call; assembly is contractually
    forbidden to invent edges L1/L2 never wrote, which is why three overlay
    comparisons all showed a complete-but-uncorrelated body (design doc §6.6).
    This table holds that join, inferred once for the whole system per trading
    day and read by every user's assembly pass.

    `clusters` is a JSONB list, NULLABLE with exactly the meaning
    `TickerIntel.analysis`/`MacroEventIntel.analysis` carry: a NULL row is an
    "attempted, no usable result" marker, so a systematically failing or
    compliance-blocked synthesis is attempted a bounded number of times per
    day rather than once per user in the fan-out. `attempt_count` bounds that
    exactly as issue #160 defined it for the other two tables — keep the three
    `_MAX_ATTEMPTS_PER_KEY` constants in step; they are one mechanism applied
    three times.

    WHY THE OUTPUT IS A LIST OF CLUSTERS AND NOT ONE DAY-LEVEL PARAGRAPH —
    this is a leak-prevention property, not a formatting preference. Assembly
    may only show a user conclusions about names that user holds, so whatever
    is stored here has to be narrowable by identifier. A single paragraph
    describing every identifier the system briefed today cannot be narrowed:
    handing it to assembly would put other users' holdings into this user's
    report body (design doc §1.3's cross-user leak, arriving through prose
    instead of through a number). Each cluster therefore carries a structured
    `identifiers` list — filterable — and a `summary` written about the
    MECHANISM with no names in it; `cross_name_intel.clusters_for_user` drops
    any cluster whose summary names an identifier the reader does not hold.

    `input_fingerprint` is a sha256 over the sorted set of identifiers that
    had a servable L1 analysis when the synthesis ran, and it is part of the
    unique key. Keying on `(trade_date, prompt_version)` alone would freeze
    the day's conclusion to whatever the FIRST user's book happened to cover:
    a later user in the same fan-out contributes their own L1 rows, and under
    a date-only key they would read a synthesis that structurally cannot
    mention any of their names. That is the "early write locks the day"
    failure round 6 found one layer down in L1's headline-only path (design
    doc §4.8, addendum 4). The fingerprint stays a GLOBAL key: it is derived
    from `ticker_intel` rows, which carry no user_id, never from who asked.

    Contents are safe unencrypted for the same reason `ticker_intel`'s and
    `macro_event_intel`'s are: the inputs are public per-security briefings
    and public macro analyses, with no per-user data in the prompt. The
    residual disclosure — a row reveals that some user in the system holds
    these identifiers today — is the same accepted, non-attributable residual
    recorded for the other two tables (design doc §4.4).
    """

    __tablename__ = "cross_name_intel"
    __table_args__ = (
        UniqueConstraint(
            "trade_date",
            "prompt_version",
            "input_fingerprint",
            name="uq_cross_name_intel_date_version_fingerprint",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    clusters: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
