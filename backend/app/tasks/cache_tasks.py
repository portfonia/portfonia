"""Daily Celery beat retention sweep for scheduled-intelligence records.

Runs `intel_records.sweep_intel` (issue #620/#639 retention: news and
accepted articles 30 days, collection and slot runs 90 days, paid-usage
ledger 400 days).

The task and Beat entry keep their original names from the shared-intel cache
sweep (issue #128 A2/A3). Issue #640 removed those caches but kept this task,
because it also carries the intelligence-record retention; renaming it would
only change the Beat schedule without changing behavior.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.services.email_sender import send_ops_alert
from app.tasks import celery_app

logger = logging.getLogger(__name__)


def _cleanup_expired(session: Session) -> dict[str, int]:
    """Delete expired intelligence records, commit, and return per-table
    delete counts. Split out from the Celery task so it is testable against a
    real db_session without mocking SessionLocal."""
    from app.services.intel_records import sweep_intel

    counts = sweep_intel(session, datetime.now(UTC))
    session.commit()
    return counts


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.cache_tasks.sweep_stale_shared_intel_cache",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def sweep_stale_shared_intel_cache(self: Any) -> dict[str, int]:
    """Daily backstop: delete expired intelligence records.

    Retry + ops-alert on exhaustion (round 2 review finding): a silent sweep
    outage lets the tables grow without bound, exactly the failure mode this
    task exists to prevent. Matches backup_database_task's pattern.
    """
    from app.core.database import SessionLocal

    session = SessionLocal()
    try:
        result = _cleanup_expired(session)
        logger.info("sweep_stale_shared_intel_cache: deleted %s", result)
        return result
    except Exception as exc:
        logger.exception("sweep_stale_shared_intel_cache: failed, scheduling retry")
        if self.request.retries >= self.max_retries:
            send_ops_alert(
                subject="[Portfonia] intelligence retention sweep FAILED — retries exhausted",
                body=(
                    f"sweep_stale_shared_intel_cache failed after {self.max_retries} retries.\n\n"
                    f"error: {type(exc).__name__}: {exc}\n\n"
                    "Impact: expired news, article, collection-run and slot-run rows are "
                    "not being deleted and will keep growing until this is fixed. Not a "
                    "correctness issue for reports, but check disk usage if this persists. "
                    "Check worker.log for the full traceback."
                ),
                severity="ALERT",
            )
        raise self.retry(exc=exc) from exc
    finally:
        session.close()
