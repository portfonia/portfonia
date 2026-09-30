"""Allow Traditional Chinese report language.

Revision ID: d58200000001
Revises: c57800000001
"""
from alembic import op

revision = "d58200000001"
down_revision = "c57800000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("locale", "users", type_="check")
    op.create_check_constraint("locale", "users", "locale IN ('en', 'zh', 'zh-Hant')")


def downgrade() -> None:
    op.execute("UPDATE users SET locale = 'zh' WHERE locale = 'zh-Hant'")
    op.drop_constraint("locale", "users", type_="check")
    op.create_check_constraint("locale", "users", "locale IN ('en', 'zh')")
