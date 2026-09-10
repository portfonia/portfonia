from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Text, func, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.report import Report
from app.models.user import User


class ReportJob(Base):
    """Background job record for one on-demand report generation (issue #193).

    POST /reports/generate used to run the whole pipeline — Pass 1, shared
    intel, Pass 2, render, email; routinely minutes for a realistic book —
    inside the request. Through the frontend's Next.js `rewrites()` proxy the
    client got a plain-text 500 at ~30s (`Failed to proxy … ECONNRESET`)
    while the backend went on to succeed, so the caller had no honest signal
    at all. Generation now runs in a Celery task against this row and the
    client polls `GET /reports/jobs/{job_id}` instead of holding one request
    open, mirroring the holdings upload job (issue #77).

    `report_id` stays null until a run produces a report row; `error` carries
    the failure detail the sync endpoint used to translate into a 502. Unlike
    `upload_jobs` there is no stale-pending sweeper and no Celery time_limit:
    those exist for a 45s parse SLA, while report generation has no bounded
    runtime to sweep against — a worker hard-killed mid-run leaves the row
    pending rather than being falsely failed, and the client's own poll
    deadline reports that honestly.
    """

    __tablename__ = "report_jobs"
    # Raw name: Base's naming convention expands this to
    # `ck_report_jobs_status`, which is the name the migration creates. Passing
    # the expanded name here would double the prefix.
    __table_args__ = (CheckConstraint("status IN ('pending', 'success', 'failed')", name="status"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    report_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reports.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Not for query-time navigation — declared so the unit of work knows a
    # report_jobs row must be inserted after the `users`/`reports` rows it
    # references (see Holding.user's docstring comment for the full rationale).
    user: Mapped[User] = relationship(lazy="raise", passive_deletes=True)
    report: Mapped[Report | None] = relationship(lazy="raise", passive_deletes=True)
