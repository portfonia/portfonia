"""On-demand report generation jobs (issue #193).

POST /reports/generate used to run the full pipeline (Pass 1 + shared intel
+ Pass 2 + render + email, routinely minutes) inside the request. Behind the
frontend's Next.js rewrites() proxy the connection is dropped around ~30s, so
the browser saw a plain-text 500 from the proxy while the worker-less backend
kept going and usually succeeded (production timeline in issue #193's Reasons
comment) — a false failure the caller could not distinguish from a real one.

These tests cover the contract that replaces it: a fast accept that returns a
durable job handle (202, no pipeline work in the request), an owner-scoped
poll, and a Celery worker that links the job to the report its existing
generate_report() call produced — or records the failure detail the sync
endpoint used to translate into a 502.
"""

from __future__ import annotations

import datetime as _dt
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.report import Report
from app.models.report_job import ReportJob
from app.models.user import User
from app.services.llm_errors import LLMEmptyResponseError
from app.tasks.report_tasks import generate_report_job
from app.tests.conftest import TEST_USER_ID, U2_USER_ID, seed_user

_OTHER_USER_ID = U2_USER_ID


def _seed(session: Session, user_id: uuid.UUID = TEST_USER_ID) -> None:
    """report_jobs.user_id FKs to a real users row (issue #129 B7 convention)."""
    if session.get(User, user_id) is None:
        seed_user(session, user_id)


def _reload(session: Session, job_id: uuid.UUID) -> ReportJob:
    """Re-read a job the worker committed through its own session: the test
    session's identity map still holds the pre-worker instance."""
    session.expire_all()
    job = session.get(ReportJob, job_id)
    assert job is not None
    return job


def _make_report(
    session: Session,
    *,
    user_id: uuid.UUID = TEST_USER_ID,
    report_date: _dt.date = _dt.date(2026, 9, 10),
    status: str = "success",
) -> Report:
    _seed(session, user_id)
    report = Report(
        user_id=user_id,
        report_date=report_date,
        report_type="incremental",
        session_node="manual",
        status=status,
        report_md="# Report\n\nBody",
    )
    session.add(report)
    session.flush()
    session.refresh(report)
    return report


def _make_job(
    session: Session, *, user_id: uuid.UUID = TEST_USER_ID, status: str = "pending"
) -> ReportJob:
    _seed(session, user_id)
    job = ReportJob(user_id=user_id, status=status)
    session.add(job)
    session.flush()
    session.refresh(job)
    return job


# ---------------------------------------------------------------------------
# Accept: 202 + a durable handle, no pipeline work in the request
# ---------------------------------------------------------------------------


def test_generate_accepts_fast_with_a_pending_job(
    app_client: TestClient, db_session: Session
) -> None:
    """Requirement 1/2: the accept returns a pollable handle immediately and
    never runs generate_report in the request."""
    _seed(db_session)
    with (
        patch("app.routers.reports.generate_report_job.delay") as mock_delay,
        patch("app.services.report_generator.generate_report") as mock_generate,
    ):
        resp = app_client.post("/reports/generate", json={"report_type": "incremental"})

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "pending"
    assert body["report_id"] is None
    assert body["error"] is None
    mock_generate.assert_not_called()

    job = db_session.get(ReportJob, uuid.UUID(body["id"]))
    assert job is not None
    assert job.user_id == TEST_USER_ID
    assert job.status == "pending"
    mock_delay.assert_called_once()


def test_generate_forwards_the_request_body_to_the_worker(
    app_client: TestClient, db_session: Session
) -> None:
    """The accepted request must reach the worker intact — report_date is
    serialized because Celery's JSON serializer does not carry a date."""
    _seed(db_session)
    with patch("app.routers.reports.generate_report_job.delay") as mock_delay:
        resp = app_client.post(
            "/reports/generate",
            json={
                "report_type": "incremental",
                "report_date": "2026-09-09",
                "base_currency": "HKD",
                "session_node": "manual",
            },
        )

    assert resp.status_code == 202
    assert mock_delay.call_args.args == (resp.json()["id"],)
    assert mock_delay.call_args.kwargs == {
        "report_type": "incremental",
        "report_date": "2026-09-09",
        "base_currency": "HKD",
        "session_node": "manual",
    }


