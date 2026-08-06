import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import get_current_user_id
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
_MIN_BARS_FOR_TECHNICAL = 50


def _tickers_with_sparse_history(session: Session) -> list[str]:
    """Return tickers that are auto-priced with a ticker but have < _MIN_BARS_FOR_TECHNICAL
    close bars in price_snapshots. These need an OHLCV backfill."""
    holdings = session.scalars(
        select(Holding).where(Holding.ticker.is_not(None), Holding.pricing_mode == "auto")
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
    sparse = [t for t in tickers if bar_counts.get(t, 0) < _MIN_BARS_FOR_TECHNICAL]
    if sparse:
        logger.info(
            "confirm_holdings: %d ticker(s) with < %d close bars — backfill enqueued: %s",
            len(sparse),
            _MIN_BARS_FOR_TECHNICAL,
            sparse,
        )
    return sparse


@router.post("/upload", response_model=UploadJobOut, status_code=status.HTTP_202_ACCEPTED)
async def upload_holdings(
    file: UploadFile,
    session: Session = Depends(get_session),
    user_id: UUID = Depends(get_current_user_id),
) -> UploadJob:
    """Kick off an async parse of the uploaded file (issue #77).

    Returns immediately with a pending job; the client polls
    GET /holdings/upload/{job_id} for the result. The previous synchronous
    version held one HTTP connection open for the full parse (2 pinned LLM
    attempts + 1 open-provider fallback — issue #78), observed taking ~5min
    in one case: fragile against any interruption on that connection in the
    meantime — the backend finished and returned 200, but the client never
    saw it because the connection had already dropped.
    """
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No filename provided."
        )
    content = await file.read()
    try:
        text = holding_parser._extract_text(content, file.filename)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    job = UploadJob(user_id=user_id, filename=file.filename, status="pending")
    session.add(job)
    session.commit()
    session.refresh(job)
    parse_holdings_upload.delay(str(job.id), text)
    return job


@router.get("/upload/{job_id}", response_model=UploadJobOut)
def get_upload_job(
    job_id: UUID,
    session: Session = Depends(get_session),
    user_id: UUID = Depends(get_current_user_id),
) -> UploadJob:
    job = session.get(UploadJob, job_id)
    if job is None or job.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload job not found.")
    return job


@router.post("/confirm", response_model=list[HoldingOut])
def confirm_holdings(
    rows: list[ParsedRow],
    session: Session = Depends(get_session),
    user_id: UUID = Depends(get_current_user_id),
) -> list[Holding]:
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
    # If any ticker has fewer than 50 close bars, it was recently added and
    # needs a historical backfill so §4.4 technical position can populate.
    tickers_needing_backfill = _tickers_with_sparse_history(session)
    if tickers_needing_backfill:
        from app.tasks.capture_tasks import backfill_ohlcv_task

        backfill_ohlcv_task.delay()
    for h in holdings:
        session.refresh(h)
    return holdings


@router.get("", response_model=list[HoldingOut])
def list_holdings(
    session: Session = Depends(get_session),
    user_id: UUID = Depends(get_current_user_id),
) -> list[Holding]:
    rows = session.scalars(
        select(Holding)
        .where(Holding.user_id == user_id)
        .order_by(Holding.asset_type.nulls_last(), Holding.name)
    ).all()
    return list(rows)


@router.get("/export")
def export_holdings(
    session: Session = Depends(get_session),
    user_id: UUID = Depends(get_current_user_id),
) -> Response:
    rows = session.scalars(
        select(Holding)
        .where(Holding.user_id == user_id)
        .order_by(Holding.asset_type.nulls_last(), Holding.name)
    ).all()
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
