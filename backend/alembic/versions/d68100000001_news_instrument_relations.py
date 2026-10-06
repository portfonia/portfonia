"""Mark related-company headline links on news_instruments (#681).

Downgrade deletes every related link before dropping the columns; direct links
are untouched.

Revision ID: d68100000001
Revises: d67500000001
"""

import sqlalchemy as sa

from alembic import op

revision: str = "d68100000001"
down_revision: str | None = "d67500000001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("news_instruments", sa.Column("relation", sa.Text(), nullable=True))
    op.add_column("news_instruments", sa.Column("related_to", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_news_instruments_relation"),
        "news_instruments",
        "relation IS NULL OR relation IN ('supplier', 'customer', 'competitor', 'input')",
    )
    op.create_check_constraint(
        op.f("ck_news_instruments_related_to"),
        "news_instruments",
        "(relation IS NULL) = (related_to IS NULL)",
    )


def downgrade() -> None:
    op.execute("DELETE FROM news_instruments WHERE relation IS NOT NULL")
    op.drop_constraint(op.f("ck_news_instruments_related_to"), "news_instruments", type_="check")
    op.drop_constraint(op.f("ck_news_instruments_relation"), "news_instruments", type_="check")
    op.drop_column("news_instruments", "related_to")
    op.drop_column("news_instruments", "relation")
