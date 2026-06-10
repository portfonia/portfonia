"""add session_node to reports, re-key dedup constraint

H-DEBT-1: report idempotency was keyed on (user_id, report_date, report_type) —
a calendar-day grain. A scheduled after-close run on the same day as an earlier
manual run was silently short-circuited (returned the manual run's row, no new
generation, no second email). session_node identifies WHICH trigger produced the
report (e.g. "manual" vs "after_close" for the M/W/F 16:30 ET cadence), so two
distinct triggers on the same calendar day no longer collide. A redelivered
Celery task still passes the same session_node + report_date, so the original
redelivery-dedup protection is preserved.

Existing rows predate this distinction and are backfilled to 'legacy'.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-06-09 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, Sequence[str], None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "reports",
        sa.Column("session_node", sa.Text(), nullable=False, server_default="legacy"),
    )
    op.alter_column("reports", "session_node", server_default=None)
    op.drop_constraint("uq_reports_user_date_type", "reports", type_="unique")
    op.create_unique_constraint(
        "uq_reports_user_date_type_session",
        "reports",
        ["user_id", "report_date", "report_type", "session_node"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_reports_user_date_type_session", "reports", type_="unique")
    op.create_unique_constraint(
        "uq_reports_user_date_type", "reports", ["user_id", "report_date", "report_type"]
    )
    op.drop_column("reports", "session_node")
