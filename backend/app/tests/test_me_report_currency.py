"""PATCH /me/report-currency (issue #350 item 1).

Self-service sibling of PATCH /me/report-language — same discipline, but
validated against VALID_CURRENCIES (15 members, app/schemas/holdings.py)
rather than a hand-kept 2-value Literal, since that source of truth
already exists and is used elsewhere for this exact field (see
UpdateReportCurrencyBody's docstring in app/routers/me.py for why this is
a plain `str` + field_validator, not a Literal).
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import current_principal
from app.main import app
from app.models.user import User
from app.tests.conftest import TEST_USER_ID
from app.tests.test_me_router import _seed_user


def test_update_report_currency_without_token_is_401(db_session: Session) -> None:
    def _override_session() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides.pop(current_principal, None)
    try:
        client = TestClient(app)
        resp = client.patch("/me/report-currency", json={"report_currency": "USD"})
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_update_report_currency_writes_and_is_reflected_in_get_me(
    app_client: TestClient, db_session: Session
) -> None:
    _seed_user(db_session, base_currency="USD")

    resp = app_client.patch("/me/report-currency", json={"report_currency": "CNY"})

    assert resp.status_code == 200
    assert resp.json()["report_currency"] == "CNY"
    db_session.expire_all()
    row = db_session.get(User, TEST_USER_ID)
    assert row is not None
    assert row.base_currency == "CNY"

    assert app_client.get("/me").json()["report_currency"] == "CNY"


def test_update_report_currency_rejects_unknown_value(
    app_client: TestClient, db_session: Session
) -> None:
    _seed_user(db_session, base_currency="USD")

    resp = app_client.patch("/me/report-currency", json={"report_currency": "XXX"})

    assert resp.status_code == 422
    db_session.expire_all()
    row = db_session.get(User, TEST_USER_ID)
    assert row is not None
    assert row.base_currency == "USD"
