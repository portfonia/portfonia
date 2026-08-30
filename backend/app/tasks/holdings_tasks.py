"""Celery task for async holdings-file parsing (issue #77)."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from celery.signals import task_revoked  # type: ignore[import-untyped]
from celery.worker.request import Request as _CeleryRequest  # type: ignore[import-untyped]
from sqlalchemy import delete
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.services.email_sender import send_ops_alert
from app.tasks import celery_app

logger = logging.getLogger(__name__)

# Product-level SLA (issue #85): a single upload interaction with no status
# update should not run past this many seconds before resolving to a
# terminal state. Real-world latency on STRUCTURED_LLM_MODEL is ~11-14s for
# a 30-row file (issue #84/#86), so 45s already has headroom over 2 back-to-
# back successful attempts — it's meant to bound a hang, not a normal run.
_SLA_SECONDS = 45


class _UploadJobRequest(_CeleryRequest):  # type: ignore[misc]
    """Resolves the UploadJob row from inside Celery's own hard-timeout
    handling, in the MainProcess, at the moment the kill is detected
    (issue #85 PR #88 review).

    Verified against the installed celery==5.6.3 source
    (celery/worker/request.py): the hard time_limit path calls
    Request.on_timeout(soft=False, ...), which only logs and writes to
    Celery's own result backend (Redis) — it never sends task_revoked.
    task_revoked is only emitted by Request.terminate(), which fires from an
    explicit admin `revoke(terminate=True)` control command, not
    automatically from a time_limit kill. That made the task_revoked handler
    below dead code for the exact SIGKILL scenario issue #85 is about — only
    the sweeper (tens of seconds later) was ever actually resolving those
    rows, silently missing the "fires as soon as the kill happens" goal.

    Task.Request is Celery's documented per-task customization point
    (celery/app/task.py's Task.Request default, resolved per-task by
    celery/worker/strategy.py via `symbol_by_name(task.Request)`) — this
    overrides it for parse_holdings_upload only, wired via the `Request=`
    kwarg on its @celery_app.task(...) decorator below.
    """

    def on_timeout(self, soft: bool, timeout: float) -> None:
        super().on_timeout(soft, timeout)
        if soft:
            # The task's own except/finally already handles this case (it
            # catches SoftTimeLimitExceeded and marks the job failed itself)
            # — nothing extra needed here.
            return
        job_id = self.args[0] if self.args else self.kwargs.get("job_id")
        if not job_id:
            logger.error(
                "on_timeout hard-limit hook: no job_id in args=%r kwargs=%r", self.args, self.kwargs
            )
            return
        _resolve_pending_job_as_failed(
            job_id=str(job_id),
            error=f"Worker hit the hard time_limit ({timeout}s) before it could record a result.",
            log_context="on_timeout hard-limit hook",
        )


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.holdings_tasks.parse_holdings_upload",
    Request=_UploadJobRequest,
    # soft_time_limit raises SoftTimeLimitExceeded inside the task (caught by
    # the broad except below, so the job still gets marked failed with a
    # specific error) — this is the preferred path, since it runs the task's
    # own cleanup, and it's given nearly the full SLA (only a 2s gap to the
    # hard kill) so it doesn't cut off a legitimate two-attempt run (2 x 20s
    # client timeout, holding_parser.py) before either attempt gets to fail
    # on its own terms (PR #88 review). time_limit is Celery's unconditional
    # SIGKILL if soft didn't get a chance to run (e.g. the hang is in a C
    # extension or the process is otherwise wedged); that kill is an OS
    # signal, invisible to this task's own except/finally, so it's
    # _UploadJobRequest.on_timeout above — not this task's code — that
    # resolves the job in that case.
    soft_time_limit=_SLA_SECONDS - 2,
    time_limit=_SLA_SECONDS,
)
def parse_holdings_upload(job_id: str) -> dict[str, str]:
    """Parse an uploaded holdings file in the background and write the result
    onto the UploadJob row the client is polling.

    Takes job_id only, not the extracted text — the text was written onto
    the job row by the router before enqueueing and is read from there. This
    keeps a holdings file's plaintext content out of the Celery/Redis broker
    message itself (Redis persists queued task payloads until ack under
    task_acks_late — PR #82 review).

    No Celery-level retry: holding_parser.parse() already retries internally
    (2 attempts — issue #78, simplified from 3 in issue #84). Stacking a
    Celery retry on top would only add unbounded extra latency to an
    interactive, user-facing action; if both internal attempts fail, this
    records the failure and the user can just re-upload.

    Idempotent against Celery redelivery (PR #82 second review): the app
    sets task_acks_late=True globally, so a worker that dies after this
    task's own commit but before it acks the message gets the same message
    redelivered. A naive second run would find raw_text already cleared by
    the first (successful) run and misinterpret that as "nothing to parse",
    overwriting a real success with a false failure and losing the preview.
    A job already in a terminal state (success/failed) is left untouched.
    """
    from app.core.database import SessionLocal
    from app.models.upload_job import UploadJob
    from app.services import holding_parser

    session = SessionLocal()
    try:
        job = session.get(UploadJob, uuid.UUID(job_id))
        if job is None:
            logger.error("parse_holdings_upload: job %s not found", job_id)
            return {"status": "job_not_found"}
        if job.status != "pending":
            logger.info(
                "parse_holdings_upload: job %s already %s — redelivery, skipping",
                job_id,
                job.status,
            )
            return {"job_id": job_id, "status": job.status}
        text = job.raw_text
        try:
            if not text:
                raise RuntimeError("No extracted text found for this upload job.")
            preview = holding_parser.parse(text)
            job.status = "success"
            job.preview = preview.model_dump(mode="json")
        except RuntimeError as exc:
            logger.warning("parse_holdings_upload: job %s parse failed: %s", job_id, exc)
            job.status = "failed"
            job.error = str(exc)
        except Exception as exc:
            logger.exception("parse_holdings_upload: job %s unexpected error", job_id)
            job.status = "failed"
            job.error = f"Parse error: {type(exc).__name__}: {exc}"
        finally:
            # Clear regardless of outcome — the job row shouldn't keep
            # holding plaintext holdings content once the parse attempt is
            # done with it (PR #82 review).
            job.raw_text = None
        session.commit()
        return {"job_id": job_id, "status": job.status}
    finally:
        session.close()


@task_revoked.connect(sender=parse_holdings_upload)  # type: ignore[untyped-decorator]
def _mark_revoked_job_failed(
    sender: Any = None,
    request: Any = None,
    terminated: bool | None = None,
    signum: int | None = None,
    expired: bool | None = None,
    **kwargs: Any,
) -> None:
    """Resolve the UploadJob row if parse_holdings_upload is ever explicitly
    revoked with terminate=True (e.g. `celery -A app.tasks control revoke
    <id> --terminate`, run by hand during ops debugging).

    NOT the path for an automatic hard time_limit kill (PR #88 review): on
    celery==5.6.3, task_revoked is only sent from Request.terminate(), which
    an explicit revoke(terminate=True) control command triggers —
    Request.on_timeout(soft=False) (the actual time_limit path) never calls
    it. _UploadJobRequest.on_timeout above is what resolves the job for
    issue #85's real SIGKILL scenario; this handler is a narrower, separate
    safety net for the explicit-revoke case, kept because it's correct and
    costs nothing, not because it covers the time_limit gap. Scoped to this
    task only (sender=parse_holdings_upload) so it never touches
    report/capture task revocations.

    terminated=False means the task was revoked before it ever started
    (e.g. removed from the queue) — nothing was written, no cleanup needed.
    """
    if not terminated or request is None:
        return
    job_id = request.args[0] if request.args else request.kwargs.get("job_id")
    if not job_id:
        logger.error("task_revoked handler: no job_id on revoked request %r", request)
        return
    _resolve_pending_job_as_failed(
        job_id=str(job_id),
        error=f"Worker was killed (signal {signum}) before it could record a result.",
        log_context="task_revoked handler",
    )


def _resolve_pending_job_as_failed(job_id: str, error: str, log_context: str) -> bool:
    """Mark one UploadJob row failed and clear raw_text, if it's still
    pending. Shared by _UploadJobRequest.on_timeout, the task_revoked
    handler, and the sweeper (issue #85) — all three are recovering a job
    the killed worker process could never resolve on its own. Returns True
    if a row was actually updated.

    Exceptions are caught, not propagated (PR #88 review): this runs from
    contexts that must not blow up on a transient DB error — Celery's own
    timeout-handling machinery (on_timeout), a signal receiver, and a loop
    over multiple stale rows in the sweeper where one bad row shouldn't
    abort the rest of that pass. A row this call fails to resolve just
    remains a candidate for the next mechanism to catch it (the sweeper
    re-scans every cycle) rather than crashing its caller.
    """
    from app.core.database import SessionLocal
    from app.models.upload_job import UploadJob

    try:
        session = SessionLocal()
    except Exception:
        logger.exception("%s: could not open a DB session for job %s", log_context, job_id)
        return False
    try:
        job = session.get(UploadJob, uuid.UUID(job_id))
        if job is None or job.status != "pending":
            return False
        job.status = "failed"
        job.error = error
        job.raw_text = None
        session.commit()
        logger.warning("%s: job %s marked failed (%s)", log_context, job_id, error)
        return True
    except Exception:
        logger.exception("%s: failed to resolve job %s", log_context, job_id)
        return False
    finally:
        session.close()


# Buffer over the hard time_limit (issue #85, widened per PR #88 review):
# the sweeper is a backstop for cases _UploadJobRequest.on_timeout itself
# misses (MainProcess restarted between the kill and the hook running, or
# the whole worker host going down) — not a race against the hard kill, and
# not just parse time. created_at is set at enqueue time, not when a worker
# actually picks the task up, and this app has no separate queue for
# holdings uploads — capture tasks (news/prices/fx/fund_navs/forward_events,
# all on the same default queue/worker pool) can legitimately delay a
# holdings job's start. 45s of slack on top of the SLA is generous headroom
# for that queue-wait, not just the 45s parse budget itself, so the sweeper
# doesn't false-fail a job that's still legitimately queued or running.
_SWEEP_STALE_AFTER_SECONDS = _SLA_SECONDS + 45


@celery_app.task(name="app.tasks.holdings_tasks.sweep_stale_upload_jobs")  # type: ignore[untyped-decorator]
def sweep_stale_upload_jobs() -> dict[str, int]:
    """Periodic backstop (issue #85): mark any UploadJob row stuck at
    status="pending" past _SWEEP_STALE_AFTER_SECONDS as failed and clear
    raw_text, in case _UploadJobRequest.on_timeout didn't run.
    """
    from app.core.database import SessionLocal
    from app.models.upload_job import UploadJob

    cutoff = datetime.now(UTC) - timedelta(seconds=_SWEEP_STALE_AFTER_SECONDS)
    session = SessionLocal()
    try:
        stale_ids = (
            session.query(UploadJob.id)
            .filter(UploadJob.status == "pending", UploadJob.created_at < cutoff)
            .all()
        )
    finally:
        session.close()

    swept = sum(
        _resolve_pending_job_as_failed(
            job_id=str(row.id),
            error=(
                "Parse did not complete within the expected time window "
                f"({_SWEEP_STALE_AFTER_SECONDS}s) and was never resolved by the worker."
            ),
            log_context="sweep_stale_upload_jobs",
        )
        for row in stale_ids
    )
    return {"swept": swept}


# Issue #264: retention policy for upload_jobs rows. A successful upload's
# preview JSONB is the only sensitive payload that outlives the parse
# (raw_text is cleared by the task itself), and it has no value once the
# user has confirmed the holdings — but rows were previously kept forever.
# 30 days is generous relative to that window (confirm happens minutes
# after upload).
_UPLOAD_JOB_RETENTION_DAYS = 30


def _cleanup_expired_upload_jobs(session: Session, cutoff: datetime) -> int:
    """Delete every terminal UploadJob row with created_at < cutoff.
    Commits and returns the delete count. Split out from the Celery task so
    it's testable against a real db_session without mocking SessionLocal
    (issue #264) — same shape as cache_tasks._cleanup_expired.

    Terminal states only: a stale-pending row is the sweeper's job
    (sweep_stale_upload_jobs resolves it within ~90s of the SLA), so this
    sweep must never race it."""
    from app.models.upload_job import UploadJob

    result = cast(
        CursorResult[Any],
        session.execute(
            delete(UploadJob).where(
                UploadJob.created_at < cutoff,
                UploadJob.status != "pending",
            )
        ),
    )
    session.commit()
    return result.rowcount or 0


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.holdings_tasks.cleanup_upload_jobs",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def cleanup_upload_jobs(self: Any) -> dict[str, int]:
    """Daily retention sweep (issue #264): delete UploadJob rows older than
    `_UPLOAD_JOB_RETENTION_DAYS`.

    Retry + ops-alert on exhaustion, matching cache_tasks's shared-intel
    sweep — a silent outage would be exactly the unbounded growth this
    task exists to prevent."""
    from app.core.database import SessionLocal

    cutoff = datetime.now(UTC) - timedelta(days=_UPLOAD_JOB_RETENTION_DAYS)
    session = SessionLocal()
    try:
        deleted = _cleanup_expired_upload_jobs(session, cutoff)
        logger.info(
            "cleanup_upload_jobs: deleted %d upload_jobs row(s) older than %s",
            deleted,
            cutoff,
        )
        return {"deleted": deleted}
    except Exception as exc:
        logger.exception("cleanup_upload_jobs: failed, scheduling retry")
        if self.request.retries >= self.max_retries:
            send_ops_alert(
                subject="[Portfonia] upload_jobs retention sweep FAILED — retries exhausted",
                body=(
                    f"cleanup_upload_jobs failed after {self.max_retries} retries.\n\n"
                    f"error: {type(exc).__name__}: {exc}\n\n"
                    f"Impact: upload_jobs rows (including holdings preview JSONB) older than "
                    f"{_UPLOAD_JOB_RETENTION_DAYS} days are not being cleaned up — the table "
                    f"keeps growing until this is fixed. Not a correctness issue, but check "
                    f"disk usage if this persists. Check worker.log for the full traceback."
                ),
            )
        raise self.retry(exc=exc) from exc
    finally:
        session.close()
