"""Celery task for async holdings-file parsing (issue #77)."""

from __future__ import annotations

import logging
import uuid

from app.tasks import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.holdings_tasks.parse_holdings_upload",
)
def parse_holdings_upload(job_id: str, text: str) -> dict[str, str]:
    """Parse an uploaded holdings file in the background and write the result
    onto the UploadJob row the client is polling.

    No Celery-level retry: holding_parser.parse() already retries internally
    (2 pinned attempts + 1 open-provider fallback — issue #78). Stacking a
    Celery retry on top would only add unbounded extra latency to an
    interactive, user-facing action; if all 3 internal attempts fail, this
    records the failure and the user can just re-upload.
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
        try:
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
        session.commit()
        return {"job_id": job_id, "status": job.status}
    finally:
        session.close()
