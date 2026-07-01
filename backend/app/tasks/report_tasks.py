"""Celery tasks for report generation (Stage H)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.core.timezones import ET
from app.services.email_sender import send_ops_alert
from app.services.github_issues import create_bug_report
from app.tasks import celery_app

logger = logging.getLogger(__name__)

# How late (minutes) a scheduled run may fire after its intended crontab time
# before it's treated as a Beat-downtime catch-up rather than an on-time run.
# Generous enough to absorb worker-pool contention / clock skew, tight enough
# to catch "Beat was down for hours/days and just came back" (issue #71).
_SCHEDULE_STALENESS_TOLERANCE_MINUTES = 30


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.report_tasks.generate_incremental_report",
    bind=True,
    max_retries=3,
    default_retry_delay=300,  # 5 min between retries
)
def generate_incremental_report(
    self: Any,
    report_type: str = "incremental",
    session_node: str = "after_close",
    trigger_hour: int | None = None,
    trigger_minute: int | None = None,
) -> dict[str, str]:
    """Generate an incremental report (changes since the user's last report of
    this report_type).

    `report_type`/`session_node` come from the Celery Beat schedule entry
    (`app.tasks._build_report_schedule`), not hardcoded here — a new cadence
    (e.g. a Ring 1 weekly/monthly type) is a new beat table row, not a new
    task function. A missed run needs no catch-up: the next run's window is
    "since last report of this type", so it widens to cover the gap. On
    failure retries up to 3 times with a 5-minute cooldown.

    `session_node` (H-DEBT-1): identifies this cadence in the dedup key
    `(user_id, report_date, report_type, session_node)`, so an earlier
    same-day "manual" run does not short-circuit this run.

    `trigger_hour`/`trigger_minute` (issue #71): the ET hour/minute this
    cadence is scheduled for, passed by Beat's schedule builder. Celery Beat's
    PersistentScheduler fires a missed crontab tick immediately once it comes
    back up rather than skipping it (e.g. the dev machine rebooted and Beat
    sat dead for days) — if the actual invocation clock time is far from the
    intended one, this is that catch-up, not an on-time run, and must NOT
    silently generate + email a report. `None` (manual trigger / tests) skips
    the check entirely.
    """
    # Imports are deferred so the module loads fast and avoids circular deps
    # when Celery first imports the task registry.
    from app.core.config import get_settings
    from app.core.database import SessionLocal
    from app.services.report_generator import generate_report

    if trigger_hour is not None and trigger_minute is not None:
        now_et = datetime.now(ET)
        scheduled_today = now_et.replace(
            hour=trigger_hour, minute=trigger_minute, second=0, microsecond=0
        )
        late_minutes = (now_et - scheduled_today).total_seconds() / 60
        if late_minutes > _SCHEDULE_STALENESS_TOLERANCE_MINUTES:
            logger.warning(
                "generate_incremental_report: skipping stale Beat catch-up "
                "(report_type=%s session_node=%s scheduled=%02d:%02d ET, "
                "fired %.0f min late — Beat was likely down)",
                report_type,
                session_node,
                trigger_hour,
                trigger_minute,
                late_minutes,
            )
            send_ops_alert(
                subject=f"[Portfonia] Scheduled report SKIPPED — Beat catch-up ({report_type})",
                body=(
                    f"A scheduled '{session_node}' {report_type} report fired "
                    f"{late_minutes:.0f} minutes after its intended {trigger_hour:02d}:"
                    f"{trigger_minute:02d} ET time. This means Celery Beat was down and "
                    f"just came back online, replaying the missed tick.\n\n"
                    f"The report was skipped — no report was generated or emailed. "
                    f"Restart Beat if it isn't already running, and if the user should "
                    f"still receive a report for this window, trigger one manually."
                ),
            )
            return {"status": "skipped_stale_trigger"}

    logger.info(
        "generate_incremental_report: starting (report_type=%s session_node=%s)",
        report_type,
        session_node,
    )
    session = SessionLocal()
    try:
        report = generate_report(
            session,
            report_type=report_type,
            output_lang=get_settings().OUTPUT_LANG,
            session_node=session_node,
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
