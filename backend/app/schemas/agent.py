"""Agent audit metadata exposed to Ops, without portfolio content."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ApiAuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    occurred_at: datetime
    user_id: UUID | None
    token_id: UUID | None
    token_prefix: str | None
    endpoint: str
    params: dict[str, str]
    status_code: int
    item_count: int | None
    client_ip: str
    user_agent: str | None


class ApiAuditPage(BaseModel):
    rows: list[ApiAuditOut]
    truncated: bool


class RevokedTokensOut(BaseModel):
    revoked_count: int
