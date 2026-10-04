from sqlalchemy.orm import Session

from app.models.report import Report
from app.models.report_job import ReportJob
from app.schemas.reports import ReportJobOut


def _job_out(session: Session, job: ReportJob) -> ReportJobOut:
    """Read the linked report's status separately from the job outcome."""
    out = ReportJobOut.model_validate(job)
    if job.report_id is not None:
        report = session.get(Report, job.report_id)
        out.report_status = report.status if report is not None else None
    return out
