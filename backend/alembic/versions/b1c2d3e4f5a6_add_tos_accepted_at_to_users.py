"""add tos_accepted_at to users

Issue #220 (Profile page) / #221 (onboarding, not yet implemented in this
migration's originating PR). GET /me (issue #220) returns the full #221
response shape in one go per the Ring 1-Onboarding.md §6 coupling decision
("don't land a narrow schema first and widen it later") — this column is
that shape's one piece of new storage. Nullable, audit-only: existing users
get NULL (registered before any ToS gate existed), and #221's actual
ToS-acceptance write path is separate, later work. No backfill, no default.

Revision ID: b1c2d3e4f5a6
Revises: 9c56ac348d7d
Create Date: 2026-08-27

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: str | Sequence[str] | None = "9c56ac348d7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("tos_accepted_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "tos_accepted_at")
