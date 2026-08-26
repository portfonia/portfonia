from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encryption import EncryptedString
from app.models.base import Base


class UserInvestmentContext(Base):
    """A user's stated investment-style questionnaire + free text (issue #129
    checkpoint B6, Ring 1-B design.md §8.4).

    One row per user, no history table: re-answering the questionnaire
    overwrites this row (Concept §4.2 — "重答问卷覆盖原记录"). Reproducibility
    for a past report comes from the snapshot `generate_report` writes into
    that report's own `report_inputs` JSONB, not from versioning this table.

    `user_id` is this table's PK (not a separate `id`) — one context per
    user, and this table postdates `users` (unlike `holdings`/`reports`,
    which predate the B4 user system and only get their `user_id` FK in B7),
    so there is no legacy-data reason to defer the FK the way B7 does for
    those four tables (design doc §9.3 lists only holdings/reports/
    upload_jobs/news_surfaced there).
    """

    __tablename__ = "user_investment_context"

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id"),
        primary_key=True,
    )
    # Closed-enum answers for the 8 questionnaire dimensions (§8.3). Every key
    # is always present once a row exists — the frontend pre-fills every
    # question from the Concept §4.3 defaults, so a submit is never partial.
    questionnaire: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    # config/analysis_framework.yml-style version pin: questionnaire_taxonomy
    # .QUESTIONNAIRE_VERSION at the time this row was written, so a later
    # taxonomy change can tell an old answer set apart from a new one.
    questionnaire_version: Mapped[str] = mapped_column(Text, nullable=False)
    # User's own free-form text (Concept §4.2 — "系统给予最高尊重": stored and
    # read back verbatim, never filtered or rewritten). Encrypted at rest for
    # the same reason as holdings.notes — arbitrary user-authored prose about
    # their finances.
    free_text: Mapped[str | None] = mapped_column(EncryptedString)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
