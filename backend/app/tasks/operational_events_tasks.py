"""Retention sweep for `operational_events` (issue #446, Design §4).

Same shape as `cache_tasks.sweep_stale_shared_intel_cache`: a daily beat task
that deletes rows past the confirmed 90-day retention window, retrying and
ops-alerting on exhaustion rather than failing silently. Scheduled at 05:00
UTC specifically (not ET like this codebase's other daily cadences — see
`app.tasks._beat_schedule`'s `cleanup-operational-events-daily` entry),
because retention here is a UTC-window policy (Design §4), not tied to any
US market session.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.operational_events import cleanup_expired_events
from app.services.email_sender import send_ops_alert
from app.tasks import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.operational_events_tasks.cleanup_operational_events",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def cleanup_operational_events(self: Any) -> dict[str, int]:
    """Delete `operational_events` rows older than the retention window, in
    bounded 1,000-row batches (`cleanup_expired_events`). Failure here never
    affects report generation — it only means the table keeps growing until
    the next successful sweep."""
    from app.core.database import SessionLocal

    session = SessionLocal()
    try:
        deleted = cleanup_expired_events(session)
        logger.info("cleanup_operational_events: deleted %d expired row(s)", deleted)
        return {"deleted": deleted}
    except Exception as exc:
        logger.exception("cleanup_operational_events: failed, scheduling retry")
        if self.request.retries >= self.max_retries:
            send_ops_alert(
                subject="[Portfonia] operational_events retention sweep FAILED",
                body=(
                    f"cleanup_operational_events failed after {self.max_retries} retries.\n\n"
                    f"error: {type(exc).__name__}: {exc}\n\n"
                    "Impact: operational_events rows older than the retention window are not "
                    "being cleaned up — the table will keep growing until this is fixed. Not a "
                    "correctness issue for reports. Check worker.log for the full traceback."
                ),
            )
        raise self.retry(exc=exc) from exc
    finally:
        session.close()
