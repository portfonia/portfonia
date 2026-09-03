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
    asset_class: str | None
    sector: str | None
    market: str
    market_value: Decimal | None
    market_value_base: Decimal | None
    price_as_of: datetime | None
    pricing_mode: str
    capture_supported: bool
    broker: str | None
    account: str | None
    portfolio: str | None
    avg_cost: Decimal | None
    shares: Decimal | None
    notes: str | None
    cost_basis_base: Decimal | None
    unrealized_pnl_base: Decimal | None
    unrealized_pnl_pct: Decimal | None

    model_config = {"from_attributes": True}


class ConcentrationOut(BaseModel):
    top_holding_name: str | None
    top_holding_ratio: Decimal | None
    top_holding_asset_class: str | None
    top3_ratio: Decimal | None
    top_asset_class_name: str | None
    top_asset_class_ratio: Decimal | None
    single_holding_watch: bool
    single_holding_high: bool
    top3_watch: bool
    asset_class_watch: bool
    asset_class_high: bool

    model_config = {"from_attributes": True}


class PortfolioSummaryResponse(BaseModel):
    base_currency: str
    fx_date: date
    total_base: Decimal
    by_market: dict[str, Decimal]
    by_currency: dict[str, Decimal]
    by_asset_type: dict[str, Decimal]
    by_sector: dict[str, Decimal]
    by_asset_class: dict[str, Decimal]
    by_group: dict[str, Decimal]
    by_account: dict[str, Decimal]
    total_cost_basis_base: Decimal
    total_unrealized_pnl_base: Decimal
    total_unrealized_pnl_pct: Decimal | None
    price_as_of_date: date | None
    concentration: ConcentrationOut
    stale_tickers: list[str]
    holdings: list[HoldingValueOut]
