"""Celery task for async holdings-file parsing (issue #77)."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from celery.signals import task_revoked  # type: ignore[import-untyped]

from app.tasks import celery_app

logger = logging.getLogger(__name__)

# Product-level SLA (issue #85): a single upload interaction with no status
# update should not run past this many seconds before resolving to a
# terminal state. Real-world latency on STRUCTURED_LLM_MODEL is ~11-14s for
# a 30-row file (issue #84/#86), so 45s already has headroom over 2 back-to-
# back successful attempts — it's meant to bound a hang, not a normal run.
_SLA_SECONDS = 45


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.holdings_tasks.parse_holdings_upload",
    # soft_time_limit raises SoftTimeLimitExceeded inside the task (caught by
    # the broad except below, so the job still gets marked failed with a
    # specific error) — this is the preferred path, since it runs the task's
    # own cleanup. time_limit is Celery's unconditional SIGKILL if soft
    # didn't get a chance to run (e.g. the hang is in a C extension or the
    # process is otherwise wedged); that kill is an OS signal, invisible to
    # this task's own except/finally, so it's the task_revoked handler below
    # (issue #85) — not this task's code — that resolves the job in that
    # case. time_limit is pinned to the SLA; soft_time_limit leaves it a
    # 10s head start to self-report before the hard kill.
    soft_time_limit=_SLA_SECONDS - 10,
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
    """Resolve the UploadJob row when Celery's hard time_limit SIGKILLs the
    worker process running parse_holdings_upload (issue #85).

    A hard time_limit kill is an OS signal (SIGKILL) delivered to the
    ForkPoolWorker — it cannot be caught by that task's own except/finally,
    so nothing in parse_holdings_upload ever runs to write a terminal
    status. Celery's MainProcess survives the kill of its child worker and
    fires task_revoked here with terminated=True, signum=SIGKILL — this is
    the earliest point anything can still write to the job row. Scoped to
    this task only (sender=parse_holdings_upload) so it never touches
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
    pending. Shared by the task_revoked handler above and the sweeper below
    (issue #85) — both are recovering a job the killed worker process could
    never resolve on its own. Returns True if a row was actually updated.
    """
    from app.core.database import SessionLocal
    from app.models.upload_job import UploadJob

    session = SessionLocal()
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
    finally:
        session.close()


# Buffer over the hard time_limit (issue #85): the sweeper is a backstop for
# cases the task_revoked handler itself misses (MainProcess restarted
# between the kill and the signal firing, a missed signal, or the whole
# worker host going down) — not a race against the hard kill. By the time a
# row crosses this cutoff, the task_revoked handler has already had a full
# _SLA_SECONDS window to have resolved it first.
_SWEEP_STALE_AFTER_SECONDS = _SLA_SECONDS + 15


@celery_app.task(name="app.tasks.holdings_tasks.sweep_stale_upload_jobs")  # type: ignore[untyped-decorator]
def sweep_stale_upload_jobs() -> dict[str, int]:
    """Periodic backstop (issue #85): mark any UploadJob row stuck at
    status="pending" past _SWEEP_STALE_AFTER_SECONDS as failed and clear
    raw_text, in case the task_revoked handler didn't run.
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
