"""add market and position to holdings

market: user-declared market bucket (US / HK / A-Share / Other), preserved from
the upload instead of re-derived from the ticker. position: the row's order in
the uploaded file, so reports can be grouped/sorted to match what the user typed.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-07 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("holdings", sa.Column("market", sa.Text(), nullable=True))
    op.add_column("holdings", sa.Column("position", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("holdings", "position")
    op.drop_column("holdings", "market")
