"""Per-user macro coverage ledger (issue #440): what macro material was
ACTUALLY rendered in a report's §2, so a later report can revisit an open
question or a continuing condition instead of repeating the same explanation
from scratch or treating a continuing story as brand new.

Design (issue #440 Design §4/§5, owner decision 2026-09-12 — design-review
default leanings where a parameter was left open):
  - One row per (report, development_key): a report can cover several
    developments, each gets its own row, keyed by the LLM-supplied
    `development_key` for that argument.
  - Written ONLY for what the model actually put in the rendered §2 body
    (extracted from the structured sidecar block, see report_prompts.py's
    macro sidecar contract) — never for input candidates/themes that were
    considered and dropped. "A report with no macro prose creates no macro
    coverage" (Design §5).
  - Eligibility for continuity ("has this been covered before") is a
    READ-time policy, not a write-time one: `macro_coverage.py`'s
    `load_recent_macro_coverage` only reads rows whose `Report.status ==
    'success'` (success-available coverage eligibility, the accepted
    default for D4). Rows are still written for a `needs_review`/`failed`
    attempt's report_id for audit uniformity — they are simply never read
    back for continuity.
  - Regeneration replace semantics (Design §5 / Contract constraints
    "Regenerate after a topic was removed"): `persist_macro_coverage`
    deletes every existing row for `report_id` before inserting the
    freshly extracted set, so a topic dropped on `analyze` regeneration
    does not leave a stale row behind. This makes the write idempotent for
    every caller (fresh generation, the #61 resume-from-prior-attempt
    path, and `regenerate_report(mode="analyze")`), never an upsert.

`user_id`/`report_id` are both ON DELETE CASCADE: this is derived analysis
audit trail tied 1:1 to one report's lifecycle, not a financial record that
should block a purge (same class as `report_currency_changes` /
`portfolio_snapshot_*` — see those models' docstrings for the precedent).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ARRAY, CheckConstraint, ForeignKey, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Proposed in Design §4, accepted as-is (owner decision 2026-09-12): first
# explanation, an incremental revision building on a prior one, or a
# refreshed confirmation of a still-relevant condition with no new headline.
VALID_MACRO_COVERAGE_MODES = ("NEW", "UPDATE", "STATE_REVIEW")

# Design §4: prior explanation depth actually rendered for this development
# in this report — lets continuity distinguish "this reader already got the
# full anchor treatment" from "this was a one-line aside".
VALID_MACRO_DEPTH_TIERS = ("anchor", "update", "context")


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in sorted(values))
    return f"{column} IN ({quoted})"


class MacroCoverage(Base):
    """One development actually covered in one report's §2, for one user."""

    __tablename__ = "macro_coverage"
    __table_args__ = (
        UniqueConstraint(
            "report_id", "development_key", name="uq_macro_coverage_report_development"
        ),
        CheckConstraint(
            _in_list_sql("coverage_mode", VALID_MACRO_COVERAGE_MODES), name="coverage_mode"
        ),
        CheckConstraint(_in_list_sql("depth_tier", VALID_MACRO_DEPTH_TIERS), name="depth_tier"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    report_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Stable identity for "the same event or continuing question" across
    # reports. Model-supplied (see the macro sidecar contract in
    # report_prompts.py); normalized (lowercased/trimmed) before storage.
    # Exact-match only — no fuzzy/semantic merge in this slice (Design §4:
    # "normalization/merge rules pending"; a routine, revisitable choice,
    # not a silent invention of an unresolved product threshold).
    development_key: Mapped[str] = mapped_column(Text, nullable=False)
    theme_keys: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    # Evidence cutoff (the report's own period_end), NOT generated_at —
    # Design §4: "separate from generation time".
    as_of: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    coverage_mode: Mapped[str] = mapped_column(Text, nullable=False)
    depth_tier: Mapped[str] = mapped_column(Text, nullable=False)
    facts_covered: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'")
    )
    explanations_covered: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'")
    )
    open_questions: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'")
    )
    observables: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'")
    )
    affected_identifiers: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    # Audit aid only, per Design §4 — never the sole dedup identity (that is
    # `development_key`) and never re-injected into a later prompt verbatim
    # at full length (see macro_coverage.py's continuity-block excerpt cap).
    paragraph_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
