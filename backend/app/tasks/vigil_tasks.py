"""Vigil outbox dispatch + cycle scan (issue #456; #457's bounded poller
removed by issue #526, #516 finding 3).

Registered on the EXISTING Celery app/queue (app/tasks/__init__.py) — no new
worker, queue, or beat schedule mechanism. Dispatch processes at most
`dispatch.MAX_ROWS_PER_SWEEP` (5) due `vigil_outbox` rows. Delivery evidence
now arrives via webhook only (`app.services.vigil.delivery.
ingest_verified_webhook`) plus hold-on-missing-evidence in the cycle scan
below — the bounded 5/15/30-minute GET /emails/{id} poll a second,
provider-probing evidence path beside the webhook was removed rather than
kept as a "backup": a single-process, no-SLA mechanism doesn't need two
delivery-evidence paths to diverge from each other.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.database import SessionLocal
from app.services.vigil.cycles import run_cycle_scan
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
    name="app.tasks.vigil_tasks.scan_vigil_cycles_task",
    bind=True,
    max_retries=0,
)
def scan_vigil_cycles_task(self: Any) -> None:
    """Bounded cycle scan on the existing worker. No HTTP inside the
    scan transaction; heartbeat is committed with the work.
    """
    session = SessionLocal()
    try:
        run_cycle_scan(session)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
