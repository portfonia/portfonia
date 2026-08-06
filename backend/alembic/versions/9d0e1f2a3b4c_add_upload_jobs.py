"""add upload_jobs table

Issue #77: /holdings/upload ran holding_parser.parse() synchronously inside
the request (up to 3 sequential LLM attempts, observed taking ~5 minutes) —
fragile against any interruption on the long-lived connection. The parse now
runs in a Celery task against a row in this table; the client polls
GET /holdings/upload/{job_id} instead of holding one request open.

Revision ID: 9d0e1f2a3b4c
Revises: 8c9d0e1f2a3b
Create Date: 2026-08-06 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9d0e1f2a3b4c"
down_revision: Union[str, Sequence[str], None] = "8c9d0e1f2a3b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "upload_jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("preview", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
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
    )
    # Polling reads by (user_id, id); user_id alone covers "list my recent
    # jobs" if that's ever needed.
    op.create_index("ix_upload_jobs_user_id", "upload_jobs", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_upload_jobs_user_id", table_name="upload_jobs")
    op.drop_table("upload_jobs")
