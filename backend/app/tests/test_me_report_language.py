"""PATCH /me/report-language (issue #308).

Self-service sibling of POST /admin/users/{user_id}/cadence, but for the
caller's own row via current_principal instead of an ops-token-authed
user_id — same Literal-vs-DB-CheckConstraint validation discipline as
UpdateCadenceBody (test_admin_cadence.py), no rate limiting (a plain
authenticated write with no external side effect or abuse surface, per
the engineering contract).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import get_args

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import current_principal
from app.main import app
from app.models.user import VALID_REPORT_LANGUAGES, User
from app.routers.me import UpdateReportLanguageBody
from app.tests.conftest import TEST_USER_ID
from app.tests.test_me_router import _seed_user


def test_update_report_language_literal_matches_valid_report_languages() -> None:
    """UpdateReportLanguageBody.report_language's Literal is a hand-kept
    second copy of VALID_REPORT_LANGUAGES (a Pydantic Literal can't be
    derived from a tuple at type-check time) — same discipline as
    UpdateCadenceBody / test_update_cadence_literal_matches_valid_report_
    cadences. This doesn't remove the duplication, it makes drift between
    the two fail a test instead of silently accepting/rejecting the wrong
    values at runtime."""
    literal_values = get_args(UpdateReportLanguageBody.model_fields["report_language"].annotation)
    assert set(literal_values) == set(VALID_REPORT_LANGUAGES)


def test_update_report_language_without_token_is_401(db_session: Session) -> None:
    """No current_principal override — a real unauthenticated request must
    401 (same pattern as test_email_verification_resend.py's
    test_resend_requires_auth)."""

    def _override_session() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides.pop(current_principal, None)
    try:
        client = TestClient(app)
        resp = client.patch("/me/report-language", json={"report_language": "en"})
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_update_report_language_writes_and_is_reflected_in_get_me(
    app_client: TestClient, db_session: Session
) -> None:
    _seed_user(db_session, locale="zh")

    resp = app_client.patch("/me/report-language", json={"report_language": "en"})

    assert resp.status_code == 200
    assert resp.json()["report_language"] == "en"
    db_session.expire_all()
    row = db_session.get(User, TEST_USER_ID)
    assert row is not None
    assert row.locale == "en"

    assert app_client.get("/me").json()["report_language"] == "en"


def test_update_report_language_rejects_unknown_value(
    app_client: TestClient, db_session: Session
) -> None:
    _seed_user(db_session, locale="zh")

    resp = app_client.patch("/me/report-language", json={"report_language": "fr"})

    assert resp.status_code == 422
    db_session.expire_all()
    row = db_session.get(User, TEST_USER_ID)
    assert row is not None
    assert row.locale == "zh"
