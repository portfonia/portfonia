"""One saved Jade reference portfolio per user.

Revision ID: d72900000001
Revises: d72000000001
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d72900000001"
down_revision = "d72000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("jade_benchmark", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "jade_benchmark")
