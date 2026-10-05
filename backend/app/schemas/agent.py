"""Agent audit metadata exposed to Ops, without portfolio content."""

import datetime as dt
from datetime import datetime
from typing import Literal
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


class AgentReportOut(BaseModel):
    id: UUID
    report_date: dt.date
    type: Literal["daily", "mwf", "weekly", "manual", "other"]
    status: Literal["success", "skipped"]
    period_start: dt.datetime | None
    period_end: dt.datetime | None
    generated_at: dt.datetime | None
    report_md: str | None


class AgentReportsOut(BaseModel):
    start: dt.date
    end: dt.date
    total_in_range: int
    truncated: bool
    items: list[AgentReportOut]


class AgentHeadlineOut(BaseModel):
    title: str
    published_at: dt.datetime
    summary: str | None


class AgentArticleOut(BaseModel):
    title: str
    published_at: str | None
    body: str


class AgentHoldingIntelOut(BaseModel):
    identifier: str
    headlines: list[AgentHeadlineOut]
    articles: list[AgentArticleOut]


class AgentMacroThemeOut(BaseModel):
    theme: str
    articles: list[AgentArticleOut]


class AgentMacroOut(BaseModel):
    source_report_id: UUID
    source_report_date: dt.date
    themes: list[AgentMacroThemeOut]


class AgentIntelOut(BaseModel):
    date: dt.date
    holdings: list[AgentHoldingIntelOut]
    macro: AgentMacroOut | None
