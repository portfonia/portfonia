from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel


class HoldingValueOut(BaseModel):
    holding_id: uuid.UUID
    name: str
    ticker: str | None
    fund_code: str | None
    currency: str
    asset_type: str | None
    market: str
    market_value: Decimal
    market_value_base: Decimal
    price_as_of: datetime | None

    model_config = {"from_attributes": True}


class PortfolioSummaryResponse(BaseModel):
    base_currency: str
    fx_date: date
    total_base: Decimal
    by_market: dict[str, Decimal]
    by_currency: dict[str, Decimal]
    by_asset_type: dict[str, Decimal]
    stale_tickers: list[str]
    holdings: list[HoldingValueOut]
