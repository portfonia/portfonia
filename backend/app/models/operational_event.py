"""Generic durable operational-event store (issue #446).

`OperationalEvent` is a report-independent record of runtime evidence —
batch/task dispatch, attempt (run) start/end, named stage boundaries within
an attempt, and the outcome of external calls — written by
`app.core.operational_events` through its own short-lived connection, never
through a caller's business `Session`. See that module's docstring for the
writer contract (fail-open, independent transactions, sink-disable-on-error)
and `docs/mechanisms/capture-and-reporting.md`'s "Operational event log"
entry for the full design record.

Deliberately NOT a FK target for `report_id`: the report row this event
correlates to may not be committed yet (a killed process before its first
commit, a business rollback) — waiting on it would couple an independent
observability write to an in-flight business transaction, exactly what this
table exists to avoid. `report_id` is correlation-only.

`user_id` IS a real FK (`ON DELETE CASCADE`): personal telemetry must not
outlive the user (Contract constraints §5) — a purge must not leave orphaned
per-user event rows, even ones written by an independent connection that
raced the purge transaction.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DOUBLE_PRECISION,
    CheckConstraint,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# event_kind values (Design §1). "dispatch" is a publication-time record (no
# run exists yet); "start"/"end" bracket a run or a span; "skipped" is a
# one-shot non-entry (no start/end pair); "retry" records an observed
# Celery-level retry/redelivery; "context" attaches report_id/user_id to an
# already-open run once known.
VALID_EVENT_KINDS = ("dispatch", "start", "end", "skipped", "retry", "context")

# outcome values (Design §3 "Every entered span has ... one terminal end").
# Column is nullable — "dispatch"/"context" events carry no outcome.
VALID_OUTCOMES = ("ok", "failed", "degraded", "unconfirmed", "skipped", "retry_requested")

SCHEMA_VERSION = 1


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in sorted(values))
    return f"{column} IN ({quoted})"


class OperationalEvent(Base):
    """One row per dispatch/start/end/skipped/retry/context event.

    `event_id` is application-generated so a redelivered write is an
    idempotent no-op (`INSERT ... ON CONFLICT (event_id) DO NOTHING` — see
    the writer) rather than a duplicate row; `run_id`/`span_id` are what
    actually distinguish two invocations, never `event_id` reuse.
    """

    __tablename__ = "operational_events"
    __table_args__ = (
        CheckConstraint(_in_list_sql("event_kind", VALID_EVENT_KINDS), name="event_kind"),
        CheckConstraint(
            f"(outcome IS NULL) OR {_in_list_sql('outcome', VALID_OUTCOMES)}", name="outcome"
        ),
        CheckConstraint(
            "(elapsed_ms IS NULL) OR (elapsed_ms >= 0 AND elapsed_ms = elapsed_ms "
            "AND elapsed_ms < 'Infinity')",
            name="elapsed_ms_finite_nonneg",
        ),
        Index("ix_operational_events_run_occurred", "run_id", "occurred_at"),
        Index("ix_operational_events_report_occurred", "report_id", "occurred_at"),
        Index("ix_operational_events_operation_occurred", "operation", "occurred_at"),
        Index("ix_operational_events_occurred_at", "occurred_at"),
        Index("ix_operational_events_user_id", "user_id"),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    schema_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text(str(SCHEMA_VERSION))
    )
    occurred_at: Mapped[datetime] = mapped_column(nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    span_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    parent_span_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    event_kind: Mapped[str] = mapped_column(Text, nullable=False)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    elapsed_ms: Mapped[float | None] = mapped_column(DOUBLE_PRECISION)
    outcome: Mapped[str | None] = mapped_column(Text)
    reason_code: Mapped[str | None] = mapped_column(Text)
    # No relationship()/ORM navigation on purpose — this table is written
    # through its own connection (app.core.operational_events), never
    # through a business Session, so an ORM relationship here would invite
    # exactly the cross-session use this design forbids.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    report_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    task_id: Mapped[str | None] = mapped_column(Text)
    dispatch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    attributes: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
