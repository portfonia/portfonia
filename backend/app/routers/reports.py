from __future__ import annotations

import uuid
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import Principal, current_principal
from app.models.report import Report
from app.schemas.reports import ReportDetailOut, ReportListItem, ReportListPage, ReportOut
from app.services.email_sender import render_report_body_html

router = APIRouter()
REPORT_LIST_PAGE_SIZE = 20
_DISPLAY_STATES: dict[str, Literal["available", "under_review", "generating"]] = {
    "success": "available",
    "skipped": "available",
    "needs_review": "under_review",
    "in_progress": "generating",
}


@router.get("", response_model=ReportListPage)
def list_reports(
    page: int = Query(default=1, ge=1),
    sort: Literal["desc", "asc"] = "desc",
    date_from: date | None = None,
    date_to: date | None = None,
    kind: Literal["report"] = "report",
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> ReportListPage:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be after date_to")
    query = select(Report).where(
        Report.user_id == principal.user_id, Report.status.in_(_DISPLAY_STATES)
    )
    if date_from is not None:
        query = query.where(Report.report_date >= date_from)
    if date_to is not None:
        query = query.where(Report.report_date <= date_to)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    order = (
        (Report.report_date.desc(), Report.created_at.desc())
        if sort == "desc"
        else (Report.report_date.asc(), Report.created_at.asc())
    )
    rows = session.scalars(
        query.order_by(*order)
        .limit(REPORT_LIST_PAGE_SIZE)
        .offset((page - 1) * REPORT_LIST_PAGE_SIZE)
    )
    items = [
        ReportListItem(
            id=r.id,
            report_date=r.report_date,
            report_type=r.report_type,
            session_node=r.session_node,
            status=r.status,
            generated_at=r.generated_at,
            created_at=r.created_at,
            kind=kind,
            display_state=_DISPLAY_STATES[r.status],
        )
        for r in rows
    ]
    return ReportListPage(
        items=items, page=page, page_size=REPORT_LIST_PAGE_SIZE, total=total, kinds=["report"]
    )


@router.get("/{report_id}", response_model=ReportDetailOut)
def get_report(
    report_id: uuid.UUID,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> ReportDetailOut:
    report = session.scalar(
        select(Report).where(
            Report.id == report_id,
            Report.user_id == principal.user_id,
            Report.status.in_(("success", "skipped")),
        )
    )
    if report is None or report.report_md is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return ReportDetailOut(
        **ReportOut.model_validate(report).model_dump(),
        report_body_html=render_report_body_html(report.report_md),
    )
