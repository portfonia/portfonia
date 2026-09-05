from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_session
from app.core.deps import Principal, current_principal
from app.models.report import Report
from app.schemas.reports import GenerateReportRequest, ReportListItem, ReportOut
from app.services.email_sender import send_report_email
from app.services.llm_errors import LLMEmptyResponseError
from app.services.report_generator import generate_report, regenerate_report
from app.services.user_scope import report_currency_for, report_language_for

router = APIRouter()


@router.post("/generate", response_model=ReportOut, status_code=201)
def trigger_report_generation(
    req: GenerateReportRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
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
            user_id=principal.user_id,
            report_date=req.report_date,
            report_type=req.report_type,
            # Issue #350 item 1: an explicit ?base_currency in the request
            # body still wins (untouched escape hatch), otherwise the
            # requesting user's own persisted preference — mirrors
            # output_lang's precedence immediately below.
            base_currency=req.base_currency
            or report_currency_for(session, principal.user_id, "USD"),
            # Issue #308: the requesting user's own report language, not the
            # global Settings.OUTPUT_LANG default.
            output_lang=report_language_for(session, principal.user_id, get_settings().OUTPUT_LANG),
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
    base_currency: str | None = None,
    resend: bool = False,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> Report:
    """Rebuild a report from stored inputs without re-fetching intel (#6).

    mode=render re-renders from the stored Pass 2 body (token-free except
    translation); mode=analyze re-runs Pass 2 from the stored intel.
    resend=true sends the email after a successful regeneration (status=success).

    Defaults output language to the report's owning user's own report
    language (issue #308) — regenerate is scoped to the caller's own
    reports (see regenerate_report's user_id filter), so that owning user
    is always the calling principal. The explicit ?output_lang= query
    param stays an untouched ops/debug escape hatch that overrides this
    default, unrelated to this issue.

    Issue #350 item 1: `base_currency` follows the identical precedence —
    the explicit ?base_currency= query param wins, else the caller's own
    persisted preference. Only affects mode="analyze" (see
    regenerate_report's own docstring).
    """
    if mode not in ("render", "analyze"):
        raise HTTPException(status_code=422, detail="mode must be 'render' or 'analyze'")
    lang = output_lang or report_language_for(
        session, principal.user_id, get_settings().OUTPUT_LANG
    )
    currency = base_currency or report_currency_for(session, principal.user_id, "USD")
    try:
        report = regenerate_report(
            session,
            report_id,
            user_id=principal.user_id,
            mode=mode,
            output_lang=lang,
            base_currency=currency,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if resend and report.status == "success":
        send_report_email(report, session)
    return report


@router.get("/", response_model=list[ReportListItem])
def list_reports(
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> list[Report]:
    user_id = principal.user_id
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
    principal: Principal = Depends(current_principal),
) -> dict[str, str | None]:
    """Manually trigger (or re-check) email delivery for an existing report."""
    user_id = principal.user_id
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
    principal: Principal = Depends(current_principal),
) -> Report:
    user_id = principal.user_id
    report = session.execute(
        select(Report).where(Report.id == report_id, Report.user_id == user_id)
    ).scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return report
