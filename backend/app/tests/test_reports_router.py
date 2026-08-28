"""Integration tests for /reports endpoints (Ring 1-B design doc §5, B3).

Before B3, all four /reports/* endpoints (generate, regenerate, list, get)
resolved identity via a bare `get_current_user_id()` call —
`dependency_overrides` (the FastAPI test mechanism every other router in
this app relies on) silently doesn't intercept that, only `Depends(...)`.
These tests prove the fix (current_principal is wired as a real dependency
and app_client's override actually reaches these routes) and lock
cross-user isolation on every endpoint that scopes a lookup by user_id,
including the two write paths (regenerate, send) added in the PR #181
review round.
"""

from __future__ import annotations

import datetime as _dt
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import Principal, current_principal
from app.main import app
from app.models.report import Report
from app.models.user import User
from app.tests.conftest import TEST_USER_ID, seed_user


def _make_report(
    session: Session,
    *,
    user_id: uuid.UUID,
    report_date: _dt.date = _dt.date(2026, 6, 1),
    session_node: str = "manual",
) -> Report:
    # issue #129 B7: reports.user_id now FKs to users.id — every fixture
    # user_id here (TEST_USER_ID or an ad hoc uuid4() for a "someone else"
    # case) needs a real row. get-or-create since some tests build multiple
    # reports for the same user_id in one flush.
    if session.get(User, user_id) is None:
        seed_user(session, user_id)
    report = Report(
        user_id=user_id,
        report_date=report_date,
        report_type="incremental",
        session_node=session_node,
        status="success",
        report_md="# Report\n\nBody",
    )
    session.add(report)
    session.flush()
    session.refresh(report)
    return report


# ---------------------------------------------------------------------------
# POST /reports/generate — identity flows from Depends(current_principal)
# ---------------------------------------------------------------------------


def test_generate_report_uses_principal_user_id(
    app_client: TestClient, db_session: Session
) -> None:
    fake_report = _make_report(db_session, user_id=TEST_USER_ID)
    with patch("app.routers.reports.generate_report", return_value=fake_report) as mock_gen:
        resp = app_client.post("/reports/generate", json={"report_type": "incremental"})

    assert resp.status_code == 201
    assert mock_gen.call_args.kwargs["user_id"] == TEST_USER_ID


# ---------------------------------------------------------------------------
# POST /reports/{id}/regenerate — same
# ---------------------------------------------------------------------------


def test_regenerate_uses_principal_user_id(app_client: TestClient, db_session: Session) -> None:
    fake_report = _make_report(db_session, user_id=TEST_USER_ID)
    with patch("app.routers.reports.regenerate_report", return_value=fake_report) as mock_regen:
        resp = app_client.post(f"/reports/{fake_report.id}/regenerate")

    assert resp.status_code == 200
    assert mock_regen.call_args.kwargs["user_id"] == TEST_USER_ID


def test_regenerate_404_for_other_user(app_client: TestClient, db_session: Session) -> None:
    """PR #181 review: regenerate's ORM lookup is scoped by
    Report.user_id == user_id inside regenerate_report itself — exercise
    the real function (no mock) so a regression that dropped that filter
    would actually fail this test."""
    other = _make_report(db_session, user_id=uuid.uuid4())
    db_session.commit()

    resp = app_client.post(f"/reports/{other.id}/regenerate")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /reports/{id}/send — cross-user isolation
# ---------------------------------------------------------------------------


def test_send_404_for_other_user(app_client: TestClient, db_session: Session) -> None:
    """Same shape as regenerate: send_report's own query is scoped by
    Report.user_id == user_id — real function, no mock."""
    other = _make_report(db_session, user_id=uuid.uuid4())
    db_session.commit()

    resp = app_client.post(f"/reports/{other.id}/send")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /reports/ and GET /reports/{id} — cross-user isolation
# ---------------------------------------------------------------------------


def test_list_reports_scoped_to_principal(app_client: TestClient, db_session: Session) -> None:
    mine = _make_report(db_session, user_id=TEST_USER_ID)
    other = _make_report(db_session, user_id=uuid.uuid4())
    db_session.commit()

    resp = app_client.get("/reports/")

    assert resp.status_code == 200
    ids = {row["id"] for row in resp.json()}
    assert str(mine.id) in ids
    assert str(other.id) not in ids


def test_get_report_404_for_other_user(app_client: TestClient, db_session: Session) -> None:
    other = _make_report(db_session, user_id=uuid.uuid4())
    db_session.commit()

    resp = app_client.get(f"/reports/{other.id}")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# dependency_overrides actually intercepts current_principal (the B3 fix
# itself — pre-B3, overriding get_current_user_id had no effect on these
# four call sites since they called it directly, not via Depends).
# ---------------------------------------------------------------------------


def test_dependency_overrides_intercepts_current_principal(db_session: Session) -> None:
    other_user = uuid.uuid4()
    theirs = _make_report(db_session, user_id=other_user)
    db_session.commit()

    def _override_session() -> object:
        yield db_session

    def _override_principal() -> Principal:
        return Principal(user_id=other_user)

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[current_principal] = _override_principal
    try:
        client = TestClient(app)
        resp = client.get("/reports/")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    ids = {row["id"] for row in resp.json()}
    assert str(theirs.id) in ids


def test_dependency_overrides_intercepts_current_principal_on_a_write_path(
    db_session: Session,
) -> None:
    """PR #181 review: the intercept test above only exercised the list
    (read) route — extend it to regenerate (a write path) so this
    property can't quietly rot into "list-only"."""
    other_user = uuid.uuid4()
    theirs = _make_report(db_session, user_id=other_user)
    db_session.commit()

    def _override_session() -> object:
        yield db_session

    def _override_principal() -> Principal:
        return Principal(user_id=other_user)

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[current_principal] = _override_principal
    try:
        client = TestClient(app)
        with patch("app.routers.reports.regenerate_report", return_value=theirs) as mock_regen:
            resp = client.post(f"/reports/{theirs.id}/regenerate")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert mock_regen.call_args.kwargs["user_id"] == other_user
