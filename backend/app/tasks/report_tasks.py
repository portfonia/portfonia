"""Celery tasks for report generation (Stage H)."""

from __future__ import annotations

import logging
from typing import Any

from app.services.email_sender import send_ops_alert
from app.services.github_issues import create_bug_report
from app.tasks import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.report_tasks.generate_incremental_report",
    bind=True,
    max_retries=2,
    default_retry_delay=300,  # 5 min between retries
)
def generate_incremental_report(self: Any) -> dict[str, str]:
    """Generate the incremental report (changes since the user's last report).

    Scheduled by Celery Beat Mon/Wed/Fri at 16:30 ET. A missed run needs no
    catch-up: the next run's window is "since last report", so it widens to
    cover the gap. On failure retries up to 2 times with a 5-minute cooldown.

    `session_node="after_close"` (H-DEBT-1): identifies this cadence in the
    dedup key `(user_id, report_date, report_type, session_node)`, so an
    earlier same-day "manual" run does not short-circuit this run.
    """
    # Imports are deferred so the module loads fast and avoids circular deps
    # when Celery first imports the task registry.
    from app.core.config import get_settings
    from app.core.database import SessionLocal
    from app.services.report_generator import generate_report

    logger.info("generate_incremental_report: starting")
    session = SessionLocal()
    try:
        report = generate_report(
            session,
            report_type="incremental",
            output_lang=get_settings().OUTPUT_LANG,
            session_node="after_close",
        )
        logger.info(
            "generate_incremental_report: complete — report_id=%s status=%s",
            report.id,
            report.status,
        )
        if report.status == "needs_review":
            send_ops_alert(
                subject=f"[Portfonia] Report BLOCKED — compliance review {report.report_date}",
                body=(
                    f"Report {report.id} ({report.report_date}) was held for compliance review "
                    f"and was NOT emailed to the user.\n\n"
                    f"Check worker.log for the triggering terms.\n"
                    f"To rerun: POST /reports/{report.id}/regenerate?mode=analyze"
                ),
            )
        return {"report_id": str(report.id), "status": report.status}
    except Exception as exc:
        logger.exception("generate_incremental_report: failed, scheduling retry")
        if self.request.retries >= self.max_retries:
            send_ops_alert(
                subject="[Portfonia] Report generation FAILED — all retries exhausted",
                body=(
                    f"generate_incremental_report failed after {self.max_retries} retries.\n\n"
                    f"error: {type(exc).__name__}: {exc}\n\n"
                    f"Check worker.log for the full traceback."
                ),
            )
            create_bug_report(
                title=f"report generation failure: {type(exc).__name__}",
                body=(
                    f"## Incremental report generation exhausted all retries\n\n"
                    f"**Error:** `{type(exc).__name__}: {exc}`\n\n"
                    f"**Retries:** {self.max_retries}\n\n"
                    f"No report was delivered to the user for this scheduled run.\n\n"
                    f"**Investigate:** check `worker.log` for the full traceback."
                ),
                labels=["bug", "ops", "report"],
            )
        raise self.retry(exc=exc) from exc
    finally:
        session.close()
