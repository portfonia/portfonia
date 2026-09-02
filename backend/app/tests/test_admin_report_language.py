"""POST /admin/users/by-email/report-language (issue #308).

Ops sibling of PATCH /me/report-language, grouped by URL shape (by-email)
alongside DELETE /admin/users/by-email (issue #274/PR #275) rather than by
"both are user-setting mutations" next to the by-id
POST /admin/users/{user_id}/cadence. No re-typed-email confirmation
ceremony (that pattern is reserved for the irreversible purge route) —
same lighter-weight shape as the by-id cadence endpoint: ops token auth,
404 for an unknown email, validated against the same VALID_REPORT_LANGUAGES
whitelist and Pydantic Literal as the self-service endpoint.
"""

from __future__ import annotations

import uuid
from typing import get_args

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.user import VALID_REPORT_LANGUAGES, User
from app.routers.admin import UpdateReportLanguageByEmailBody
from app.tests.test_admin_router import _headers
from app.tests.test_user_scope import _user

_UID = uuid.UUID("00000000-0000-0000-0000-0000000000d1")


def test_update_report_language_by_email_literal_matches_valid_report_languages() -> None:
    """Same hand-kept-sync discipline as UpdateReportLanguageBody's Literal
    (backend/app/routers/me.py) — reuses the same whitelist and Literal
    values per the engineering contract ("do not define a second, separately
    drifting whitelist for the ops path"), verified here independently so a
    drift in this copy fails a test too."""
    literal_values = get_args(
        UpdateReportLanguageByEmailBody.model_fields["report_language"].annotation
    )
    assert set(literal_values) == set(VALID_REPORT_LANGUAGES)


def test_update_report_language_by_email_requires_ops_token(app_client: TestClient) -> None:
    resp = app_client.post(
        "/admin/users/by-email/report-language",
        params={"email": "seed@example.com"},
        json={"report_language": "en"},
    )
    assert resp.status_code == 401


def test_update_report_language_by_email_sets_and_reads_back(
    app_client: TestClient, db_session: Session
) -> None:
    db_session.add(_user(_UID, "seed@example.com"))
    db_session.flush()

    resp = app_client.post(
        "/admin/users/by-email/report-language",
        headers=_headers(),
        params={"email": "seed@example.com"},
        json={"report_language": "en"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"user_id": str(_UID), "email": "seed@example.com", "report_language": "en"}
    db_session.expire_all()
    row = db_session.get(User, _UID)
    assert row is not None
    assert row.locale == "en"

    # Reflected in a subsequent read (mirrors GET /me's own field, via the
    # read-only ops user directory rather than the user's own session).
    listed = app_client.get(
        "/admin/users", headers=_headers(), params={"email": "seed@example.com"}
    ).json()
    assert len(listed) == 1


def test_update_report_language_by_email_unknown_email_404(app_client: TestClient) -> None:
    resp = app_client.post(
        "/admin/users/by-email/report-language",
        headers=_headers(),
        params={"email": "no-such-user@example.com"},
        json={"report_language": "en"},
    )
    assert resp.status_code == 404


def test_update_report_language_by_email_rejects_unknown_value(
    app_client: TestClient, db_session: Session
) -> None:
    db_session.add(_user(_UID, "seed@example.com"))
    db_session.flush()

    resp = app_client.post(
        "/admin/users/by-email/report-language",
        headers=_headers(),
        params={"email": "seed@example.com"},
        json={"report_language": "fr"},
    )

    assert resp.status_code == 422
    db_session.expire_all()
    row = db_session.get(User, _UID)
    assert row is not None
    assert row.locale == "zh"
