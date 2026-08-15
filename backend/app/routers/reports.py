from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_session
from app.core.deps import get_current_user_id
from app.models.report import Report
from app.schemas.reports import GenerateReportRequest, ReportListItem, ReportOut
from app.services.email_sender import send_report_email
from app.services.report_generator import generate_report, regenerate_report
from app.services.report_llm import LLMEmptyResponseError

router = APIRouter()


@router.post("/generate", response_model=ReportOut, status_code=201)
def trigger_report_generation(
    req: GenerateReportRequest,
    session: Session = Depends(get_session),
) -> Report:
    """Manually trigger report generation (Ring 0 entry point before Celery).

    The synchronous path has no Celery retry, so the two transient LLM failure
    modes — a malformed empty-choices response and the Pass-2 completeness guard
    (RuntimeError on a truncated body) — would otherwise surface as a bare 500
    with no diagnosis. Translate them to a 502 carrying the reason. (I-DEBT-4)
    """
    try:
        return generate_report(
            session,
            report_date=req.report_date,
            report_type=req.report_type,
            base_currency=req.base_currency,
            output_lang=get_settings().OUTPUT_LANG,
            session_node=req.session_node,
        )
    except LLMEmptyResponseError as exc:
        raise HTTPException(
            status_code=502, detail=f"LLM returned an empty response: {exc}"
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"Report generation failed: {exc}") from exc


@router.post("/{report_id}/regenerate", response_model=ReportOut)
def regenerate(
    report_id: uuid.UUID,
    mode: str = "render",
    output_lang: str | None = None,
    resend: bool = False,
    session: Session = Depends(get_session),
) -> Report:
    """Rebuild a report from stored inputs without re-fetching intel (#6).

    mode=render re-renders from the stored Pass 2 body (token-free except
    translation); mode=analyze re-runs Pass 2 from the stored intel.
    resend=true sends the email after a successful regeneration (status=success).
    Defaults output language to OUTPUT_LANG.
    """
    if mode not in ("render", "analyze"):
        raise HTTPException(status_code=422, detail="mode must be 'render' or 'analyze'")
    lang = output_lang or get_settings().OUTPUT_LANG
    try:
        report = regenerate_report(session, report_id, mode=mode, output_lang=lang)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if resend and report.status == "success":
        send_report_email(report, session)
    return report


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


@router.post("/{report_id}/send", status_code=200)
def send_report(
    report_id: uuid.UUID,
    session: Session = Depends(get_session),
) -> dict[str, str | None]:
    """Manually trigger (or re-check) email delivery for an existing report."""
    user_id = get_current_user_id()
    report = session.execute(
        select(Report).where(Report.id == report_id, Report.user_id == user_id)
    ).scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    if report.status != "success":
        raise HTTPException(status_code=422, detail="Report is not in success state")
    if report.email_sent_at is not None:
        return {
            "status": "already_sent",
            "email_sent_at": report.email_sent_at.isoformat(),
        }
    delivered = send_report_email(report, session)
    if not delivered:
        raise HTTPException(status_code=502, detail="Email delivery failed — check server logs")
    return {
        "status": "sent",
        "email_sent_at": report.email_sent_at.isoformat() if report.email_sent_at else None,
    }


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
