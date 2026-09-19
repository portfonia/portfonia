"""drop vigil_consumed_nonces

Issue #528 (#516 finding 14): the HMAC-signed compact public-nonce
issue/verify path is removed from services/vigil/tokens.py — single-use on
a public confirm is now marked on the existing `vigil_action_tokens.
confirmed_at` column, in the same transaction as the confirm side effects.
`vigil_consumed_nonces` has no remaining reader or writer, so (unlike
#527's `vigil_audit_events`, abandoned in place) this one is a real DROP
TABLE, per owner decision on #516 (2026-09-19).

Revision ID: 700aa7d9e13e
Revises: b7d1c4e8f2a3
Create Date: 2026-09-19

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "700aa7d9e13e"
down_revision: str | None = "b7d1c4e8f2a3"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("vigil_consumed_nonces")


def downgrade() -> None:
    op.create_table(
        "vigil_consumed_nonces",
        sa.Column("jti", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("jti", name=op.f("pk_vigil_consumed_nonces")),
    )
