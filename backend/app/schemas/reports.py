from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from app.schemas.holdings import VALID_CURRENCIES
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


class ReportJobOut(BaseModel):
    """Poll target for an on-demand report generation (issue #193).

    `status` is the JOB's state — "pending" -> a worker still owns it,
    "success" -> the pipeline ran to a terminal report row, "failed" ->
    `error` carries the detail (the message that used to be the sync
    endpoint's 502 body).

    `report_status` is the generated report row's OWN status, read through
    `report_id`: job success says the pipeline finished, not that the user
    was sent anything — a compliance hold (`needs_review`) or a quiet-day
    `skipped` row is still a completed job. Both are null until a run
    produces a report.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: Literal["pending", "success", "failed"]
    report_id: uuid.UUID | None = None
    report_status: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class GenerateReportRequest(BaseModel):
    report_date: date | None = None
    report_type: str = "incremental"
    # Issue #350 item 1: None (not a hardcoded "USD" default) means "use the
    # requesting user's own persisted users.base_currency preference" — see
    # routers/reports.py's trigger_report_generation, which resolves this via
    # report_currency_for(). An explicit value here still overrides that
    # preference for this one call (untouched escape hatch).
    base_currency: str | None = None
    # H-DEBT-1: identifies WHICH trigger produced the report so a same-day
    # scheduled run (session_node="after_close") doesn't collide with an
    # earlier manual run. Defaults to "manual" for this API entry point.
    session_node: str = "manual"

    @field_validator("report_type")
    @classmethod
    def _validate_report_type(cls, v: str) -> str:
        validate_report_type(v)
        return v

    @field_validator("base_currency")
    @classmethod
    def _validate_base_currency(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_CURRENCIES:
            raise ValueError(f"unrecognized currency {v!r} — not in VALID_CURRENCIES")
        return v
