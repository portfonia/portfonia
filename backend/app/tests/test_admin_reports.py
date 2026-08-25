"""POST /admin/users/{user_id}/reports/generate (issue #201).

Ops-token path that fires generate_report for a specific user, bypassing
the self-service POST /reports/generate (and the Next.js proxy timeout
documented in issue #193). Preconditions mirror active_user_ids(): the
target must exist, be status=active, and have at least one holding.
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.holding import Holding
from app.models.report import Report
from app.models.user import User
from app.tests.test_admin_router import _headers

_UID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")


def _user(user_id: uuid.UUID, email: str, *, status: str = "active") -> User:
    return User(
        id=user_id,
        auth_provider="supabase",
        auth_subject=f"sub-{user_id}",
        email=email,
        status=status,
        locale="zh",
        base_currency="USD",
        report_cadence="mwf",
    )


def _holding(user_id: uuid.UUID) -> Holding:
    return Holding(
        user_id=user_id,
        name="NVIDIA",
        ticker="NVDA",
        pricing_mode="auto",
        currency="USD",
    )


def _path(user_id: uuid.UUID) -> str:
    return f"/admin/users/{user_id}/reports/generate"


def _seed_reportable(session: Session, user_id: uuid.UUID = _UID) -> None:
    session.add_all([_user(user_id, "ops-target@example.com"), _holding(user_id)])
    session.flush()


def _fake_report(session: Session, user_id: uuid.UUID) -> Report:
    report = Report(
        user_id=user_id,
        report_date=date(2026, 8, 25),
        report_type="incremental",
        session_node="manual",
        status="success",
        report_md="# Report\n\nBody",
    )
    session.add(report)
    session.flush()
    session.refresh(report)
    return report


def test_admin_generate_requires_ops_token(app_client: TestClient) -> None:
    resp = app_client.post(_path(_UID))
    assert resp.status_code == 401


def test_admin_generate_404_unknown_user(app_client: TestClient, db_session: Session) -> None:
    with patch("app.routers.admin.generate_report") as mock_gen:
        resp = app_client.post(_path(_UID), headers=_headers())

    assert resp.status_code == 404
    assert resp.json()["detail"] == "user not found"
    mock_gen.assert_not_called()


def test_admin_generate_422_no_holdings(app_client: TestClient, db_session: Session) -> None:
    db_session.add(_user(_UID, "empty@example.com"))
    db_session.flush()

    with patch("app.routers.admin.generate_report") as mock_gen:
        resp = app_client.post(_path(_UID), headers=_headers())

    assert resp.status_code == 422
    assert resp.json()["detail"] == "user has no holdings"
    mock_gen.assert_not_called()


def test_admin_generate_422_inactive_user(app_client: TestClient, db_session: Session) -> None:
    """active_user_ids() requires status=active; a suspended book must not
    generate even if holdings exist."""
    db_session.add_all([_user(_UID, "suspended@example.com", status="suspended"), _holding(_UID)])
    db_session.flush()

    with patch("app.routers.admin.generate_report") as mock_gen:
        resp = app_client.post(_path(_UID), headers=_headers())

    assert resp.status_code == 422
    assert resp.json()["detail"] == "user is not active"
    mock_gen.assert_not_called()


def test_admin_generate_calls_generate_report_for_path_user(
    app_client: TestClient, db_session: Session
) -> None:
    """Admin targets the path param, never the request-scoped principal
    (app_client overrides current_principal to TEST_USER_ID)."""
    _seed_reportable(db_session)
    fake = _fake_report(db_session, _UID)

    with patch("app.routers.admin.generate_report", return_value=fake) as mock_gen:
        resp = app_client.post(_path(_UID), headers=_headers())

    assert resp.status_code == 201
    mock_gen.assert_called_once()
    kwargs = mock_gen.call_args.kwargs
    assert kwargs["user_id"] == _UID
    assert kwargs["session_node"] == "manual"
    assert kwargs["output_lang"] == get_settings().OUTPUT_LANG
    body = resp.json()
    assert body["id"] == str(fake.id)
    assert body["session_node"] == "manual"
    assert body["status"] == "success"


def test_admin_generate_translates_llm_empty_to_502(
    app_client: TestClient, db_session: Session
) -> None:
    from app.services.llm_errors import LLMEmptyResponseError

    _seed_reportable(db_session)
    with patch(
        "app.routers.admin.generate_report",
        side_effect=LLMEmptyResponseError("empty choices"),
    ):
        resp = app_client.post(_path(_UID), headers=_headers())

    assert resp.status_code == 502
    assert "empty response" in resp.json()["detail"]


def test_admin_generate_translates_runtime_error_to_502(
    app_client: TestClient, db_session: Session
) -> None:
    _seed_reportable(db_session)
    with patch(
        "app.routers.admin.generate_report",
        side_effect=RuntimeError("truncated body"),
    ):
        resp = app_client.post(_path(_UID), headers=_headers())

    assert resp.status_code == 502
    assert "truncated body" in resp.json()["detail"]


def test_admin_generate_translates_openai_api_error_to_502(
    app_client: TestClient, db_session: Session
) -> None:
    """Provider/auth faults from _call_llm are openai.APIError, not RuntimeError
    — without this mapping they surface as a bare FastAPI 500 (PR #203 review)."""
    import httpx
    import openai

    _seed_reportable(db_session)
    err = openai.APIError(
        "invalid api key",
        httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        body=None,
    )
    with patch("app.routers.admin.generate_report", side_effect=err):
        resp = app_client.post(_path(_UID), headers=_headers())

    assert resp.status_code == 502
    assert "APIError" in resp.json()["detail"]
    assert "invalid api key" in resp.json()["detail"]


def test_admin_generate_integrity_error_is_409(app_client: TestClient, db_session: Session) -> None:
    """Concurrent same-key inserts race generate_report's idempotency SELECT
    and hit uq_reports_user_date_type_session (PR #203 review)."""
    from sqlalchemy.exc import IntegrityError

    _seed_reportable(db_session)
    with patch(
        "app.routers.admin.generate_report",
        side_effect=IntegrityError("INSERT", {}, Exception("uq_reports")),
    ):
        resp = app_client.post(_path(_UID), headers=_headers())

    assert resp.status_code == 409
    assert "already in progress" in resp.json()["detail"]
