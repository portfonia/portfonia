"""Issue #642 acceptance tests against real PostgreSQL."""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.report import Report
from app.models.report_job import ReportJob
from app.models.user import User
from app.tests.conftest import TEST_USER_ID, seed_user
from app.tests.test_admin_router import _headers


@pytest.fixture(autouse=True)
def no_generation_network(monkeypatch: pytest.MonkeyPatch) -> None:
    for target in (
        "app.services.report_llm._call_llm",
        "app.services.report_generator._call_llm",
        "app.services.report_generator.generate_report",
    ):
        monkeypatch.setattr(
            target,
            MagicMock(side_effect=AssertionError("report generation must be mocked")),
            raising=False,
        )


def row(
    session: Session,
    *,
    owner: UUID = TEST_USER_ID,
    status: str = "success",
    day: date = date(2026, 10, 1),
    node: str = "manual",
    md: str | None = "# Briefing\n\nBody",
    created: datetime | None = None,
) -> Report:
    if session.get(User, owner) is None:
        seed_user(session, owner)
    report = Report(
        user_id=owner,
        status=status,
        report_date=day,
        report_type="incremental",
        session_node=node,
        report_md=md,
        report_html="private unsubscribe",
        report_inputs={"private": True},
    )
    if created is not None:
        report.created_at = created
    session.add(report)
    session.flush()
    return report


def test_acceptance_01_pagination_order_and_failed_exclusion(
    app_client: TestClient, db_session: Session
) -> None:
    visible = [
        row(
            db_session,
            status=("success", "skipped", "needs_review", "in_progress")[i % 4],
            day=date(2026, 10, 1) + timedelta(days=i // 2),
            node=f"fixture-{i}",
            created=datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=i),
        )
        for i in range(25)
    ]
    failed = [row(db_session, status="failed", node=f"failed-{i}") for i in range(2)]
    expected = [str(r.id) for r in reversed(visible)]
    pages = [
        app_client.get("/reports", params={"page": p}, follow_redirects=False) for p in (1, 2, 3)
    ]
    assert pages[0].status_code == 200
    bodies = [p.json() for p in pages]
    assert [b["total"] for b in bodies] == [25, 25, 25]
    assert [len(b["items"]) for b in bodies] == [20, 5, 0]
    assert [b["page"] for b in bodies] == [1, 2, 3]
    assert all(b["page_size"] == 20 for b in bodies)
    ids = [r["id"] for b in bodies for r in b["items"]]
    assert ids == expected
    assert not set(ids) & {str(r.id) for r in failed}
    assert all(
        not {"report_md", "report_body_html", "report_html", "report_inputs"} & r.keys()
        for b in bodies
        for r in b["items"]
    )


def test_acceptance_02_display_states(app_client: TestClient, db_session: Session) -> None:
    mapping = {
        "success": "available",
        "skipped": "available",
        "needs_review": "under_review",
        "in_progress": "generating",
    }
    for status in mapping:
        row(db_session, status=status, node=status)
    body = app_client.get("/reports").json()
    assert {r["status"]: r["display_state"] for r in body["items"]} == mapping


def test_acceptance_03_sort_dates_and_validation(
    app_client: TestClient, db_session: Session
) -> None:
    reports = [row(db_session, day=date(2026, 10, d)) for d in (1, 2, 3)]
    body = app_client.get(
        "/reports", params={"sort": "asc", "date_from": "2026-10-01", "date_to": "2026-10-02"}
    ).json()
    assert [r["id"] for r in body["items"]] == [str(r.id) for r in reports[:2]]
    assert body["total"] == 2
    assert app_client.get("/reports", params={"date_from": "2026-10-03"}).json()["total"] == 1
    assert app_client.get("/reports", params={"date_to": "2026-10-01"}).json()["total"] == 1
    for params in (
        {"sort": "x"},
        {"page": 0},
        {"kind": "notice"},
        {"date_from": "2026-10-02", "date_to": "2026-10-01"},
        {"date_from": "bad-date"},
    ):
        assert app_client.get("/reports", params=params).status_code == 422


def test_acceptance_04_kinds(app_client: TestClient, db_session: Session) -> None:
    row(db_session)
    body = app_client.get("/reports").json()
    assert body["kinds"] == ["report"]
    assert [r["kind"] for r in body["items"]] == ["report"]