def test_generate_still_validates_the_request_body(
    app_client: TestClient, db_session: Session
) -> None:
    with patch("app.routers.reports.generate_report_job.delay") as mock_delay:
        resp = app_client.post("/reports/generate", json={"report_type": "not-a-type"})

    assert resp.status_code == 422
    mock_delay.assert_not_called()


def test_generate_marks_the_job_failed_when_the_enqueue_fails(
    app_client: TestClient, db_session: Session
) -> None:
    """A broker blip must not leave a permanently pending job the client
    would poll forever (same rule as the holdings upload accept)."""
    _seed(db_session)
    with patch(
        "app.routers.reports.generate_report_job.delay",
        side_effect=RuntimeError("broker down"),
    ):
        resp = app_client.post("/reports/generate", json={"report_type": "incremental"})

    assert resp.status_code == 503
    jobs = db_session.query(ReportJob).filter(ReportJob.user_id == TEST_USER_ID).all()
    assert len(jobs) == 1
    assert jobs[0].status == "failed"
    assert "broker down" in (jobs[0].error or "")


# ---------------------------------------------------------------------------
# Poll: owner-scoped only
# ---------------------------------------------------------------------------


def test_get_job_returns_the_callers_own_job(app_client: TestClient, db_session: Session) -> None:
    job = _make_job(db_session, status="success")
    db_session.commit()

    resp = app_client.get(f"/reports/jobs/{job.id}")

    assert resp.status_code == 200
    assert resp.json()["id"] == str(job.id)
    assert resp.json()["status"] == "success"


def test_get_job_404_for_another_users_job(app_client: TestClient, db_session: Session) -> None:
    """No cross-user job read — and a 404, not a 403, so a guessed job id
    cannot be distinguished from a non-existent one."""
    theirs = _make_job(db_session, user_id=_OTHER_USER_ID)
    db_session.commit()

    resp = app_client.get(f"/reports/jobs/{theirs.id}")

    assert resp.status_code == 404


def test_get_job_404_for_unknown_id(app_client: TestClient, db_session: Session) -> None:
    resp = app_client.get(f"/reports/jobs/{uuid.uuid4()}")

    assert resp.status_code == 404


def test_get_job_surfaces_the_report_rows_own_status(
    app_client: TestClient, db_session: Session
) -> None:
    """Job status is the pipeline's outcome; the report's own status stays
    authoritative for delivery (a compliance hold is not "success" for the
    user), so the poll carries both."""
    report = _make_report(db_session, status="needs_review")
    job = _make_job(db_session, status="success")
    job.report_id = report.id
    db_session.commit()

    resp = app_client.get(f"/reports/jobs/{job.id}")

    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    assert resp.json()["report_id"] == str(report.id)
    assert resp.json()["report_status"] == "needs_review"


# ---------------------------------------------------------------------------
# Worker: existing generate_report, scoped to the job's owner
# ---------------------------------------------------------------------------


def test_worker_success_links_the_report_id(db_session: Session) -> None:
    report = _make_report(db_session)
    job = _make_job(db_session)

    with patch("app.services.report_generator.generate_report", return_value=report) as mock_gen:
        result = generate_report_job.run(str(job.id))  # bypasses Celery routing

    assert result == {"job_id": str(job.id), "status": "success", "report_id": str(report.id)}
    assert mock_gen.call_args.kwargs["user_id"] == TEST_USER_ID
    assert mock_gen.call_args.kwargs["report_date"] is None
    assert mock_gen.call_args.kwargs["session_node"] == "manual"
    job = _reload(db_session, job.id)
    assert job.status == "success"
    assert job.report_id == report.id
    assert job.error is None


def test_worker_uses_the_job_rows_owner_and_accepted_arguments(db_session: Session) -> None:
    """Identity comes from the job row (the Celery process has no
    principal), and the accepted request's own fields are what the pipeline
    is called with — including session_node, which is part of the report
    dedup key."""
    report = _make_report(db_session, user_id=_OTHER_USER_ID)
    job = _make_job(db_session, user_id=_OTHER_USER_ID)

    with patch("app.services.report_generator.generate_report", return_value=report) as mock_gen:
        generate_report_job.run(
            str(job.id),
            report_type="incremental",
            report_date="2026-09-09",
            base_currency="HKD",
            session_node="after_close",
        )

    assert mock_gen.call_args.kwargs["user_id"] == _OTHER_USER_ID
    assert mock_gen.call_args.kwargs["report_date"] == _dt.date(2026, 9, 9)
    assert mock_gen.call_args.kwargs["base_currency"] == "HKD"
    assert mock_gen.call_args.kwargs["session_node"] == "after_close"


