"""POST /admin/users/by-email/report-currency (issue #350 item 1).

Ops sibling of PATCH /me/report-currency, grouped by URL shape (by-email)
alongside POST /admin/users/by-email/report-language — same lighter-weight
shape (ops token auth, no re-typed-email confirmation ceremony), validated
against VALID_CURRENCIES rather than a hand-kept Literal (see
app/routers/admin.py's UpdateReportCurrencyByEmailBody docstring for why).
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.user import User
from app.tests.test_admin_router import _headers
from app.tests.test_user_scope import _user

_UID = uuid.UUID("00000000-0000-0000-0000-0000000000d2")


def test_update_report_currency_by_email_requires_ops_token(app_client: TestClient) -> None:
    resp = app_client.post(
        "/admin/users/by-email/report-currency",
        params={"email": "seed-currency@example.com"},
        json={"report_currency": "USD"},
    )
    assert resp.status_code == 401


def test_update_report_currency_by_email_sets_and_reads_back(
    app_client: TestClient, db_session: Session
) -> None:
    db_session.add(_user(_UID, "seed-currency@example.com"))
    db_session.flush()

    resp = app_client.post(
        "/admin/users/by-email/report-currency",
        headers=_headers(),
        params={"email": "seed-currency@example.com"},
        json={"report_currency": "CNY"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "user_id": str(_UID),
        "email": "seed-currency@example.com",
        "report_currency": "CNY",
    }
    db_session.expire_all()
    row = db_session.get(User, _UID)
    assert row is not None
    assert row.base_currency == "CNY"


def test_update_report_currency_by_email_unknown_email_404(app_client: TestClient) -> None:
    resp = app_client.post(
        "/admin/users/by-email/report-currency",
        headers=_headers(),
        params={"email": "no-such-currency-user@example.com"},
        json={"report_currency": "USD"},
    )
    assert resp.status_code == 404


def test_update_report_currency_by_email_rejects_unknown_value(
    app_client: TestClient, db_session: Session
) -> None:
    db_session.add(_user(_UID, "seed-currency@example.com"))
    db_session.flush()

    resp = app_client.post(
        "/admin/users/by-email/report-currency",
        headers=_headers(),
        params={"email": "seed-currency@example.com"},
        json={"report_currency": "XXX"},
    )

    assert resp.status_code == 422
    db_session.expire_all()
    row = db_session.get(User, _UID)
    assert row is not None
    assert row.base_currency == "USD"
