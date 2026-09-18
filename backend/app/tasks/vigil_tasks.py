"""Vigil outbox dispatch + delivery-evidence poll (issues #456/#457).

Registered on the EXISTING Celery app/queue (app/tasks/__init__.py) — no new
worker, queue, or beat schedule mechanism. Dispatch processes at most
`dispatch.MAX_ROWS_PER_SWEEP` (5) due `vigil_outbox` rows; the delivery poll
issues at most five GET /emails/{id} calls for missing evidence at the
5/15/30-minute windows.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.database import SessionLocal
from app.services.vigil.delivery import run_delivery_poll_sweep
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


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.vigil_tasks.poll_vigil_delivery_task",
    bind=True,
    max_retries=0,
)
def poll_vigil_delivery_task(self: Any) -> None:
    """Bounded missing-evidence poll. No Celery retry — the 5/15/30-minute
    windows are computed from first_attempt_at on the next beat tick."""
    summary = run_delivery_poll_sweep(SessionLocal)
    if summary.polled:
        logger.info("vigil delivery poll: polled=%d", summary.polled)
