"""add operational_events

Issue #446: durable batch/attempt/stage operational-event store, replacing
the earlier "six report_inputs timings, no migration" contract (superseded
in the Design comment). See `app/models/operational_event.py` and
`app/core/operational_events.py` for the writer contract, and
`docs/mechanisms/capture-and-reporting.md`'s "Operational event log" entry
for the full design record.

`event_kind`/`outcome` CHECKs are a FROZEN SNAPSHOT of
`VALID_EVENT_KINDS`/`VALID_OUTCOMES` as of this migration's authoring date
(e3f4a5b6c7d8 precedent) — deliberately not imported live; widening the set
later is a new migration.

`user_id` is ON DELETE CASCADE (personal telemetry must not outlive the
user, Contract constraints #5); `report_id` is a plain correlation column,
NOT a foreign key — the report row an event correlates to may not be
committed yet, and this table must never wait on it.

Revision ID: b3c4d5e6f7a8
Revises: 7b5e371448f9
Create Date: 2026-09-13

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b3c4d5e6f7a8"
down_revision: str | None = "7b5e371448f9"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operational_events",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "schema_version",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("span_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_span_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_kind", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("elapsed_ms", sa.DOUBLE_PRECISION(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("report_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("dispatch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_operational_events")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_operational_events_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "event_kind IN ('context', 'dispatch', 'end', 'retry', 'skipped', 'start')",
            name=op.f("ck_operational_events_event_kind"),
        ),
        sa.CheckConstraint(
            "(outcome IS NULL) OR (outcome IN "
            "('degraded', 'failed', 'ok', 'retry_requested', 'skipped', 'unconfirmed'))",
            name=op.f("ck_operational_events_outcome"),
        ),
        sa.CheckConstraint(
            "(elapsed_ms IS NULL) OR (elapsed_ms >= 0 AND elapsed_ms = elapsed_ms "
            "AND elapsed_ms < 'Infinity')",
            name=op.f("ck_operational_events_elapsed_ms_finite_nonneg"),
        ),
    )
    op.create_index(
        "ix_operational_events_run_occurred",
        "operational_events",
        ["run_id", "occurred_at"],
    )
    op.create_index(
        "ix_operational_events_report_occurred",
        "operational_events",
        ["report_id", "occurred_at"],
    )
    op.create_index(
        "ix_operational_events_operation_occurred",
        "operational_events",
        ["operation", "occurred_at"],
    )
    op.create_index("ix_operational_events_occurred_at", "operational_events", ["occurred_at"])
    op.create_index("ix_operational_events_user_id", "operational_events", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_operational_events_user_id", table_name="operational_events")
    op.drop_index("ix_operational_events_occurred_at", table_name="operational_events")
    op.drop_index("ix_operational_events_operation_occurred", table_name="operational_events")
    op.drop_index("ix_operational_events_report_occurred", table_name="operational_events")
    op.drop_index("ix_operational_events_run_occurred", table_name="operational_events")
    op.drop_table("operational_events")
