"""Report-currency change audit (issue #372 slice A).

Append-only log of users.base_currency preference changes. Does not rewrite
portfolio_value_snapshots.base_currency (capture-time stamp from #367).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.portfolio_value_snapshot import PortfolioValueSnapshot
from app.models.report_currency_change import ReportCurrencyChange
from app.models.user import User
from app.tests.conftest import TEST_USER_ID
from app.tests.test_admin_router import _headers
from app.tests.test_me_router import _seed_user
from app.tests.test_user_scope import _user

_ADMIN_UID = uuid.UUID("00000000-0000-0000-0000-0000000000d3")


def _audit_rows(session: Session, user_id: object) -> list[ReportCurrencyChange]:
    return list(
        session.scalars(
            select(ReportCurrencyChange)
            .where(ReportCurrencyChange.user_id == user_id)
            .order_by(ReportCurrencyChange.changed_at.asc(), ReportCurrencyChange.id.asc())
        ).all()
    )


def test_self_service_change_appends_audit_row(app_client: TestClient, db_session: Session) -> None:
    _seed_user(db_session, base_currency="USD")

    resp = app_client.patch("/me/report-currency", json={"report_currency": "CNY"})

    assert resp.status_code == 200
    db_session.expire_all()
    rows = _audit_rows(db_session, TEST_USER_ID)
    assert len(rows) == 1
    row = rows[0]
    assert row.old_currency == "USD"
    assert row.new_currency == "CNY"
    assert row.source == "self"
    assert row.actor_user_id == TEST_USER_ID
    assert row.changed_at is not None


def test_self_service_same_currency_does_not_append(
    app_client: TestClient, db_session: Session
) -> None:
    _seed_user(db_session, base_currency="USD")

    resp = app_client.patch("/me/report-currency", json={"report_currency": "USD"})

    assert resp.status_code == 200
    db_session.expire_all()
    assert _audit_rows(db_session, TEST_USER_ID) == []


def test_self_service_rejects_unknown_value_without_audit(
    app_client: TestClient, db_session: Session
) -> None:
    _seed_user(db_session, base_currency="USD")

    resp = app_client.patch("/me/report-currency", json={"report_currency": "XXX"})

    assert resp.status_code == 422
    db_session.expire_all()
    assert _audit_rows(db_session, TEST_USER_ID) == []
    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    assert user.base_currency == "USD"


def test_two_sequential_changes_append_two_rows(
    app_client: TestClient, db_session: Session
) -> None:
    _seed_user(db_session, base_currency="USD")

    assert (
        app_client.patch("/me/report-currency", json={"report_currency": "CNY"}).status_code == 200
    )
    assert (
        app_client.patch("/me/report-currency", json={"report_currency": "HKD"}).status_code == 200
    )

    db_session.expire_all()
    rows = _audit_rows(db_session, TEST_USER_ID)
    assert [(r.old_currency, r.new_currency) for r in rows] == [("USD", "CNY"), ("CNY", "HKD")]


def test_change_does_not_rewrite_snapshot_base_currency(
    app_client: TestClient, db_session: Session
) -> None:
    _seed_user(db_session, base_currency="USD")
    snap = PortfolioValueSnapshot(
        user_id=TEST_USER_ID,
        snapshot_date=date(2026, 9, 1),
        currency="USD",
        base_currency="USD",
        market_value_base=Decimal("100"),
        data_quality="ok",
    )
    db_session.add(snap)
    db_session.flush()
    snap_id = snap.id

    resp = app_client.patch("/me/report-currency", json={"report_currency": "CNY"})
    assert resp.status_code == 200

    db_session.expire_all()
    stored = db_session.get(PortfolioValueSnapshot, snap_id)
    assert stored is not None
    assert stored.base_currency == "USD"
    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    assert user.base_currency == "CNY"


def test_admin_change_appends_audit_with_admin_source(
    app_client: TestClient, db_session: Session
) -> None:
    db_session.add(_user(_ADMIN_UID, "audit-currency@example.com"))
    db_session.flush()

    resp = app_client.post(
        "/admin/users/by-email/report-currency",
        headers=_headers(),
        params={"email": "audit-currency@example.com"},
        json={"report_currency": "EUR"},
    )

    assert resp.status_code == 200
    db_session.expire_all()
    rows = _audit_rows(db_session, _ADMIN_UID)
    assert len(rows) == 1
    assert rows[0].old_currency == "USD"
    assert rows[0].new_currency == "EUR"
    assert rows[0].source == "admin"
    assert rows[0].actor_user_id is None


def test_admin_same_currency_does_not_append(app_client: TestClient, db_session: Session) -> None:
    db_session.add(_user(_ADMIN_UID, "audit-currency@example.com"))
    db_session.flush()

    resp = app_client.post(
        "/admin/users/by-email/report-currency",
        headers=_headers(),
        params={"email": "audit-currency@example.com"},
        json={"report_currency": "USD"},
    )

    assert resp.status_code == 200
    db_session.expire_all()
    assert _audit_rows(db_session, _ADMIN_UID) == []


def test_admin_audit_read_requires_ops_token(app_client: TestClient) -> None:
    resp = app_client.get(f"/admin/users/{TEST_USER_ID}/report-currency-audit")
    assert resp.status_code == 401


def test_admin_audit_read_unknown_user_404(app_client: TestClient) -> None:
    resp = app_client.get(
        f"/admin/users/{TEST_USER_ID}/report-currency-audit",
        headers=_headers(),
    )
    assert resp.status_code == 404


def test_admin_audit_read_returns_newest_first(app_client: TestClient, db_session: Session) -> None:
    _seed_user(db_session, base_currency="USD")
    assert (
        app_client.patch("/me/report-currency", json={"report_currency": "CNY"}).status_code == 200
    )
    assert (
        app_client.patch("/me/report-currency", json={"report_currency": "JPY"}).status_code == 200
    )

    resp = app_client.get(
        f"/admin/users/{TEST_USER_ID}/report-currency-audit",
        headers=_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [item["old_currency"] + "->" + item["new_currency"] for item in body] == [
        "CNY->JPY",
        "USD->CNY",
    ]
    assert body[0]["source"] == "self"
    assert body[0]["user_id"] == str(TEST_USER_ID)
    assert body[0]["actor_user_id"] == str(TEST_USER_ID)
    assert "changed_at" in body[0]


def test_user_purge_cascades_audit_rows(db_session: Session) -> None:
    """Preference audit must not block hard-purge (ON DELETE CASCADE)."""
    from app.services.user_purge import purge_user

    db_session.add(_user(_ADMIN_UID, "purge-currency@example.com"))
    db_session.flush()
    db_session.add(
        ReportCurrencyChange(
            user_id=_ADMIN_UID,
            old_currency="USD",
            new_currency="CNY",
            source="self",
            actor_user_id=_ADMIN_UID,
        )
    )
    db_session.flush()

    result = purge_user(db_session, _ADMIN_UID)
    db_session.commit()
    assert result.users == 1
    db_session.expire_all()
    assert db_session.get(User, _ADMIN_UID) is None
    assert _audit_rows(db_session, _ADMIN_UID) == []
