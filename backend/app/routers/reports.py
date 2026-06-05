from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import get_current_user_id
from app.models.report import Report
from app.schemas.reports import GenerateReportRequest, ReportListItem, ReportOut
from app.services.report_generator import generate_report

router = APIRouter()


@router.post("/generate", response_model=ReportOut, status_code=201)
def trigger_report_generation(
    req: GenerateReportRequest,
    session: Session = Depends(get_session),
) -> Report:
    """Manually trigger report generation (Ring 0 entry point before Celery)."""
    return generate_report(
        session,
        report_date=req.report_date,
        report_type=req.report_type,
        base_currency=req.base_currency,
    )


@router.get("/", response_model=list[ReportListItem])
def list_reports(session: Session = Depends(get_session)) -> list[Report]:
    user_id = get_current_user_id()
    rows = session.execute(
        select(Report)
        .where(Report.user_id == user_id)
        .order_by(Report.report_date.desc(), Report.created_at.desc())
        .limit(20)
    ).scalars()
    return list(rows)


@router.get("/{report_id}", response_model=ReportOut)
def get_report(
    report_id: uuid.UUID,
    session: Session = Depends(get_session),
) -> Report:
    user_id = get_current_user_id()
    report = session.execute(
        select(Report).where(Report.id == report_id, Report.user_id == user_id)
    ).scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return report
