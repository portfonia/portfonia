"""Token permission and holdings collection version.

Revision ID: d75300000001
Revises: d72900000001
"""

import sqlalchemy as sa
from alembic import op

revision = "d75300000001"
down_revision = "d72900000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "api_tokens",
        sa.Column(
            "permission",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'read_only'"),
        ),
    )
    # Bare names: naming_convention prefixes ck_<table>_.
    op.create_check_constraint(
        "permission",
        "api_tokens",
        "permission IN ('read_only', 'holdings_write')",
    )
    op.add_column(
        "users",
        sa.Column(
            "holdings_version",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_check_constraint(
        "holdings_version",
        "users",
        "holdings_version >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("holdings_version", "users", type_="check")
    op.drop_column("users", "holdings_version")
    op.drop_constraint("permission", "api_tokens", type_="check")
    op.drop_column("api_tokens", "permission")
