"""Holding history and Agent token response models."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class ChangedField(BaseModel):
    field: str
    before: str | int | None
    after: str | int | None


class ChangedRow(BaseModel):
    holding_id: UUID
    holding_name: str
    kind: Literal["created", "updated", "deleted"]
    fields: list[ChangedField]


class ChangeToken(BaseModel):
    name: str
    prefix: str


class HoldingChangeOut(BaseModel):
    id: UUID
    changed_at: datetime
    source: Literal["web", "file", "agent", "rollback"]
    rollback_to: datetime | None
    token: ChangeToken | None
    rows: list[ChangedRow]


class HoldingChangesOut(BaseModel):
    start: date
    end: date
    truncated: bool
    items: list[HoldingChangeOut]


class WebHoldingChangeOut(HoldingChangeOut):
    rollback_available: bool


class WebHoldingChangesOut(BaseModel):
    start: date
    end: date
    truncated: bool
    items: list[WebHoldingChangeOut]


class HoldingRollbackPreview(BaseModel):
    target_changed_at: datetime
    groups: int
    restored: int
    removed: int
    modified: int
    version: str


class HoldingRollbackIn(BaseModel):
    version: str


class AgentCapabilities(BaseModel):
    reports: bool
    snapshots: bool
    intel: bool
    holdings_read: bool
    holdings_write: bool
    jade: bool


class AgentTokenOut(BaseModel):
    name: str
    prefix: str
    permission: Literal["read_only", "holdings_write"]
    created_at: datetime
    expires_at: datetime | None
    capabilities: AgentCapabilities
