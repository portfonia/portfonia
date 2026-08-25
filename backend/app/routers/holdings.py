import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import Principal, current_principal
from app.models.holding import Holding
from app.models.price_snapshot import PriceSnapshot
from app.models.upload_job import UploadJob
from app.schemas.holdings import HoldingOut, ParsedRow, UploadJobOut
from app.services import holding_parser
from app.services.price_fetcher import backfill_sectors
from app.tasks.holdings_tasks import parse_holdings_upload

logger = logging.getLogger(__name__)

router = APIRouter()

_ASSET_TYPE_ORDER = {"stock": 0, "etf": 1, "fund": 2, "wmf": 3, "cash": 4, "other": 5}


def _sorted_holdings(rows: Sequence[Holding]) -> list[Holding]:
    """Order by asset_type (NULLs last) then name (issue #31).

    ``name`` is encrypted (ciphertext at the SQL level), so ``ORDER BY`` at
    the database can no longer sort by its real value — this used to be
    ``.order_by(Holding.asset_type.nulls_last(), Holding.name)`` in the
    caller's query. ``asset_type`` itself stays plaintext (a classification
    column, not encrypted by design) but is included here for stable
    ordering, matching the old query. TypeDecorator decryption happens
    transparently on ORM attribute access, so sorting the already-fetched
    Python objects by ``h.name`` sees plaintext.
    """
    return sorted(rows, key=lambda h: (h.asset_type is None, h.asset_type or "", h.name))


_MIN_BARS_FOR_TECHNICAL = 50
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


def _tickers_with_sparse_history(session: Session, user_id: UUID) -> list[str]:
    """Return this user's auto-priced tickers with < _MIN_BARS_FOR_TECHNICAL close bars.

    price_snapshots is a global store (no user_id); the *fetch* still writes
    shared rows. The *trigger* is this confirm's book — another user's sparse
    name must not fire a 420-day job here (issue #194).
    """
    holdings = session.scalars(
        select(Holding).where(
            Holding.user_id == user_id,
            Holding.ticker.is_not(None),
            Holding.pricing_mode == "auto",
        )
    ).all()
    tickers = {h.ticker for h in holdings if h.ticker}
    if not tickers:
        return []
    bar_counts: dict[str, int] = {
        row[0]: row[1]
        for row in session.execute(
            select(PriceSnapshot.ticker, func.count().label("bars"))
            .where(
                PriceSnapshot.ticker.in_(tickers),
                PriceSnapshot.session_node == "close",
                PriceSnapshot.close.is_not(None),
            )
            .group_by(PriceSnapshot.ticker)
        ).all()
    }
    sparse = sorted(t for t in tickers if bar_counts.get(t, 0) < _MIN_BARS_FOR_TECHNICAL)
    if sparse:
        # Concept §8.8: application logs record user_id, never holdings
        # content — a ticker list is holdings-derived (issue #129 §2.2 D).
        logger.info(
            "confirm_holdings: user_id=%s triggered backfill for %d ticker(s) with < %d close bars",
            user_id,
            len(sparse),
            _MIN_BARS_FOR_TECHNICAL,
        )
    return sparse


def _fund_codes_without_close(session: Session, user_id: UUID) -> list[str]:
    """Return this user's auto-priced fund_codes with no captured close.

    price_snapshots is global; if any user (or a prior scheduled run) already
    wrote a close under this fund_code, this confirm reuses it. §4.4 technical
    position skips funds (no ticker), so one close is enough for valuation —
    unlike tickers, which backfill when they have < 50 bars (issue #196).
    """
    holdings = session.scalars(
        select(Holding).where(
            Holding.user_id == user_id,
            Holding.fund_code.is_not(None),
            Holding.pricing_mode == "auto",
        )
    ).all()
    codes = {h.fund_code for h in holdings if h.fund_code}
    if not codes:
        return []
    cached = set(
        session.scalars(
            select(PriceSnapshot.ticker)
            .where(
                PriceSnapshot.ticker.in_(codes),
                PriceSnapshot.session_node == "close",
                PriceSnapshot.close.is_not(None),
            )
            .distinct()
        ).all()
    )
    missing = sorted(c for c in codes if c not in cached)
    if missing:
        # Concept §8.8: count + user_id, never the fund_code list.
        logger.info(
            "confirm_holdings: user_id=%s triggered fund NAV capture for %d fund(s) with no close snapshot",
            user_id,
            len(missing),
        )
    return missing


