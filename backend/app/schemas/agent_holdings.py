"""Strict Agent transport schemas; domain validation is shared by writes and previews."""

from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_serializer, field_validator

from app.schemas.holdings import AssetTypeValue, PricingMode, WatchTier

Version = Annotated[StrictStr, Field(pattern=r"^(0|[1-9][0-9]*)$")]
Market = Literal["US", "HK", "A-Share", "UK", "Europe", "Japan", "Korea", "Other"]


def fixed(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentHoldingInput(StrictModel):
    name: StrictStr
    currency: StrictStr
    pricing_mode: PricingMode
    ticker: StrictStr | None = None
    fund_code: StrictStr | None = None
    shares: Decimal | None = None
    avg_cost: Decimal | None = None
    current_value: Decimal | None = None
    asset_type: AssetTypeValue | None = None
    market: Market | None = None
    broker: StrictStr | None = None
    account: StrictStr | None = None
    portfolio: StrictStr | None = None
    notes: StrictStr | None = None
    watch_tier: WatchTier | None = None

    @field_validator("shares", "avg_cost", "current_value", mode="before")
    @classmethod
    def amount(cls, value: object) -> Decimal | None:
        import re

        if value is None:
            return None
        if not isinstance(value, str) or re.fullmatch(r"(0|[1-9][0-9]*)(\.[0-9]+)?", value) is None:
            raise ValueError("amount must be a nonnegative fixed decimal string")
        return Decimal(value)

    @field_serializer("shares", "avg_cost", "current_value", when_used="json")
    def serialize_amount(self, value: Decimal | None) -> str | None:
        return fixed(value) if value is not None else None


class AgentHoldingRecord(AgentHoldingInput):
    id: UUID


class WholeRow(AgentHoldingInput):
    id: UUID | None = None


class Create(StrictModel):
    version: Version
    holding: AgentHoldingInput


class ReplaceAll(StrictModel):
    version: Version
    holdings: list[WholeRow]
    delete_ids: list[UUID]


class Changes(StrictModel):
    name: StrictStr | None = None
    currency: StrictStr | None = None
    pricing_mode: PricingMode | None = None
    ticker: StrictStr | None = None
    fund_code: StrictStr | None = None
    shares: Decimal | None = None
    avg_cost: Decimal | None = None
    current_value: Decimal | None = None
    asset_type: AssetTypeValue | None = None
    market: Market | None = None
    broker: StrictStr | None = None
    account: StrictStr | None = None
    portfolio: StrictStr | None = None
    notes: StrictStr | None = None
    watch_tier: WatchTier | None = None

    @field_validator("shares", "avg_cost", "current_value", mode="before")
    @classmethod
    def amount(cls, value: object) -> Decimal | None:
        return AgentHoldingInput.amount(value)

    @field_serializer("shares", "avg_cost", "current_value", when_used="json")
    def serialize_amount(self, value: Decimal | None) -> str | None:
        return fixed(value) if value is not None else None

    @field_validator("name", "currency", "pricing_mode")
    @classmethod
    def required_not_null(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("required fields cannot be null")
        return value


class Patch(StrictModel):
    version: Version
    changes: Changes


class ValidateCreate(Create):
    operation: Literal["create"]


class ValidateAll(ReplaceAll):
    operation: Literal["replace_all"]


class ValidateReplace(Create):
    operation: Literal["replace"]
    holding_id: UUID


class ValidatePatch(Patch):
    operation: Literal["patch"]
    holding_id: UUID


Validation = Annotated[
    ValidateCreate | ValidateAll | ValidateReplace | ValidatePatch, Field(discriminator="operation")
]
