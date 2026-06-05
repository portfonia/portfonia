from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel


class ReportOut(BaseModel):
    id: uuid.UUID
    report_date: date
    report_type: str
    status: str
    prompt_version: str | None
    disclaimer_version: str | None
    report_md: str | None
    generated_at: datetime | None
    email_sent_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ReportListItem(BaseModel):
    id: uuid.UUID
    report_date: date
    report_type: str
    status: str
    generated_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class GenerateReportRequest(BaseModel):
    report_date: date | None = None
    report_type: str = "weekly"
    base_currency: str = "USD"
