"""Celery task for the daily Postgres -> OCI Object Storage backup (issue #106)."""

from __future__ import annotations

import logging
from typing import Any

from app.services.email_sender import send_ops_alert
from app.services.github_issues import create_bug_report
from app.tasks import celery_app

logger = logging.getLogger(__name__)


def _backup_failed(exc: BaseException) -> None:
    """Send ops alert + create GitHub issue when the backup task exhausts retries."""
    send_ops_alert(
        subject="[Portfonia] database backup FAILED",
        body=(
            "backup_database_task exhausted all retries.\n\n"
            f"error: {type(exc).__name__}: {exc}\n\n"
            "Impact: no fresh backup uploaded today — production DB has one "
            "fewer day of restore coverage. Check worker.log for the full traceback."
        ),
    )
    create_bug_report(
        title="database backup failure",
        body=(
            "## backup_database_task exhausted retries\n\n"
            f"**Error:** `{type(exc).__name__}: {exc}`\n\n"
            "**Impact:** today's backup did not upload to OCI Object Storage — "
            "restore coverage for this day is missing.\n\n"
            "**Investigate:** check `worker.log` for the full traceback."
        ),
        labels=["bug", "ops", "backup"],
    )


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.backup_tasks.backup_database_task",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
    time_limit=900,
    soft_time_limit=870,
)
def backup_database_task(self: Any) -> dict[str, str | None]:
    from app.services.db_backup import backup_database

    try:
        object_name = backup_database()
        return {"object_name": object_name}
    except Exception as exc:
        logger.exception("backup_database_task: failed, scheduling retry")
        if self.request.retries >= self.max_retries:
            _backup_failed(exc)
        raise self.retry(exc=exc) from exc
