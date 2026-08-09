from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ISO 4217 codes this app is willing to accept from the holdings parser.
# Not exhaustive ISO 4217 — scoped to currencies plausible for a retail
# investor's holdings (the three markets this product supports natively —
# US/HK/A-Share, per holding_parser.py's currency-inference rules — plus
# the other majors an international brokerage account or cash holding might
# carry). Single source of truth: also imported by the domain-constraint
# migration (issue #25) so the DB CHECK and this validator can't drift
# apart. Adding a currency is a code change (mirrors VALID_ASSET_CLASSES in
# app/services/asset_class_config.py), not a config edit — a migration is
# needed to widen the DB-side CHECK too.
VALID_CURRENCIES: frozenset[str] = frozenset(
    {
        "USD",
        "CNY",
        "HKD",
        "GBP",
        "EUR",
        "JPY",
        "SGD",
        "AUD",
        "CAD",
        "CHF",
        "KRW",
        "TWD",
        "MOP",
        "NZD",
    }
)


class IssueRow(BaseModel):
    raw: str
    reason: str


class ParsedRow(BaseModel):
    name: str
    ticker: str | None = None
    fund_code: str | None = None
    currency: str
    # Boundary validation (issue #25): this is the only user-writable entry
    # point for these fields — POST /holdings/confirm takes list[ParsedRow]
    # directly. A DB-level CHECK on shares/avg_cost/current_value is no
    # longer possible after encryption at rest (issue #31/PR #111 made these
    # columns Fernet ciphertext, not plaintext numeric) — see issue #113.
    # market_price is deliberately excluded: it's never user input, only
    # written by trusted fetcher code (yfinance/NAV).
    shares: float | None = Field(default=None, ge=0)
    avg_cost: float | None = Field(default=None, ge=0)
    current_value: float | None = Field(default=None, ge=0)
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

    @field_validator("currency")
    @classmethod
    def _currency_must_be_known(cls, v: str) -> str:
        if v not in VALID_CURRENCIES:
            raise ValueError(f"unrecognized currency {v!r} — not in VALID_CURRENCIES")
        return v


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
