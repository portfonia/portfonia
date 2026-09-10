from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_session
from app.core.deps import Principal, current_principal
from app.models.report import Report
from app.models.report_job import ReportJob
from app.schemas.reports import (
    GenerateReportRequest,
    ReportJobOut,
    ReportListItem,
    ReportOut,
)
from app.services.email_sender import send_report_email
from app.services.report_generator import regenerate_report
from app.services.user_scope import report_currency_for, report_language_for
from app.tasks.report_tasks import generate_report_job

logger = logging.getLogger(__name__)

router = APIRouter()


def _job_out(session: Session, job: ReportJob) -> ReportJobOut:
    """Job fields plus the linked report's own status (see ReportJobOut)."""
    out = ReportJobOut.model_validate(job)
    if job.report_id is not None:
        report = session.get(Report, job.report_id)
        out.report_status = report.status if report is not None else None
    return out


@router.post("/generate", response_model=ReportJobOut, status_code=status.HTTP_202_ACCEPTED)
def trigger_report_generation(
    req: GenerateReportRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> ReportJobOut:
    """Accept an on-demand generation and return a poll handle (issue #193).

    Was synchronous: the full Pass-1/Pass-2/runtime ran for minutes inside
    this request, and through the frontend's Next.js `rewrites()` proxy the
    client got a plain-text 500 at ~30s (`Failed to proxy …
    ECONNRESET`) while the backend went on to finish and email — a false
    failure with no honest signal. The pipeline now runs in a Celery task
    (`generate_report_job`) against the `report_jobs` row this returns, and
    the caller polls `GET /reports/jobs/{job_id}`.

    The request body is unchanged (`GenerateReportRequest`), including the
    `session_node` default of "manual" — that value is part of
    generate_report's same-day dedup key, so a repeat trigger still reuses
    the day's existing row instead of double-emailing. Passing the body
    through as task arguments is safe: it carries no holdings content, and a
    Celery redelivery replays the same values.

    An enqueue failure marks the job failed and returns 503 rather than
    leaving a pending row no worker will ever pick up.
    """
    job = ReportJob(user_id=principal.user_id, status="pending")
    session.add(job)
    session.commit()
    session.refresh(job)
    try:
        generate_report_job.delay(
            str(job.id),
            report_type=req.report_type,
            # Celery's JSON serializer does not carry a date.
            report_date=req.report_date.isoformat() if req.report_date is not None else None,
            base_currency=req.base_currency,
            session_node=req.session_node,
        )
    except Exception as exc:
        logger.exception("trigger_report_generation: failed to enqueue job %s", job.id)
        job.status = "failed"
        job.error = f"Failed to queue report job: {type(exc).__name__}: {exc}"
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not queue the report generation. Please try again.",
        ) from exc
    return _job_out(session, job)


@router.get("/jobs/{job_id}", response_model=ReportJobOut)
def get_report_job(
    job_id: uuid.UUID,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> ReportJobOut:
    """Poll one of the caller's own generation jobs (issue #193).

    Owner-scoped: another user's job id is a 404, not a 403, so a guessed id
    is indistinguishable from a non-existent one.
    """
    job = session.get(ReportJob, job_id)
    if job is None or job.user_id != principal.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report job not found.")
    return _job_out(session, job)


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
