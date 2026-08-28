from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.user import User


class UploadJob(Base):
    """Background job record for an async holdings-file parse (issue #77).

    /holdings/upload used to run holding_parser.parse() synchronously inside
    the request — up to 3 sequential LLM attempts that could take minutes,
    fragile against any interruption on the long-lived connection in between
    (confirmed: the backend completed and returned 200, but the client never
    saw it because the connection dropped first). The parse now runs in a
    Celery task against this row; the client polls GET
    /holdings/upload/{job_id} instead of holding one request open.

    `preview` currently has no automatic retention/cleanup — accepted for
    Ring 0 (single dev user, small row count); revisit before Ring 1 if
    `upload_jobs` grows unbounded (PR #82 review).

    A row stuck at `status="pending"` forever with `raw_text` still
    populated (worker hard-killed by Celery's `time_limit` before `finally`
    runs, an OS-level SIGKILL invisible to the task's own exception
    handling) is resolved two ways (issue #85, PR #88 review): primarily
    `_UploadJobRequest.on_timeout` (`app/tasks/holdings_tasks.py`), a
    `Task.Request` override that fires in the Celery MainProcess at the
    moment the hard-limit kill is detected — Celery's `task_revoked` signal
    does NOT fire for this automatic-timeout path (verified against the
    installed celery==5.6.3 source; it only fires from an explicit admin
    `revoke(terminate=True)`, which this app never calls), so a separate
    `task_revoked` handler exists only as a correct-but-narrow safety net
    for that explicit-revoke case, not for this one. Backstopped by a
    periodic sweeper task (`sweep_stale_upload_jobs`) for cases the
    on_timeout hook itself misses. The frontend's 120s poll deadline is a
    separate, pre-existing guard that stops the *user* from waiting forever
    regardless of server-side state.
    """

    __tablename__ = "upload_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # Extracted (not raw file bytes) holdings text, set by the router before
    # enqueueing and cleared by the Celery task once parse finishes (success
    # or failure) — PR #82 review: the task takes job_id only, never the raw
    # text itself, so a holdings file's plaintext content never becomes a
    # Celery/Redis broker message argument (Redis persists queued task
    # payloads until ack under task_acks_late — a new, avoidable surface for
    # sensitive data vs. the old request-scoped in-memory path).
    raw_text: Mapped[str | None] = mapped_column(Text)
    # UploadPreview.model_dump() once status="success". Never populated on
    # failure (see `error` instead).
    preview: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Purely for unit-of-work flush ordering (issue #129 B7) — not for query
    # navigation. See Holding.user's docstring comment for why this is
    # necessary at all.
    user: Mapped[User] = relationship(lazy="raise", passive_deletes=True)
