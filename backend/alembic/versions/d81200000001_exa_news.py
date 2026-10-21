"""Allow shared Exa articles, links and usage (#812).

Revision ID: d81200000001
Revises: d79100000001
"""

from alembic import op

revision = "d81200000001"
down_revision = "d79100000001"
branch_labels = None
depends_on = None


def _constraints(exa: bool) -> None:
    for table, column, values in (
        ("intel_articles", "provider", "'tavily','parallel'"),
        ("paid_api_usage", "provider", "'tavily','parallel'"),
        ("intel_article_links", "role", "'mover','quiet','macro'"),
    ):
        name = op.f(f"ck_{table}_{column}")
        op.drop_constraint(name, table, type_="check")
        allowed = values + (",'exa'" if exa else "")
        op.create_check_constraint(name, table, f"{column} IN ({allowed})")


def upgrade() -> None:
    _constraints(True)


def downgrade() -> None:
    _constraints(False)
