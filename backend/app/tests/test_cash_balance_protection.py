"""Issue #599 cash adjustment and purge contracts against real Postgres."""

from __future__ import annotations

import csv
import io
import uuid
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.credit_ledger import CreditLedgerEntry
from app.models.holding import Holding
from app.models.user import User
from app.services.credit_ledger import adjust_by_admin, record_purchase
from app.tests.conftest import seed_user
from app.tests.test_admin_router import _headers
from app.tests.test_credit_ledger import assert_balanced
from app.tests.test_user_scope import _h

URL = "/admin/users/by-email/credit-adjustments"
NOTE = "waived by user 2026-10-01, past refund window"


@pytest.fixture
def cash_user(db_session: Session) -> User:
    user = seed_user(db_session, uuid.uuid4(), "a@x.com")
    adjust_by_admin(
        db_session, user_id=user.id, amount=Decimal("3.00"), note="gift", idempotency_key="gift"
    )
    record_purchase(
        db_session,
        user_id=user.id,
        credits=Decimal("10.00"),
        transaction_id="cash",
        note="purchase",
    )
    db_session.add(_h(user_id=user.id, name="NVIDIA", ticker="NVDA"))
    db_session.commit()
    return user


@pytest.fixture
def auth_delete(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock(return_value=True)
    monkeypatch.setattr("app.routers.admin.delete_auth_user", mock)
    return mock


def _body(**changes: str) -> dict[str, str]:
    return {
        "email": "a@x.com",
        "bucket": "cash",
        "amount": "-10.00",
        "note": NOTE,
        "idempotency_key": "waive-1",
        **changes,
    }


def _purge(client: TestClient, user: User, route: str, confirm: str = "a@x.com") -> Response:
    return client.delete(
        "/admin/users/by-email" if route == "email" else f"/admin/users/{user.id}",
        params={"email": user.email, "confirm": confirm},
        headers=_headers(),
    )


def _snapshot(session: Session) -> list[object]:
    return [
        list(session.execute(select(model.__table__)).all())
        for model in (User, Holding, CreditLedgerEntry)
    ]


@pytest.mark.parametrize("route", ["email", "id"])
def test_purge_refuses_cash_without_touching_rows(
    app_client: TestClient, db_session: Session, cash_user: User, auth_delete: MagicMock, route: str
) -> None:
    before = _snapshot(db_session)
    response = _purge(app_client, cash_user, route)
    assert response.status_code == 409
    assert response.json()["detail"] == "user has a cash balance; refund or adjust it to zero first"
    auth_delete.assert_not_called()
    assert _snapshot(db_session) == before


@pytest.mark.parametrize(
    "route,status,detail",
    [
        ("id", 409, "confirm does not match user email"),
        ("email", 422, "email and confirm must match"),
    ],
)
def test_wrong_confirm_precedes_cash_guard(
    app_client: TestClient,
    db_session: Session,
    cash_user: User,
    auth_delete: MagicMock,
    route: str,
    status: int,
    detail: str,
) -> None:
    before = _snapshot(db_session)
    response = _purge(app_client, cash_user, route, "wrong@x.com")
    assert response.status_code == status
    assert response.json()["detail"] == detail
    auth_delete.assert_not_called()
    assert _snapshot(db_session) == before


@pytest.mark.parametrize("route", ["email", "id"])
def test_cash_waiver_then_purge_flags_ledger(
    app_client: TestClient, db_session: Session, cash_user: User, auth_delete: MagicMock, route: str
) -> None:
    response = app_client.post(URL, json=_body(), headers=_headers())
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["cash_balance"] == "0.00"
    assert data["gift_balance"] == "3.00"
    assert data["replayed"] is False
    entry = db_session.get(CreditLedgerEntry, data["entry"]["id"])
    assert entry is not None
    assert (
        entry.user_id,
        entry.bucket,
        entry.amount,
        entry.balance_after,
        entry.reason,
        entry.actor_type,
        entry.actor_id,
        entry.idempotency_key,
        entry.note,
        entry.reference,
    ) == (
        cash_user.id,
        "cash",
        Decimal("-10.00"),
        Decimal("0.00"),
        "admin_adjustment",
        "admin",
        None,
        "admin_adjustment:waive-1",
        NOTE,
        None,
    )
    assert_balanced(db_session, cash_user)
    user_id = cash_user.id
    rows = list(
        db_session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.user_id == user_id))
    )
    assert len(rows) == 3
    response = _purge(app_client, cash_user, route)
    assert response.status_code == 200, response.text
    assert response.json()["deleted"]["credit_ledger_flagged"] == 3
    auth_delete.assert_called_once_with(cash_user.auth_subject)
    db_session.expire_all()
    assert db_session.get(User, user_id) is None
    assert list(db_session.scalars(select(Holding).where(Holding.user_id == user_id))) == []
    for row in rows:
        assert row.user_deleted_at is not None


