from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.schemas.portfolio import HoldingValueOut, PortfolioSummaryResponse
from app.services.portfolio_calculator import compute_portfolio

router = APIRouter()

BaseCurrency = Literal["USD", "HKD", "CNY"]


@router.get("/summary", response_model=PortfolioSummaryResponse)
def get_portfolio_summary(
    base_currency: Annotated[BaseCurrency, Query()] = "USD",
    session: Session = Depends(get_session),
) -> PortfolioSummaryResponse:
    snap = compute_portfolio(session, base_currency=base_currency)

    holdings_out = [
        HoldingValueOut(
            holding_id=hv.holding_id,
            name=hv.name,
            ticker=hv.ticker,
            fund_code=hv.fund_code,
            currency=hv.currency,
            asset_type=hv.asset_type,
            market=hv.market,
            market_value=hv.market_value,
            market_value_base=hv.market_value_base,
            price_as_of=hv.price_as_of,
        )
        for hv in snap.holdings
    ]

    return PortfolioSummaryResponse(
        base_currency=snap.base_currency,
        fx_date=snap.fx_date,
        total_base=snap.total_base,
        by_market=snap.by_market,
        by_currency=snap.by_currency,
        by_asset_type=snap.by_asset_type,
        stale_tickers=snap.stale_tickers,
        holdings=holdings_out,
    )
