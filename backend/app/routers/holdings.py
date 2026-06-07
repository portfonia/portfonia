from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import get_current_user_id
from app.models.holding import Holding
from app.schemas.holdings import HoldingOut, ParsedRow, UploadPreview
from app.services import holding_parser

router = APIRouter()

_ASSET_TYPE_ORDER = {"stock": 0, "etf": 1, "fund": 2, "wmf": 3, "cash": 4, "other": 5}


@router.post("/upload", response_model=UploadPreview)
async def upload_holdings(
    file: UploadFile,
    _user_id: UUID = Depends(get_current_user_id),
) -> UploadPreview:
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
    try:
        return holding_parser.parse(text)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    except Exception as exc:
        import logging

        logging.getLogger(__name__).exception("Unexpected parse error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Parse error: {type(exc).__name__}: {exc}",
        ) from exc


@router.post("/confirm", response_model=list[HoldingOut])
def confirm_holdings(
    rows: list[ParsedRow],
    session: Session = Depends(get_session),
    user_id: UUID = Depends(get_current_user_id),
) -> list[Holding]:
    session.execute(delete(Holding).where(Holding.user_id == user_id))
    holdings: list[Holding] = []
    now = datetime.now(tz=UTC)
    for row in rows:
        data = row.model_dump(exclude={"issues", "confidence"})
        # Coerce float fields to Decimal for the ORM
        for field in ("shares", "avg_cost", "current_value"):
            if data[field] is not None:
                data[field] = Decimal(str(data[field]))
        if data["pricing_mode"] == "manual":
            data["last_manual_update"] = now
        holdings.append(Holding(user_id=user_id, **data))
    session.add_all(holdings)
    session.commit()
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
