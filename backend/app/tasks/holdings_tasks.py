"""Celery task for async holdings-file parsing (issue #77)."""

from __future__ import annotations

import logging
import uuid

from app.tasks import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.holdings_tasks.parse_holdings_upload",
    # Hard ceiling above the ~60s worst case (3 attempts x 20s client timeout
    # — issue #77/holding_parser.py). soft_time_limit raises
    # SoftTimeLimitExceeded inside the task (caught by the broad except
    # below, so the job still gets marked failed); time_limit is Celery's
    # unconditional kill if soft doesn't get a chance to run. Without this, a
    # hang outside the client-timeout path (a hung worker, a future code
    # change) could hold a prefork slot indefinitely (PR #82 review).
    soft_time_limit=75,
    time_limit=90,
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
    (2 pinned attempts + 1 open-provider fallback — issue #78). Stacking a
    Celery retry on top would only add unbounded extra latency to an
    interactive, user-facing action; if all 3 internal attempts fail, this
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
