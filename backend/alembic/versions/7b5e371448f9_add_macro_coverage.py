"""add macro_coverage

Issue #440: per-user ledger of macro developments actually rendered in a
report's §2, keyed by (report_id, development_key). Read back (success-only,
bounded lookback) by report_generator.py/report_prompts.py to give the next
report continuity — what was already explained, what question was left open
— instead of repeating or silently dropping a continuing story.

`user_id`/`report_id` are both ON DELETE CASCADE: derived analysis audit
trail tied to one report's lifecycle, not a financial record that should
block a purge (f5a6b7c8d9e0 report_currency_changes precedent).

`coverage_mode`/`depth_tier` CHECKs mirror
app.models.macro_coverage.VALID_MACRO_COVERAGE_MODES /
VALID_MACRO_DEPTH_TIERS as of this migration's authoring date —
deliberately NOT imported live (f3a4b5c6d7e8 precedent). Widening the set
later is a new migration.

Revision ID: 7b5e371448f9
Revises: 94a25755ea05
Create Date: 2026-09-12

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "7b5e371448f9"
down_revision: str | Sequence[str] | None = "94a25755ea05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen snapshot — see module docstring. Do not import live constants here.
_COVERAGE_MODES = ("NEW", "UPDATE", "STATE_REVIEW")
_DEPTH_TIERS = ("anchor", "update", "context")


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    op.create_table(
        "macro_coverage",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("development_key", sa.Text(), nullable=False),
        sa.Column(
            "theme_keys",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("as_of", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("coverage_mode", sa.Text(), nullable=False),
        sa.Column("depth_tier", sa.Text(), nullable=False),
        sa.Column(
            "facts_covered", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'")
        ),
        sa.Column(
            "explanations_covered",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "open_questions", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'")
        ),
        sa.Column(
            "observables", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'")
        ),
        sa.Column(
            "affected_identifiers",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("paragraph_text", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "report_id", "development_key", name="uq_macro_coverage_report_development"
        ),
        sa.CheckConstraint(_in_list_sql("coverage_mode", _COVERAGE_MODES), name="coverage_mode"),
        sa.CheckConstraint(_in_list_sql("depth_tier", _DEPTH_TIERS), name="depth_tier"),
    )
    op.create_foreign_key(
        "fk_macro_coverage_user_id_users",
        "macro_coverage",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_macro_coverage_report_id_reports",
        "macro_coverage",
        "reports",
        ["report_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_macro_coverage_user_id", "macro_coverage", ["user_id"])
    op.create_index("ix_macro_coverage_report_id", "macro_coverage", ["report_id"])
    # Continuity read path: most-recent-per-development_key for a user,
    # excluding the report being (re)generated.
    op.create_index(
        "ix_macro_coverage_user_development_asof",
        "macro_coverage",
        ["user_id", "development_key", "as_of"],
    )


def downgrade() -> None:
    op.drop_index("ix_macro_coverage_user_development_asof", table_name="macro_coverage")
    op.drop_index("ix_macro_coverage_report_id", table_name="macro_coverage")
    op.drop_index("ix_macro_coverage_user_id", table_name="macro_coverage")
    op.drop_table("macro_coverage")
