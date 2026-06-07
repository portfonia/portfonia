"""Celery tasks for report generation (Stage H)."""

from __future__ import annotations

import logging
from typing import Any

from app.tasks import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.report_tasks.generate_weekly_report",
    bind=True,
    max_retries=2,
    default_retry_delay=300,  # 5 min between retries
)
def generate_weekly_report(self: Any) -> dict[str, str]:
    """Generate the weekly intelligence report and send it by email.

    Scheduled by Celery Beat every Friday at 16:30 ET.
    On failure retries up to 2 times with a 5-minute cooldown.
    """
    # Imports are deferred so the module loads fast and avoids circular deps
    # when Celery first imports the task registry.
    from app.core.database import SessionLocal
    from app.services.report_generator import generate_report

    logger.info("generate_weekly_report: starting")
    session = SessionLocal()
    try:
        report = generate_report(session)
        logger.info(
            "generate_weekly_report: complete — report_id=%s status=%s",
            report.id,
            report.status,
        )
        return {"report_id": str(report.id), "status": report.status}
    except Exception as exc:
        logger.exception("generate_weekly_report: failed, scheduling retry")
        raise self.retry(exc=exc) from exc
    finally:
        session.close()
