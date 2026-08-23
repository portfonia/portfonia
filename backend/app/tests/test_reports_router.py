"""Integration tests for /reports endpoints (Ring 1-B design doc §5, B3).

Before B3, all four endpoints resolved identity via a bare
`get_current_user_id()` call — `dependency_overrides` (the FastAPI test
mechanism every other router in this app relies on) silently doesn't
intercept that, only `Depends(...)`. These tests exist specifically to
prove the fix: that `current_principal` is wired as a real dependency and
that `app_client`'s override actually reaches these four routes.
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
from app.tests.conftest import TEST_USER_ID


def _make_report(
    session: Session,
    *,
    user_id: uuid.UUID,
    report_date: _dt.date = _dt.date(2026, 6, 1),
    session_node: str = "manual",
) -> Report:
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
