"""Vigil outbox dispatch task (issue #456, Vigil R0 P3.1).

Registered on the EXISTING Celery app/queue (app/tasks/__init__.py) — no new
worker, queue, or beat schedule mechanism. One task invocation processes at
most `dispatch.MAX_ROWS_PER_SWEEP` (5) due `vigil_outbox` rows; the periodic
beat schedule entry is what provides recovery if a specific enqueue is ever
lost (see services/vigil/dispatch.py's module docstring).
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.database import SessionLocal
from app.services.vigil.dispatch import run_outbox_dispatch_sweep
from app.tasks import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.vigil_tasks.dispatch_vigil_outbox_task",
    bind=True,
    max_retries=0,
)
def dispatch_vigil_outbox_task(self: Any) -> None:
    """No `self.retry` — a failed send is retried by the outbox's own
    `next_attempt_at` schedule on the next sweep, not by Celery's task
    retry mechanism (retrying at the Celery level would race the outbox's
    own lease/attempt bookkeeping)."""
    summary = run_outbox_dispatch_sweep(SessionLocal)
    if summary.sent or summary.expired:
        logger.info(
            "vigil outbox sweep: expired=%d leased=%d sent=%d",
            summary.expired,
            summary.leased,
            summary.sent,
        )
