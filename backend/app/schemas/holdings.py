from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class IssueRow(BaseModel):
    raw: str
    reason: str


class ParsedRow(BaseModel):
    name: str
    ticker: str | None = None
    fund_code: str | None = None
    currency: str
    shares: float | None = None
    avg_cost: float | None = None
    current_value: float | None = None
    pricing_mode: Literal["auto", "manual"]
    asset_type: Literal["stock", "etf", "fund", "cash", "wmf", "other"] | None = None
    # Economic classification set by _postprocess (ticker lookup + asset_type fallback).
    # Not emitted by the LLM; always populated before confirm.
    asset_class: str = "STOCK"
    # User-declared market bucket; normalized in _postprocess. None = let the
    # calculator derive it from the ticker.
    market: Literal["US", "HK", "A-Share", "Other"] | None = None
    broker: str | None = None
    account: str | None = None
    portfolio: str | None = None
    notes: str | None = None
    issues: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class CurrencySubtotal(BaseModel):
    """Cost-basis subtotal for one currency within a broker group.

    Cost basis = sum of shares*avg_cost (or current_value where shares/avg_cost
    are absent). Pre-capture file-content figure for cross-checking, not a
    market valuation.
    """

    currency: str
    cost_basis: float
    holding_count: int


class BrokerGroup(BaseModel):
    """Per-broker (Custodian) parse summary for upload cross-checking.

    Groups mirror §1's broker grouping: first-seen/upload order, broker-less
    rows under "Other". Subtotals are split by currency so mixed-currency
    institutions never sum incomparable figures.
    """

    broker: str
    holding_count: int
    subtotals: list[CurrencySubtotal]


class UploadPreview(BaseModel):
    valid_rows: list[ParsedRow]
    issue_rows: list[IssueRow]
    broker_groups: list[BrokerGroup] = Field(default_factory=list)


class UploadJobOut(BaseModel):
    """Poll target for an async holdings-file parse (issue #77).

    `preview` is populated only once `status="success"`; `error` only once
    `status="failed"`. `status="pending"` carries neither.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: Literal["pending", "success", "failed"]
    preview: UploadPreview | None = None
    error: str | None = None


class HoldingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    ticker: str | None
    fund_code: str | None
    currency: str
    shares: Decimal | None
    avg_cost: Decimal | None
    current_value: Decimal | None
    pricing_mode: str
    asset_type: str | None
    asset_class: str
    market: str | None
    broker: str | None
    account: str | None
    portfolio: str | None
    notes: str | None
    last_manual_update: datetime | None
    created_at: datetime
    updated_at: datetime
