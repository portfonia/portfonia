"""Celery tasks for report generation (Stage H)."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, NamedTuple

from app.core.timezones import ET
from app.services.email_sender import send_ops_alert
from app.services.github_issues import create_bug_report
from app.tasks import celery_app

logger = logging.getLogger(__name__)


class _Recipient(NamedTuple):
    """Snapshot of the two `User` fields the fan-out loop needs (issue
    #308, blacktomb42 PR #309 round-1 review) — taken ONCE, immediately
    after `active_users()` returns, before any `generate_report` call in
    this loop can `session.commit()`.

    `expire_on_commit` defaults to True on every `Session` in this
    codebase (including the real one `db_session` builds for tests), so a
    live ORM `User` object's attribute read on a LATER loop iteration,
    after an EARLIER iteration's commit already expired every object on
    this session, would trigger an implicit refresh SELECT on the same
    session mid-batch — the same class of interleaved-query hang already
    diagnosed and fixed once for `active_users` itself (see its
    docstring). A plain NamedTuple can never expire, so the loop body
    below reads only this snapshot, never a live `User` attribute.
    """

    user_id: uuid.UUID
    locale: str


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
    cadence: str = "mwf",
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

    `cadence` (issue #191): which `users.report_cadence` batch this run
    serves — passed through to `active_users`, which also decides per
    cadence whether the holdings gate applies (loosened for `weekly` only,
    see `app.services.user_scope`).

    Multi-user fan-out (issue #128 A1): `active_users` (active `users`
    rows on this cadence, gated by holdings per-cadence) replaces the pre-A1
    single fixed-dev-user call. Each user's
    `generate_report` call is wrapped in its own try/except: one user's
    failure is logged, ops-alerted, and does NOT stop or retry the batch —
    the remaining users still get their reports (design doc §3.3/UAT-3).
    Only a failure OUTSIDE the per-user loop (e.g. `active_users` itself
    can't reach the DB) is a batch-level failure that retries via
    `self.retry`, matching the pre-A1 behavior for that class of error.
    `moves_cache`, shared across every user in this batch, is what makes
    `compute_global_moves()` run once per (period_start, period_end) window
    instead of once per user who happens to share it — see
    `window_data.detect_window_anomalies`.
    """
    # Imports are deferred so the module loads fast and avoids circular deps
    # when Celery first imports the task registry.
    from app.core.database import SessionLocal
    from app.services.report_generator import generate_report
    from app.services.user_scope import active_users
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
        # Issue #308: full User rows, not just ids — each recipient's own
        # locale (report language) rides along. Snapshotted into `_Recipient`
        # NamedTuples immediately (see its docstring) — the loop below never
        # touches a live `User` attribute, only this snapshot.
        users = active_users(session, cadence)
        if not users:
            logger.info("generate_incremental_report: no active users, nothing to generate")
            return {"status": "no_active_users", "results": []}
        recipients = [_Recipient(user_id=u.id, locale=u.locale) for u in users]

        moves_cache: MovesCache = {}
        # Stamped ONCE for the whole batch (PR #151 review): moves_cache is keyed
        # on the exact (period_start, period_end) tuple, and generate_report
        # stamps a fresh row's period_end from `now`. Without a shared `now`,
        # each user's independent datetime.now() call would land microseconds
        # apart, the cache key would never collide across users, and
        # compute_global_moves would silently run once per user again — passing
        # the same moves_cache dict object alone does not make the cache hit.
        batch_now = datetime.now(tz=UTC)
        results: list[dict[str, str]] = []
        for index, recipient in enumerate(recipients):
            user_id = recipient.user_id
            try:
                report = generate_report(
                    session,
                    report_type=report_type,
                    # Issue #308: this recipient's own report language, not
                    # a shared Settings.OUTPUT_LANG default for the whole
                    # batch — that would defeat the entire point of a
                    # per-user setting for scheduled (not just self-service)
                    # reports. Read off the snapshot, not a live `User`
                    # attribute (see `_Recipient`'s docstring).
                    output_lang=recipient.locale,
                    session_node=session_node,
                    user_id=user_id,
                    moves_cache=moves_cache,
                    now=batch_now,
                    # Issue #128 A4: how many users (including this one) are
                    # still to be served, so the shared daily caps can be
                    # sliced fairly instead of first-come-first-served — see
                    # shared_budget.py. Derived from the batch POSITION, not
                    # from how many succeeded: a failed user still consumed
                    # its turn, and the countdown must stay monotonic so
                    # later users neither over- nor under-claim.
                    users_remaining=len(recipients) - index,
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

        if all(r["status"] == "failed" for r in results):
            # PR #151 review: per-user isolation must not come at the cost of
            # dropping the task's retry contract when EVERY user failed — today's
            # production has exactly one active user, so this is functionally the
            # pre-A1 single-user failure path, and it must still get the 3x /
            # 5-minute Celery retry rather than silently reporting "completed"
            # with every user marked failed. A retry re-fetches active_user_ids
            # and re-attempts each one; generate_report's own idempotency check
            # makes re-attempting an already-succeeded user's report a cheap
            # no-op, so this is safe even for a batch with a mix that later
            # turns "all failed" on a subsequent retry attempt.
            raise RuntimeError(
                f"generate_incremental_report: all {len(results)} user(s) failed in this batch"
            )

        return {"status": "completed", "results": results}
    except Exception as exc:
        logger.exception("generate_incremental_report: batch failed, scheduling retry")
        if self.request.retries >= self.max_retries:
            send_ops_alert(
                subject="[Portfonia] Report generation batch FAILED — all retries exhausted",
                body=(
                    f"generate_incremental_report failed after {self.max_retries} retries.\n\n"
                    f"error: {type(exc).__name__}: {exc}\n\n"
                    f"This can happen before per-user isolation even applies (e.g. the "
                    f"active-user query itself failed), or because every user's generation "
                    f"failed independently within the same run — check the per-user ops "
                    f"alerts already sent for this run's individual failures.\n\n"
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
