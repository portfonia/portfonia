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
    broker: str | None = None
    account: str | None = None
    portfolio: str | None = None
    notes: str | None = None
    issues: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class UploadPreview(BaseModel):
    valid_rows: list[ParsedRow]
    issue_rows: list[IssueRow]


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
    broker: str | None
    account: str | None
    portfolio: str | None
    notes: str | None
    last_manual_update: datetime | None
    created_at: datetime
    updated_at: datetime
