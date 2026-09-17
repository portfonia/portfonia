from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

# GET /vigil/vault response shape — #452 Design comment / #450 Design
# section 5 (incorporated by reference), object_summary and full field
# list. `active`/`pending`/`recipients`/`delivery_status` stay structurally
# present but always empty/None at this checkpoint: vigil_configurations
# and vigil_objects (#454+) don't exist yet, so there is nothing to
# populate them from.


class VigilObjectSummary(BaseModel):
    id: UUID
    filename: str
    plaintext_size: int
    status: str
    has_password: bool | None


class VigilVaultStatus(BaseModel):
    vault_id: UUID | None
    phase: str
    revision: int
    hold_reason: str | None = None
    next_check_at: datetime | None = None
    deadline_at: datetime | None = None
    last_scan_completed_at: datetime | None = None
    active: VigilObjectSummary | None = None
    pending: VigilObjectSummary | None = None
    recipients: list[str] = Field(default_factory=list)
    delivery_status: list[Any] = Field(default_factory=list)
