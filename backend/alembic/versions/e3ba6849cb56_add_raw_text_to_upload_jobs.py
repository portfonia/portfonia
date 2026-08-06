"""add raw_text to upload_jobs

PR #82 review: the Celery task now takes job_id only, never the extracted
holdings text as a task argument — Redis (broker) persists queued task
payloads until ack under task_acks_late, a new plaintext-holdings surface
the old request-scoped in-memory path never had. The router writes the
extracted text here before enqueueing; the task reads it and clears it
(success or failure) once the parse attempt finishes.

Revision ID: e3ba6849cb56
Revises: 9d0e1f2a3b4c
Create Date: 2026-08-06 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e3ba6849cb56"
down_revision: Union[str, Sequence[str], None] = "9d0e1f2a3b4c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("upload_jobs", sa.Column("raw_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("upload_jobs", "raw_text")
