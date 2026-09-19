from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# GET /vigil/vault response shape — #452 Design comment / #450 Design
# section 5 (incorporated by reference), object_summary and full field
# list. `active`/`pending`/`recipients`/`delivery_status` stay structurally
# present but always empty/None as of #454 too: vigil_configurations and
# vigil_objects now exist and GET /vigil/vault could read them, but this
# route doesn't decrypt/populate the summary yet (no masking policy for
# `recipients`/`filename` decided at this checkpoint — blacktomb42 PR #507
# review: "reasonable until decrypt/masking policy", not a gap to silently
# paper over). Filled in once that policy exists.


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


# POST /vigil/configurations / /vigil/objects/init / /vigil/objects/upload
# (issue #454, Vigil R0 P2.1) — #450 Design section 5. `extra="forbid"`
# throughout matches "Unknown fields -> 422" from that section; validation
# beyond basic shape (interval/grace bounds, recipient count/dedupe, email
# normalization, revision locking) happens in services/vigil/configuration.py
# and services/vigil/objects.py, not here. Save-time DNS/MX validation was
# removed (issue #524, #516 finding 2) — see git history for #454's original.


class VigilRecipientIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    email_confirm: str


class VigilConfigurationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int
    interval_days: int | None = None
    grace_hours: int | None = None
    recipients: list[VigilRecipientIn]
    message: str | None = None


class VigilConfigurationOut(BaseModel):
    vault_id: UUID
    config_id: UUID
    revision: int


class VigilObjectInitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int
    config_id: UUID
    request_id: UUID
    filename: str
    plaintext_size: int


class VigilObjectInitOut(BaseModel):
    object_id: UUID
    revision: int


class VigilObjectUploadOut(BaseModel):
    object_id: UUID
    status: str
    revision: int


class VigilDrillIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int
    config_id: UUID
    object_id: UUID


class VigilDrillOut(BaseModel):
    drill_id: UUID
    status: str
    revision: int


class VigilArmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int
    config_id: UUID
    object_id: UUID


class VigilArmOut(BaseModel):
    phase: str
    revision: int
    next_check_at: str


class VigilCheckInIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int


class VigilCheckInOut(BaseModel):
    phase: str
    revision: int
    next_check_at: str | None = None


class VigilPublicConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str
    altcha: str


class VigilPublicConfirmOut(BaseModel):
    result: str
    next_check_at: str | None = None
