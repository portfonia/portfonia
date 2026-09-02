from datetime import datetime
from decimal import Decimal
from typing import Literal, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.asset_class_config import VALID_ASSET_CLASSES

# ISO 4217 codes this app is willing to accept from the holdings parser.
# Not exhaustive ISO 4217 — scoped to currencies plausible for a retail
# investor's holdings (the three markets this product supports natively —
# US/HK/A-Share, per holding_parser.py's currency-inference rules — plus
# the other majors an international brokerage account or cash holding might
# carry). CNH (offshore yuan) is distinct from CNY and already a first-class
# currency elsewhere in the app (portfolio_calculator.py's
# _CURRENCY_TO_FX_PAIR, fx_fetcher.py's USDCNH pair) — omitting it here was
# a real gap caught in PR #114 review (blacktomb42). Single source of
# truth: also imported by the domain-constraint migration (issue #25) so
# the DB CHECK and this validator can't drift apart. Adding a currency is a
# code change (mirrors VALID_ASSET_CLASSES in
# app/services/asset_class_config.py), not a config edit — a migration is
# needed to widen the DB-side CHECK too.
VALID_CURRENCIES: frozenset[str] = frozenset(
    {
        "USD",
        "CNY",
        "CNH",
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

# Canonical closed sets for pricing_mode/asset_type, derived from the
# Literal types below (get_args) rather than duplicated as separate
# constants — the Literal stays the one place these values are written.
# Imported by app/models/holding.py and the domain-constraint migration
# (issue #25) so all three layers can't drift apart (PR #114 review nit:
# these two used to be hand-copied in three places while currency/
# asset_class already had a single source of truth).
PricingMode = Literal["auto", "manual"]
AssetTypeValue = Literal["stock", "etf", "fund", "cash", "wmf", "other"]
VALID_PRICING_MODES: tuple[str, ...] = get_args(PricingMode)
VALID_ASSET_TYPES: tuple[str, ...] = get_args(AssetTypeValue)


class IssueRow(BaseModel):
    raw: str
    reason: str


# Deterministic postprocess codes only (issue #92 / PR #310). Model-supplied
# free-text notes are dropped — they are English and would leak through
# parser_note {message} for zh-locale users. Keep in sync with
# frontend/src/app/holdings/_components/preview.tsx ISSUE_NOTE_CODES.
KNOWN_ISSUE_CODES: frozenset[str] = frozenset(
    {
        "unrecognized_asset_type",
        "currency_normalized",
        "ticker_normalized_hk",
        "currency_corrected",
        "unrecognized_currency",
        "dropped_spurious_id",
        "cash_amount_moved",
        "cleared_residual_shares",
        "ticker_no_suffix",
    }
)


class IssueNote(BaseModel):
    """Structured parse note. Preview JSON only — not persisted on confirm."""

    code: str
    params: dict[str, str] = Field(default_factory=dict)
    severity: Literal["info", "warning"] = "info"


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
    pricing_mode: PricingMode
    asset_type: AssetTypeValue | None = None
    # Economic classification set by _postprocess (ticker lookup + asset_type
    # fallback) — not emitted by the LLM, always populated before confirm.
    # Still validated here (not just DB-CHECKed): POST /holdings/confirm
    # takes list[ParsedRow] straight from the client, so a caller bypassing
    # the normal upload flow could otherwise hit a raw IntegrityError/500
    # instead of a clean 422 (PR #114 review finding — same boundary
    # argument as the currency validator above).
    asset_class: str = "STOCK"
    # User-declared market bucket; normalized in _postprocess. None = let the
    # calculator derive it from the ticker. Closed set is VALID_HOLDING_MARKETS
    # (issue #311) — Other is a legitimate value, not a rejection flag.
    market: Literal["US", "HK", "A-Share", "UK", "Europe", "Japan", "Korea", "Other"] | None = None
    # Explicit not-processed marker. False when ticker/market does not resolve
    # into a scheduled capture bucket. Distinct from market == "Other".
    capture_supported: bool = True
    broker: str | None = None
    account: str | None = None
    portfolio: str | None = None
    notes: str | None = None
    issues: list[IssueNote] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("issues", mode="before")
    @classmethod
    def _drop_unknown_and_legacy_issue_strings(cls, v: object) -> object:
        # UploadJob.preview JSONB may still carry free-text notes or unknown
        # LLM codes from before PR #310. Drop them rather than wrapping as
        # parser_note {message} (zh users would still see English).
        if not v:
            return []
        if not isinstance(v, list):
            return v
        coerced: list[object] = []
        for item in v:
            if isinstance(item, str):
                continue
            if isinstance(item, dict) and item.get("code") in KNOWN_ISSUE_CODES:
                coerced.append(item)
        return coerced

    @field_validator("currency")
    @classmethod
    def _currency_must_be_known(cls, v: str) -> str:
        if v not in VALID_CURRENCIES:
            raise ValueError(f"unrecognized currency {v!r} — not in VALID_CURRENCIES")
        return v

    @field_validator("asset_class")
    @classmethod
    def _asset_class_must_be_known(cls, v: str) -> str:
        if v not in VALID_ASSET_CLASSES:
            raise ValueError(f"unrecognized asset_class {v!r} — not in VALID_ASSET_CLASSES")
        return v

    @model_validator(mode="after")
    def _cash_wmf_boundary(self) -> "ParsedRow":
        # Boundary guard (issue #120, tightened in PR #121 round 2 review):
        # cash/wmf products carry no real instrument identifier and have no
        # fetchable price. holding_parser._postprocess coerces all three of
        # these away for the normal upload path, but POST /holdings/confirm
        # takes list[ParsedRow] straight from the client — a caller
        # bypassing _postprocess must get a clean 422 rather than a row
        # that silently misses current_value and vanishes from every report
        # (compute_portfolio()'s manual-pricing branch only ever reads
        # current_value, and its auto branch has no ticker/fund_code to
        # fetch a price for). The original version of this guard only
        # checked ticker/fund_code, which left both of these open.
        if self.asset_type in ("cash", "wmf"):
            if self.ticker or self.fund_code:
                raise ValueError(
                    f"cash/wmf row {self.name!r} has a ticker/fund_code "
                    f"({self.ticker or self.fund_code!r}) — cash/wmf products carry no "
                    "real instrument identifier; the amount belongs in current_value"
                )
            if self.pricing_mode != "manual":
                raise ValueError(
                    f"cash/wmf row {self.name!r} has pricing_mode={self.pricing_mode!r} "
                    "— cash/wmf has no fetchable price and must be priced manually"
                )
            if self.current_value is None:
                raise ValueError(
                    f"cash/wmf row {self.name!r} is missing current_value — cash/wmf "
                    "rows are valued from current_value only, never shares"
                )
        return self


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
    # Non-blocking heads-up (issue #311): count of rows that will be stored
    # but never auto-priced. Not a parse error — rows stay in valid_rows.
    unsupported_capture_count: int = 0


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
    capture_supported: bool = True
    broker: str | None
    account: str | None
    portfolio: str | None
    notes: str | None
    last_manual_update: datetime | None
    created_at: datetime
    updated_at: datetime
    position: int | None = None


class HoldingPatch(BaseModel):
    """Partial update for PATCH /holdings/{id}. Unset fields are left unchanged."""

    name: str | None = None
    ticker: str | None = None
    fund_code: str | None = None
    currency: str | None = None
    shares: float | None = Field(default=None, ge=0)
    avg_cost: float | None = Field(default=None, ge=0)
    current_value: float | None = Field(default=None, ge=0)
    pricing_mode: PricingMode | None = None
    asset_type: AssetTypeValue | None = None
    market: Literal["US", "HK", "A-Share", "UK", "Europe", "Japan", "Korea", "Other"] | None = None
    broker: str | None = None
    account: str | None = None
    portfolio: str | None = None
    notes: str | None = None

    @field_validator("currency")
    @classmethod
    def _currency_must_be_known(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_CURRENCIES:
            raise ValueError(f"unrecognized currency {v!r} — not in VALID_CURRENCIES")
        return v


class ReorderIn(BaseModel):
    ids: list[UUID]
