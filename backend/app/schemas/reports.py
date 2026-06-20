from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, field_validator

from app.services.report_types import validate_report_type


class ReportOut(BaseModel):
    id: uuid.UUID
    report_date: date
    report_type: str
    session_node: str
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
    session_node: str
    status: str
    generated_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class GenerateReportRequest(BaseModel):
    report_date: date | None = None
    report_type: str = "incremental"
    base_currency: str = "USD"
    # H-DEBT-1: identifies WHICH trigger produced the report so a same-day
    # scheduled run (session_node="after_close") doesn't collide with an
    # earlier manual run. Defaults to "manual" for this API entry point.
    session_node: str = "manual"

    @field_validator("report_type")
    @classmethod
    def _validate_report_type(cls, v: str) -> str:
        validate_report_type(v)
        return v