def _enqueue_confirm_capture(task: Any, args: list[str], label: str, user_id: UUID) -> None:
    """Fire-and-forget after holdings are already committed.

    A broker blip must not turn a successful confirm into a 500 — the write
    is done; the daily capture beat covers a missed enqueue. Unlike
    upload_holdings (PR #82), there is no pending job row that would be
    stuck without the task.
    """
    try:
        task.delay(args)
    except Exception:
        logger.exception(
            "confirm_holdings: user_id=%s failed to enqueue %s for %d identifier(s)",
            user_id,
            label,
            len(args),
        )


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
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> list[Holding]:
    user_id = principal.user_id
    session.execute(delete(Holding).where(Holding.user_id == user_id))
    holdings: list[Holding] = []
    now = datetime.now(tz=UTC)
    for idx, row in enumerate(rows):
        data = row.model_dump(exclude={"issues", "confidence"})
        # Coerce float fields to Decimal for the ORM
        for field in ("shares", "avg_cost", "current_value"):
            if data[field] is not None:
                data[field] = Decimal(str(data[field]))
        if data["pricing_mode"] == "manual":
            data["last_manual_update"] = now
        # Preserve upload order so reports can mirror the user's file layout.
        data["position"] = idx
        holdings.append(Holding(user_id=user_id, **data))
    session.add_all(holdings)
    session.commit()
    # Populate sector from yfinance for all auto-mode ticker holdings.
    backfill_sectors(session)
    # If this user's auto tickers have fewer than 50 close bars, they were
    # recently added and need a historical backfill so §4.4 can populate.
    tickers_needing_backfill = _tickers_with_sparse_history(session, user_id)
    if tickers_needing_backfill:
        from app.tasks.capture_tasks import backfill_ohlcv_task

        _enqueue_confirm_capture(
            backfill_ohlcv_task, tickers_needing_backfill, "ohlcv backfill", user_id
        )
    # Fund_code holdings are not on the OHLCV path. Without this dispatch
    # they wait until the next capture-fund-navs-daily beat (up to ~24h,
    # longer over a weekend) and the first report drops them as missing
    # price data (issue #196). Fire-and-forget — confirm must stay fast
    # (issue #193).
    fund_codes_needing_nav = _fund_codes_without_close(session, user_id)
    if fund_codes_needing_nav:
        from app.tasks.capture_tasks import backfill_fund_navs_task

        _enqueue_confirm_capture(
            backfill_fund_navs_task, fund_codes_needing_nav, "fund NAV capture", user_id
        )
    for h in holdings:
        session.refresh(h)
    return holdings


@router.get("", response_model=list[HoldingOut])
def list_holdings(
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> list[Holding]:
    rows = _sorted_holdings(
        session.scalars(select(Holding).where(Holding.user_id == principal.user_id)).all()
    )
    return list(rows)


@router.get("/export")
def export_holdings(
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> Response:
    rows = _sorted_holdings(
        session.scalars(select(Holding).where(Holding.user_id == principal.user_id)).all()
    )
    md = _render_markdown(list(rows))
    return Response(
        content=md,
        media_type="text/markdown",
        headers={"Content-Disposition": "attachment; filename=holdings.md"},
    )


def _render_markdown(holdings: list[Holding]) -> str:
    lines: list[str] = [
        "# Holdings",
        "",
        "| Name | Ticker | Fund Code | Currency | Shares | Avg Cost | Current Value | Pricing Mode | Asset Type | Broker | Account | Portfolio | Notes |",
        "|------|--------|-----------|----------|--------|----------|---------------|--------------|------------|--------|---------|-----------|-------|",
    ]
    for h in holdings:

        def _cell(v: object) -> str:
            if v is None:
                return ""
            # Escape pipes and flatten newlines so free-text fields (name, notes)
            # cannot break or inject into the Markdown table structure.
            return (
                str(v)
                .replace("\\", "\\\\")
                .replace("|", "\\|")
                .replace("\n", " ")
                .replace("\r", " ")
            )

        lines.append(
            f"| {_cell(h.name)} | {_cell(h.ticker)} | {_cell(h.fund_code)} "
            f"| {_cell(h.currency)} | {_cell(h.shares)} | {_cell(h.avg_cost)} "
            f"| {_cell(h.current_value)} | {_cell(h.pricing_mode)} "
            f"| {_cell(h.asset_type)} | {_cell(h.broker)} | {_cell(h.account)} "
            f"| {_cell(h.portfolio)} | {_cell(h.notes)} |"
        )
    return "\n".join(lines) + "\n"
