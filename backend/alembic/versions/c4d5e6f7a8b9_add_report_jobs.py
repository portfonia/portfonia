"""add report_jobs table

Issue #193: POST /reports/generate ran the full generation pipeline
synchronously and, through the frontend's Next.js `rewrites()` proxy, the
client got a plain-text 500 at ~30s while the backend went on to succeed.
Generation now runs in a Celery task against a row in this table; the client
polls GET /reports/jobs/{job_id} instead of holding one request open
(the holdings `upload_jobs` shape, issue #77).

Both FKs are ON DELETE CASCADE: a report_jobs row is a dependent operational
record of a trigger, never an audited financial row — so a user purge (or the
deletion of the report it points at) collects its jobs instead of needing an
extra explicit DELETE in `services/user_purge.purge_user` and a new field in
the admin purge response.

Revision ID: c4d5e6f7a8b9
Revises: 9d2f4b7c1e05
Create Date: 2026-09-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: str | Sequence[str] | None = "9d2f4b7c1e05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "report_jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("report_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'success', 'failed')", name="ck_report_jobs_status"
        ),
    )
    op.create_foreign_key(
        "fk_report_jobs_user_id_users",
        "report_jobs",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_report_jobs_report_id_reports",
        "report_jobs",
        "reports",
        ["report_id"],
        ["id"],
        ondelete="CASCADE",
    )
    # Polling reads one row by (user_id, id); user_id alone covers "this
    # user's recent jobs" if that's ever needed, same as upload_jobs.
    op.create_index("ix_report_jobs_user_id", "report_jobs", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_report_jobs_user_id", table_name="report_jobs")
    op.drop_table("report_jobs")
