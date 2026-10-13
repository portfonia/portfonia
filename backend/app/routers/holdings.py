import logging
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import Principal, current_principal
from app.models.holding import Holding
from app.models.upload_job import UploadJob
from app.models.user import User
from app.schemas.holdings import (
    HoldingOut,
    HoldingPatch,
    ParsedRow,
    ReorderIn,
    UploadJobOut,
)
from app.services import holding_parser, holdings_write
from app.services.holding_ordering import sorted_holdings as _sorted_holdings
from app.services.holdings_export import holdings_export_filename, render_export, render_template
from app.tasks.holdings_tasks import parse_holdings_upload

logger = logging.getLogger(__name__)

router = APIRouter()


# A holdings file is a few dozen to a few thousand rows of text/spreadsheet
# data — this is generous headroom, not a realistic size, meant only to stop
# an oversized upload from being read fully into memory and handed to
# extract/enqueue (PR #82 review). Enforced two ways (PR #82 second review):
# a Content-Length fast-path rejects an obviously oversized request before
# reading any body, and a chunked read aborts once the running total crosses
# the limit rather than materializing the full body first — the earlier
# `content = await file.read()` version read the whole thing into memory
# before ever checking its size.
_MAX_UPLOAD_BYTES = 5 * 1024 * 1024
_UPLOAD_READ_CHUNK_BYTES = 64 * 1024

# _MAX_UPLOAD_BYTES bounds the raw file only. A high text-to-byte-ratio file
# (e.g. an .xlsx/.xls that unpacks into a much larger CSV) can still extract
# to far more text than any real holdings file — same "a few dozen to a few
# thousand rows" headroom logic as _MAX_UPLOAD_BYTES above, applied to the
# extracted text that actually reaches the LLM (issue #54).
_MAX_TEXT_BYTES = 100 * 1024


def _report_locale(session: Session, user_id: UUID) -> str:
    user = session.get(User, user_id)
    if user is None or user.locale not in ("en", "zh", "zh-Hant"):
        return "en"
    return user.locale


@router.post("/upload", response_model=UploadJobOut, status_code=status.HTTP_202_ACCEPTED)
async def upload_holdings(
    request: Request,
    file: UploadFile,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> UploadJob:
    """Kick off an async parse of the uploaded file (issue #77).

    Returns immediately with a pending job; the client polls
    GET /holdings/upload/{job_id} for the result. The previous synchronous
    version held one HTTP connection open for the full parse (holding_parser
    .parse()'s internal retry loop — issue #78), observed taking ~5min in
    one case: fragile against any interruption on that connection in the
    meantime — the backend finished and returned 200, but the client never
    saw it because the connection had already dropped.
    """
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No filename provided."
        )
    # Fast path: reject an obviously oversized request before reading any
    # body. This reflects the whole multipart body (boundary + other fields
    # too), not just this file, so it's a coarse pre-check — the chunked
    # read below is the real bound.
    content_length = request.headers.get("content-length")
    if (
        content_length is not None
        and content_length.isdigit()
        and int(content_length) > _MAX_UPLOAD_BYTES
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"File too large ({content_length} bytes) — max {_MAX_UPLOAD_BYTES} bytes.",
        )
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(_UPLOAD_READ_CHUNK_BYTES):
        total += len(chunk)
        if total > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"File too large — max {_MAX_UPLOAD_BYTES} bytes.",
            )
        chunks.append(chunk)
    content = b"".join(chunks)
    try:
        text = holding_parser._extract_text(content, file.filename)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    if len(text.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Extracted text too large — max {_MAX_TEXT_BYTES} bytes.",
        )

    # raw_text is cleared by the Celery task once the parse attempt finishes
    # (success or failure) — the task takes job_id only, never the text
    # itself, so a holdings file's content never becomes a Celery/Redis
    # broker message argument (PR #82 review).
    job = UploadJob(
        user_id=principal.user_id, filename=file.filename, status="pending", raw_text=text
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    try:
        parse_holdings_upload.delay(str(job.id))
    except Exception as exc:
        # Enqueue failed after the row was already committed — without this,
        # the job would sit at status="pending" forever with no task ever
        # attached to pick it up (PR #82 review).
        logger.exception("upload_holdings: failed to enqueue job %s", job.id)
        job.status = "failed"
        job.error = f"Failed to queue parse job: {type(exc).__name__}: {exc}"
        job.raw_text = None
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not queue the upload for processing. Please try again.",
        ) from exc
    return job


@router.get("/upload/{job_id}", response_model=UploadJobOut)
def get_upload_job(
    job_id: UUID,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> UploadJob:
    job = session.get(UploadJob, job_id)
    if job is None or job.user_id != principal.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload job not found.")
    return job


@router.post("/confirm", response_model=list[HoldingOut])
def confirm_holdings(
    rows: list[ParsedRow],
    mode: Literal["append", "replace"] = Query("append"),
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> list[Holding]:
    """Persist parsed rows. Append is the default.

    File confirm strips watch_tier inside the shared writer. Single-row
    create still accepts a client watch_tier.
    """
    return holdings_write.confirm_holdings(session, principal.user_id, rows, mode)


@router.get("", response_model=list[HoldingOut])
def list_holdings(
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> list[Holding]:
    rows = _sorted_holdings(
        session.scalars(select(Holding).where(Holding.user_id == principal.user_id)).all()
    )
    return list(rows)


@router.post("", response_model=HoldingOut, status_code=status.HTTP_201_CREATED)
def create_holding(
    row: ParsedRow,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> Holding:
    """Create one holding from a structured body. Does not call the LLM."""
    return holdings_write.create_holding(session, principal.user_id, row)


@router.patch("/reorder", response_model=list[HoldingOut])
def reorder_holdings(
    body: ReorderIn,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> list[Holding]:
    return holdings_write.reorder_holdings(session, principal.user_id, body.ids)


@router.get("/export")
def export_holdings(
    locale: str | None = Query(None),
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> Response:
    """`locale`, when given, is the frontend's current UI locale (issue #319
    item 9) and takes precedence over the report-language fallback below.
    Any locale `render_rules` does not recognize falls back to English
    there — this endpoint does not itself validate the value.
    """
    rows = _sorted_holdings(
        session.scalars(select(Holding).where(Holding.user_id == principal.user_id)).all()
    )
    effective_locale = locale if locale is not None else _report_locale(session, principal.user_id)
    md = render_export(list(rows), effective_locale)
    filename = holdings_export_filename()
    return Response(
        content=md,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/template")
def holdings_template(
    locale: str | None = Query(None),
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> Response:
    """Same `locale` precedence as GET /holdings/export above."""
    effective_locale = locale if locale is not None else _report_locale(session, principal.user_id)
    md = render_template(effective_locale)
    return Response(
        content=md,
        media_type="text/markdown",
        headers={"Content-Disposition": "attachment; filename=holdings-template.md"},
    )


@router.patch("/{holding_id}", response_model=HoldingOut)
def update_holding(
    holding_id: UUID,
    patch: HoldingPatch,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> Holding:
    return holdings_write.update_holding(session, principal.user_id, holding_id, patch)


@router.delete("/{holding_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_holding(
    holding_id: UUID,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> Response:
    holdings_write.delete_holding(session, principal.user_id, holding_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
