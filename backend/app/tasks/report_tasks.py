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
) -> dict[str, Any]:
    """Generate an incremental report for every active user (changes since
    each user's own last report of this report_type).

    `report_type`/`session_node` come from the Celery Beat schedule entry
    (`app.tasks._build_report_schedule`), not hardcoded here — a new cadence
    (e.g. a Ring 1 weekly/monthly type) is a new beat table row, not a new
    task function. A missed run needs no catch-up: the next run's window is
    "since last report of this type", so it widens to cover the gap. On a
    batch-level failure (e.g. the active-user query itself fails, before any
    per-user isolation can apply) retries up to 3 times with a 5-minute
    cooldown.

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

    Multi-user fan-out (issue #128 A1): `active_user_ids` (SELECT DISTINCT
    user_id FROM holdings — design doc §1.5, no `users` table needed yet)
    replaces the pre-A1 single fixed-DEV_USER_ID call. Each user's
    `generate_report` call is wrapped in its own try/except: one user's
    failure is logged, ops-alerted, and does NOT stop or retry the batch —
    the remaining users still get their reports (design doc §3.3/UAT-3).
    Only a failure OUTSIDE the per-user loop (e.g. `active_user_ids` itself
    can't reach the DB) is a batch-level failure that retries via
    `self.retry`, matching the pre-A1 behavior for that class of error.
    `moves_cache`, shared across every user in this batch, is what makes
    `compute_global_moves()` run once per (period_start, period_end) window
    instead of once per user who happens to share it — see
    `window_data.detect_window_anomalies`.
    """
    # Imports are deferred so the module loads fast and avoids circular deps
    # when Celery first imports the task registry.
    from app.core.config import get_settings
    from app.core.database import SessionLocal
    from app.services.report_generator import generate_report
    from app.services.user_scope import active_user_ids
    from app.services.window_data import MovesCache

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
        user_ids = active_user_ids(session)
        if not user_ids:
            logger.info("generate_incremental_report: no active users, nothing to generate")
            return {"status": "no_active_users", "results": []}

        moves_cache: MovesCache = {}
        results: list[dict[str, str]] = []
        for user_id in user_ids:
            try:
                report = generate_report(
                    session,
                    report_type=report_type,
                    output_lang=get_settings().OUTPUT_LANG,
                    session_node=session_node,
                    user_id=user_id,
                    moves_cache=moves_cache,
                )
                logger.info(
                    "generate_incremental_report: complete for user %s — report_id=%s status=%s",
                    user_id,
                    report.id,
                    report.status,
                )
                if report.status == "needs_review":
                    send_ops_alert(
                        subject=f"[Portfonia] Report BLOCKED — compliance review {report.report_date}",
                        body=(
                            f"Report {report.id} ({report.report_date}, user {user_id}) was held "
                            f"for compliance review and was NOT emailed to the user.\n\n"
                            f"Check worker.log for the triggering terms.\n"
                            f"To rerun: POST /reports/{report.id}/regenerate?mode=analyze"
                        ),
                    )
                results.append(
                    {"user_id": str(user_id), "report_id": str(report.id), "status": report.status}
                )
            except Exception as exc:
                logger.exception(
                    "generate_incremental_report: failed for user %s — continuing with "
                    "remaining users in this batch",
                    user_id,
                )
                # generate_report's own except block already commits/rolls back around
                # its own failure; this is defense-in-depth for an error raised before
                # that block starts (e.g. the idempotency lookup itself), which would
                # otherwise leave the session's transaction aborted for the next user.
                session.rollback()
                send_ops_alert(
                    subject=f"[Portfonia] Report generation FAILED for one user ({report_type})",
                    body=(
                        f"generate_incremental_report failed for user {user_id} "
                        f"(report_type={report_type}, session_node={session_node}).\n\n"
                        f"error: {type(exc).__name__}: {exc}\n\n"
                        f"Other users in this batch were unaffected — this failure did not stop "
                        f"the batch. Check worker.log for the full traceback."
                    ),
                )
                results.append({"user_id": str(user_id), "status": "failed"})

        return {"status": "completed", "results": results}
    except Exception as exc:
        logger.exception("generate_incremental_report: batch failed, scheduling retry")
        if self.request.retries >= self.max_retries:
            send_ops_alert(
                subject="[Portfonia] Report generation batch FAILED — all retries exhausted",
                body=(
                    f"generate_incremental_report failed after {self.max_retries} retries, "
                    f"before per-user isolation could even apply (e.g. the active-user query "
                    f"itself failed).\n\n"
                    f"error: {type(exc).__name__}: {exc}\n\n"
                    f"Check worker.log for the full traceback."
                ),
            )
            create_bug_report(
                title=f"report generation batch failure: {type(exc).__name__}",
                body=(
                    f"## Incremental report generation batch exhausted all retries\n\n"
                    f"**Error:** `{type(exc).__name__}: {exc}`\n\n"
                    f"**Retries:** {self.max_retries}\n\n"
                    f"No reports were delivered for this scheduled run.\n\n"
                    f"**Investigate:** check `worker.log` for the full traceback."
                ),
                labels=["bug", "ops", "report"],
            )
        raise self.retry(exc=exc) from exc
    finally:
        session.close()