def test_worker_resolves_the_owners_report_language_and_currency(db_session: Session) -> None:
    """Issues #308/#350 carry over unchanged into the async path: with no
    explicit base_currency in the body, both values come from the job's own
    user, not a system-wide default."""
    report = _make_report(db_session)
    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    user.locale = "en"
    user.base_currency = "CNY"
    db_session.flush()
    job = _make_job(db_session)

    with patch("app.services.report_generator.generate_report", return_value=report) as mock_gen:
        generate_report_job.run(str(job.id))

    assert mock_gen.call_args.kwargs["output_lang"] == "en"
    assert mock_gen.call_args.kwargs["base_currency"] == "CNY"


def test_worker_failure_marks_the_job_failed_with_detail(db_session: Session) -> None:
    job = _make_job(db_session)

    with patch(
        "app.services.report_generator.generate_report",
        side_effect=RuntimeError("Pass 2 output looks truncated"),
    ):
        result = generate_report_job.run(str(job.id))

    assert result == {"job_id": str(job.id), "status": "failed", "report_id": None}
    job = _reload(db_session, job.id)
    assert job.status == "failed"
    assert job.report_id is None
    # Same detail string the sync endpoint used to return as a 502.
    assert job.error == "Report generation failed: Pass 2 output looks truncated"


def test_worker_maps_an_empty_llm_response_to_its_own_detail(db_session: Session) -> None:
    job = _make_job(db_session)

    with patch(
        "app.services.report_generator.generate_report",
        side_effect=LLMEmptyResponseError("choices=None"),
    ):
        generate_report_job.run(str(job.id))

    job = _reload(db_session, job.id)
    assert job.status == "failed"
    assert job.error == "LLM returned an empty response: choices=None"


def test_worker_records_an_unexpected_error_without_leaving_the_job_pending(
    db_session: Session,
) -> None:
    job = _make_job(db_session)

    with patch("app.services.report_generator.generate_report", side_effect=ValueError("boom")):
        result = generate_report_job.run(str(job.id))

    assert result["status"] == "failed"
    job = _reload(db_session, job.id)
    assert job.status == "failed"
    assert job.error == "Report generation error: ValueError: boom"


def test_worker_leaves_a_terminal_job_alone_on_redelivery(db_session: Session) -> None:
    """task_acks_late=True means a worker killed after this task's commit but
    before its ack gets the same message again — the second run must not
    re-run the pipeline over an already-resolved job."""
    report = _make_report(db_session)
    job = _make_job(db_session, status="success")
    job.report_id = report.id
    db_session.commit()

    with patch("app.services.report_generator.generate_report") as mock_gen:
        result = generate_report_job.run(str(job.id))

    mock_gen.assert_not_called()
    assert result == {"job_id": str(job.id), "status": "success", "report_id": str(report.id)}


def test_worker_returns_not_found_for_a_missing_job(db_session: Session) -> None:
    with patch("app.services.report_generator.generate_report") as mock_gen:
        result = generate_report_job.run(str(uuid.uuid4()))

    mock_gen.assert_not_called()
    assert result == {"status": "job_not_found"}


# ---------------------------------------------------------------------------
# Ops purge: both FKs cascade, so the existing delete order still holds
# ---------------------------------------------------------------------------


def test_user_purge_collects_report_jobs(db_session: Session) -> None:
    """report_jobs.user_id and report_jobs.report_id both CASCADE: the admin
    purge path deletes reports and then users, and must not hit a foreign key
    from a job row it knows nothing about (report_jobs is a dependent
    operational record — see the migration's docstring)."""
    from app.services.user_purge import purge_user

    report = _make_report(db_session)
    linked = _make_job(db_session, status="success")
    linked.report_id = report.id
    _make_job(db_session)
    db_session.flush()

    purge_user(db_session, TEST_USER_ID)

    remaining = db_session.query(ReportJob).filter(ReportJob.user_id == TEST_USER_ID).count()
    assert remaining == 0