def test_acceptance_05_list_owner(app_client: TestClient, db_session: Session) -> None:
    mine = row(db_session)
    row(db_session, owner=uuid4())
    body = app_client.get("/reports").json()
    assert body["total"] == 1
    assert [r["id"] for r in body["items"]] == [str(mine.id)]


def test_acceptance_06_indistinguishable_not_found(
    app_client: TestClient, db_session: Session
) -> None:
    ids = [row(db_session, status=s, node=s).id for s in ("needs_review", "failed", "in_progress")]
    ids += [row(db_session, owner=uuid4()).id, uuid4()]
    responses = [app_client.get(f"/reports/{rid}") for rid in ids]
    assert [r.status_code for r in responses] == [404] * 5
    assert [r.json() for r in responses] == [{"detail": "Report not found"}] * 5


@pytest.mark.parametrize("status", ["success", "skipped"])
def test_acceptance_07_plain_fragment_and_alignment(
    app_client: TestClient, db_session: Session, status: str
) -> None:
    md = "# Briefing\n\n| Left | Right |\n|:--|--:|\n| A | B |\n"
    report = row(db_session, status=status, md=md)
    response = app_client.get(f"/reports/{report.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["report_md"] == md
    assert "report_body_html" in body
    from app.services.email_sender import render_report_body_html

    assert body["report_body_html"] == render_report_body_html(md)
    html = body["report_body_html"]
    assert '<th style="text-align:left">Left</th>' in html
    assert '<th style="text-align:right">Right</th>' in html
    assert '<td style="text-align:left">A</td>' in html
    assert '<td style="text-align:right">B</td>' in html
    soup = BeautifulSoup(html, "html.parser")
    assert all(
        tag.name in {"th", "td"} and tag["style"] in {"text-align:left", "text-align:right"}
        for tag in soup.select("[style]")
    )
    assert "<style" not in html and "unsubscribe" not in html
    assert "report_inputs" not in body and "report_html" not in body


@pytest.mark.parametrize(
    "md,expected",
    [
        ("<script>alert(1)</script>", "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>\n"),
        ("[x](javascript:alert(1))", "<p>[x](javascript:alert(1))</p>\n"),
    ],
)
def test_acceptance_08_xss_escaping(
    app_client: TestClient, db_session: Session, md: str, expected: str
) -> None:
    report = row(db_session, md=md)
    body = app_client.get(f"/reports/{report.id}").json()
    assert "report_body_html" in body
    assert body["report_body_html"] == expected
    assert 'href="javascript:' not in body["report_body_html"]


def test_detail_null_markdown_is_not_found(app_client: TestClient, db_session: Session) -> None:
    report = row(db_session, md=None)
    response = app_client.get(f"/reports/{report.id}")
    assert response.status_code == 404
    assert response.json() == {"detail": "Report not found"}


def test_acceptance_09_removed_routes_have_no_effect(
    app_client: TestClient, db_session: Session
) -> None:
    report = row(db_session)
    before = db_session.scalar(select(func.count()).select_from(ReportJob))
    with (
        patch("app.tasks.report_tasks.generate_report_job.delay") as enqueue,
        patch("app.routers.admin.regenerate_report") as regen,
        patch("app.routers.admin.send_report_email") as send,
        patch(
            "app.routers.reports.regenerate_report", create=True, return_value=report
        ) as old_regen,
        patch("app.routers.reports.send_report_email", create=True, return_value=True) as old_send,
    ):
        requests = [
            ("POST", "/reports/generate"),
            ("GET", f"/reports/jobs/{uuid4()}"),
            ("POST", f"/reports/{report.id}/regenerate?mode=render"),
            ("POST", f"/reports/{report.id}/regenerate?mode=analyze"),
            ("POST", f"/reports/{report.id}/send"),
        ]
        responses = [
            app_client.request(method, path, json={} if method == "POST" else None)
            for method, path in requests
        ]
        assert all(r.status_code in (404, 405) for r in responses)
        assert db_session.scalar(select(func.count()).select_from(ReportJob)) == before
        for mock in (enqueue, regen, send, old_regen, old_send):
            mock.assert_not_called()


def test_acceptance_10_ops_generate_and_enqueue_failure(
    app_client: TestClient, db_session: Session
) -> None:
    active = seed_user(db_session, uuid4())
    inactive = seed_user(db_session, uuid4())
    inactive.status = "suspended"
    db_session.flush()
    with (
        patch("app.tasks.report_tasks.generate_report_job.delay") as enqueue,
        patch(
            "app.routers.admin.generate_report",
            create=True,
            side_effect=AssertionError("synchronous generation is forbidden"),
        ),
    ):
        assert (
            app_client.post(
                f"/admin/users/{uuid4()}/reports/generate", headers=_headers()
            ).status_code
            == 404
        )
        assert (
            app_client.post(
                f"/admin/users/{inactive.id}/reports/generate", headers=_headers()
            ).status_code
            == 422
        )
        enqueue.assert_not_called()
        response = app_client.post(f"/admin/users/{active.id}/reports/generate", headers=_headers())
        assert response.status_code == 202
        job = db_session.get(ReportJob, UUID(response.json()["id"]))
        assert job is not None and job.user_id == active.id and job.status == "pending"
        enqueue.assert_called_once_with(
            str(job.id),
            report_type="incremental",
            report_date=None,
            base_currency=None,
            session_node="manual",
        )
    with patch(
        "app.tasks.report_tasks.generate_report_job.delay", side_effect=RuntimeError("broker down")
    ):
        response = app_client.post(f"/admin/users/{active.id}/reports/generate", headers=_headers())
        assert response.status_code == 503
    jobs = list(
        db_session.scalars(
            select(ReportJob).where(ReportJob.user_id == active.id, ReportJob.status == "failed")
        )
    )
    assert len(jobs) == 1
    assert jobs[0].error == "Failed to queue report job: RuntimeError: broker down"


def test_acceptance_11_ops_poll(app_client: TestClient, db_session: Session) -> None:
    report = row(db_session, owner=uuid4(), status="needs_review")
    job = ReportJob(user_id=report.user_id, status="success", report_id=report.id)
    db_session.add(job)
    db_session.flush()
    path = f"/admin/report-jobs/{job.id}"
    response = app_client.get(path, headers=_headers())
    assert response.status_code == 200
    assert response.json()["report_status"] == "needs_review"
    assert response.json()["report_id"] == str(report.id)
    assert app_client.get(f"/admin/report-jobs/{uuid4()}", headers=_headers()).status_code == 404
    assert app_client.get(path).status_code == 401
    assert app_client.get(path, headers=_headers("invalid")).status_code == 401


def test_acceptance_12_ops_send_branches(app_client: TestClient, db_session: Session) -> None:
    report = row(db_session)

    def path(owner: UUID = TEST_USER_ID, rid: UUID = report.id) -> str:
        return f"/admin/users/{owner}/reports/{rid}/send"

    with patch("app.routers.admin.send_report_email", return_value=True) as send:
        assert app_client.post(path(rid=uuid4()), headers=_headers()).status_code == 404
        assert app_client.post(path(owner=uuid4()), headers=_headers()).status_code == 404
        report.status = "skipped"
        db_session.flush()
        assert app_client.post(path(), headers=_headers()).status_code == 422
        report.status = "success"
        report.email_sent_at = datetime(2026, 10, 1, tzinfo=UTC)
        db_session.flush()
        response = app_client.post(path(), headers=_headers())
        assert response.status_code == 200 and response.json()["status"] == "already_sent"
        send.assert_not_called()
        report.email_sent_at = None
        db_session.flush()
        send.return_value = False
        assert app_client.post(path(), headers=_headers()).status_code == 502
        send.return_value = True
        response = app_client.post(path(), headers=_headers())
        assert response.status_code == 200 and response.json() == {
            "status": "sent",
            "email_sent_at": None,
        }
        assert send.call_count == 2
    assert app_client.post(path()).status_code == 401


def test_acceptance_13_no_synchronous_handler(app_client: TestClient, db_session: Session) -> None:
    import app.routers.admin as admin

    assert not hasattr(admin, "generate_report_for_user")
    user = seed_user(db_session, uuid4())
    with (
        patch("app.tasks.report_tasks.generate_report_job.delay"),
        patch("app.services.report_generator.generate_report") as generate,
    ):
        assert (
            app_client.post(
                f"/admin/users/{user.id}/reports/generate", headers=_headers()
            ).status_code
            == 202
        )
        generate.assert_not_called()