def test_cash_adjustment_replay_and_bucket_conflict(
    app_client: TestClient, db_session: Session, cash_user: User
) -> None:
    first = app_client.post(URL, json=_body(), headers=_headers())
    assert first.status_code == 200
    before = _snapshot(db_session)
    replay = app_client.post(URL, json=_body(), headers=_headers())
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["entry"] == first.json()["entry"]
    conflict = app_client.post(URL, json=_body(bucket="gift"), headers=_headers())
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "idempotency_key already used for a different adjustment"
    assert _snapshot(db_session) == before
    assert_balanced(db_session, cash_user)
    assert cash_user.credit_cash_balance == Decimal("0.00")
    assert cash_user.credit_gift_balance == Decimal("3.00")


def test_cash_adjustment_overdraft_leaves_balances_and_rows(
    app_client: TestClient, db_session: Session, cash_user: User
) -> None:
    before = _snapshot(db_session)
    response = app_client.post(URL, json=_body(amount="-10.01"), headers=_headers())
    assert response.status_code == 409
    assert response.json()["detail"] == "insufficient balance"
    assert _snapshot(db_session) == before
    assert_balanced(db_session, cash_user)
    assert cash_user.credit_cash_balance == Decimal("10.00")
    assert cash_user.credit_gift_balance == Decimal("3.00")


def test_cash_credit_and_balances_csv_consistent(
    app_client: TestClient, db_session: Session, cash_user: User
) -> None:
    response = app_client.post(
        URL, json=_body(amount="2.00", note="manual credit"), headers=_headers()
    )
    assert response.status_code == 200
    assert response.json()["cash_balance"] == "12.00"
    assert response.json()["gift_balance"] == "3.00"
    entry = response.json()["entry"]
    assert (
        entry["bucket"],
        entry["amount"],
        entry["balance_after"],
        entry["reason"],
        entry["actor_type"],
    ) == (
        "cash",
        "2.00",
        "12.00",
        "admin_adjustment",
        "admin",
    )
    assert_balanced(db_session, cash_user)
    export = app_client.get("/admin/credits/balances.csv", headers=_headers())
    assert export.status_code == 200
    row = next(
        row
        for row in csv.DictReader(io.StringIO(export.text))
        if row["user_id"] == str(cash_user.id)
    )
    assert (
        row["cash_balance"],
        row["gift_balance"],
        row["total_balance"],
        row["ledger_cash_sum"],
        row["ledger_gift_sum"],
        row["consistent"],
    ) == (
        "12.00",
        "3.00",
        "15.00",
        "12.00",
        "3.00",
        "true",
    )


@pytest.mark.parametrize("note", [None, "", "   "])
def test_cash_adjustment_requires_nonblank_note(
    app_client: TestClient, db_session: Session, cash_user: User, note: str | None
) -> None:
    body = _body()
    if note is None:
        del body["note"]
    else:
        body["note"] = note
    before = _snapshot(db_session)
    response = app_client.post(URL, json=body, headers=_headers())
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "note"]
    assert _snapshot(db_session) == before


@pytest.mark.parametrize("route", ["email", "id"])
def test_zero_cash_with_gift_purge_flags_ledger(
    app_client: TestClient, db_session: Session, auth_delete: MagicMock, route: str
) -> None:
    user = seed_user(db_session, uuid.uuid4(), "a@x.com")
    result = adjust_by_admin(
        db_session, user_id=user.id, amount=Decimal("3.00"), note="gift", idempotency_key="gift"
    )
    assert_balanced(db_session, user)
    assert user.credit_cash_balance == Decimal("0.00")
    assert user.credit_gift_balance == Decimal("3.00")
    user_id, subject = user.id, user.auth_subject
    response = _purge(app_client, user, route)
    assert response.status_code == 200
    assert response.json()["deleted"]["credit_ledger_flagged"] == 1
    auth_delete.assert_called_once_with(subject)
    db_session.expire_all()
    assert db_session.get(User, user_id) is None
    assert result.entries[0].user_deleted_at is not None
    assert result.entries[0].amount == Decimal("3.00")
    assert result.entries[0].bucket == "gift"
