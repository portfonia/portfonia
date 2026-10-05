"""Issue #644 acceptance against real Postgres; no real Auth calls."""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.deps import current_principal
from app.core.timezones import ET
from app.main import app
from app.models.credit_ledger import CreditLedgerEntry
from app.models.invite import Invite
from app.models.user import User
from app.models.waitlist_entry import WaitlistEntry
from app.services import credit_ledger as ledger
from app.services.altcha_challenge import create_change_password_challenge
from app.services.auth_provider import AuthProviderError
from app.services.invites import create_invite
from app.tests.conftest import TEST_USER_ID, seed_user
from app.tests.test_admin_router import _headers
from app.tests.test_admin_user_purge import _context, _report
from app.tests.test_me_change_password_altcha import _solve
from app.tests.test_user_scope import _h

PATH = "/me/account-deletion"
NOW = datetime(2026, 10, 5, 12, tzinfo=ET)


@pytest.fixture(autouse=True)
def auth(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock(return_value=True)
    monkeypatch.setattr("app.services.auth_provider.delete_auth_user", mock)
    monkeypatch.setattr("app.routers.admin.delete_auth_user", mock)
    monkeypatch.setattr("app.routers.me.delete_auth_user", mock, raising=False)
    monkeypatch.setattr("app.routers.me.send_ops_alert", MagicMock(), raising=False)
    return mock


@pytest.fixture
def user(db_session: Session) -> User:
    return seed_user(db_session, TEST_USER_ID, "user@example.com")


def purchase(session: Session, user: User, amount: str, age: int, txn: str) -> None:
    result = ledger.record_purchase(
        session, user_id=user.id, credits=Decimal(amount), transaction_id=txn, note="fixture"
    )
    result.entries[0].created_at = NOW - timedelta(days=age)
    session.flush()


def body(amount: str = "0.00", altcha: str | None = None) -> dict[str, str | None]:
    return {"confirm_email": " User@Example.com ", "relinquish_cash": amount, "altcha": altcha}


def rows(session: Session) -> list[CreditLedgerEntry]:
    return list(
        session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.user_id == TEST_USER_ID))
    )


def proof(client: TestClient) -> str:
    response = client.get(PATH + "/altcha-challenge")
    assert response.status_code == 200
    return _solve(response.json())


@pytest.mark.parametrize(
    "kind,cash,refundable,gift",
    [
        (1, "0.00", "0.00", "3.01"),
        (2, "12.00", "0.00", "0.00"),
        (3, "12.00", "10.00", "0.00"),
        (4, "6.00", "6.00", "0.00"),
        (5, "12.00", "8.00", "0.00"),
    ],
)
def test_02_deletion_summary(
    app_client: TestClient,
    db_session: Session,
    user: User,
    monkeypatch: pytest.MonkeyPatch,
    kind: int,
    cash: str,
    refundable: str,
    gift: str,
) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Clock:
            return cls.fromtimestamp(NOW.timestamp(), ET)

    monkeypatch.setattr("app.routers.me.datetime", Clock, raising=False)
    monkeypatch.setattr("app.services.credit_ledger.datetime", Clock)
    if kind == 1:
        ledger.adjust_by_admin(
            db_session,
            user_id=user.id,
            amount=Decimal(gift),
            note="fixture",
            idempotency_key="gift",
        )
        user.subscription_status, user.subscription_type = "active", "weekly"
    elif kind == 2:
        purchase(db_session, user, "12.00", 200, "old")
    elif kind == 3:
        purchase(db_session, user, "10.00", 30, "new")
        purchase(db_session, user, "2.00", 200, "old")
    else:
        purchase(db_session, user, "10.00", 120, "boundary")
        ledger.debit_refund(
            db_session,
            transaction_id="boundary",
            credits=Decimal("2.00"),
            request_key="partial",
            note="fixture",
        )
        if kind == 5:
            purchase(db_session, user, "4.00", 200, "old")
        else:
            ledger.consume_credits(
                db_session,
                user_id=user.id,
                amount=Decimal("2.00"),
                reason="subscription",
                idempotency_key="spent",
            )
    db_session.flush()
    response = app_client.get(PATH)
    assert response.status_code == 200
    assert response.json() == {
        "cash_balance": cash,
        "refundable_cash": refundable,
        "gift_balance": gift,
        "subscription_active": kind == 1,
    }


def test_03_zero_cash_purges_owned_data(
    app_client: TestClient, db_session: Session, user: User, auth: MagicMock
) -> None:
    ledger.grant_signup_credits(db_session, user)
    from app.models.api_audit_log import ApiAuditLog
    from app.models.api_token import ApiToken
    from app.services.api_tokens import new_token

    token, _ = new_token(db_session, user.id, "fixture", None)
    db_session.add(
        ApiAuditLog(
            user_id=user.id,
            token_id=token.id,
            endpoint="/agent/reports",
            status_code=200,
            client_ip="127.0.0.1",
        )
    )
    db_session.add_all([_h(user_id=user.id, name="fixture"), _report(user.id), _context(user.id)])
    db_session.flush()
    response = app_client.post(PATH, json=body())
    assert response.status_code == 204
    assert db_session.get(User, TEST_USER_ID) is None
    from app.models.holding import Holding
    from app.models.report import Report
    from app.models.user_investment_context import UserInvestmentContext

    for model in (Holding, Report, UserInvestmentContext, ApiToken, ApiAuditLog):
        assert not list(db_session.scalars(select(model).where(model.user_id == TEST_USER_ID)))
    history = rows(db_session)
    assert history and all(r.user_deleted_at is not None for r in history)
    assert all(r.reason != "relinquish" for r in history)
    auth.assert_called_once_with(f"sub-{TEST_USER_ID}")


@pytest.mark.parametrize("recent", [False, True])
def test_04_cash_relinquishment_before_auth(
    app_client: TestClient, db_session: Session, user: User, auth: MagicMock, recent: bool
) -> None:
    entry = ledger.record_purchase(
        db_session, user_id=user.id, credits=Decimal("12.00"), transaction_id="txn", note="fixture"
    ).entries[0]
    if not recent:
        entry.created_at = datetime.now(ET) - timedelta(days=200)
    db_session.flush()

    def deleted(sub: str) -> bool:
        assert db_session.get(User, TEST_USER_ID) is None
        relinquishments = [r for r in rows(db_session) if r.reason == "relinquish"]
        assert len(relinquishments) == 1
        assert relinquishments[0].user_deleted_at is not None
        return True

    auth.side_effect = deleted
    assert app_client.post(PATH, json=body("12.00", proof(app_client))).status_code == 204
    entry = next(r for r in rows(db_session) if r.reason == "relinquish")
    assert (entry.bucket, entry.amount, entry.actor_type, entry.balance_after) == (
        "cash",
        Decimal("-12.00"),
        "user",
        Decimal("0.00"),
    )
    assert entry.idempotency_key == f"relinquish:{TEST_USER_ID}"
    assert (
        entry.note
        == "Relinquished by user at account deletion; refundable within 120 days at that time: "
        + ("12.00" if recent else "0.00")
    )
    auth.assert_called_once()


@pytest.mark.parametrize("kind", ["missing", "invalid", "expired", "other-purpose"])
def test_05_invalid_proofs_do_nothing(
    app_client: TestClient,
    db_session: Session,
    user: User,
    auth: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    ledger.record_purchase(
        db_session, user_id=user.id, credits=Decimal("12.00"), transaction_id="txn", note="fixture"
    )
    payload: str | None = None
    if kind == "invalid":
        payload = "garbage"
    elif kind == "other-purpose":
        payload = _solve(create_change_password_challenge())
    elif kind == "expired":
        monkeypatch.setattr("app.services.altcha_challenge.CHALLENGE_TTL", timedelta(minutes=-1))
        payload = proof(app_client)
    response = app_client.post(PATH, json=body("12.00", payload))
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid captcha"
    db_session.refresh(user)
    assert user.credit_cash_balance == Decimal("12.00")
    assert len(rows(db_session)) == 1
    auth.assert_not_called()


@pytest.mark.parametrize(
    "cash,submitted,email,detail",
    [
        ("12.00", "12.00", "wrong@example.com", "confirm does not match account email"),
        ("12.00", "11.00", "user@example.com", "balance_changed"),
        ("12.00", "13.00", "user@example.com", "balance_changed"),
        ("0.00", "1.00", "user@example.com", "balance_changed"),
    ],
)
def test_06_and_11_locked_balance_and_email(
    app_client: TestClient,
    db_session: Session,
    user: User,
    auth: MagicMock,
    cash: str,
    submitted: str,
    email: str,
    detail: str,
) -> None:
    if Decimal(cash):
        ledger.record_purchase(
            db_session, user_id=user.id, credits=Decimal(cash), transaction_id="txn", note="fixture"
        )
    statements: list[str] = []

    def observe(
        _conn: object,
        _cursor: object,
        statement: str,
        _params: object,
        _context: object,
        _many: bool,
    ) -> None:
        statements.append(statement)

    bind = db_session.get_bind()
    event.listen(bind, "before_cursor_execute", observe)
    try:
        response = app_client.post(PATH, json={**body(submitted), "confirm_email": email})
    finally:
        event.remove(bind, "before_cursor_execute", observe)
    assert any("FOR UPDATE" in statement and "users" in statement for statement in statements)
    assert response.status_code == 409
    assert response.json()["detail"] == detail
    db_session.refresh(user)
    assert user.credit_cash_balance == Decimal(cash)
    assert all(r.reason != "relinquish" for r in rows(db_session))
    auth.assert_not_called()


def test_07_provider_failure_rolls_back(
    app_client: TestClient, db_session: Session, user: User, auth: MagicMock
) -> None:
    ledger.record_purchase(
        db_session, user_id=user.id, credits=Decimal("12.00"), transaction_id="txn", note="fixture"
    )
    holding = _h(user_id=user.id, name="fixture")
    db_session.add(holding)
    db_session.commit()
    hid = holding.id
    auth.side_effect = AuthProviderError("fixture")
    response = app_client.post(PATH, json=body("12.00", proof(app_client)))
    assert response.status_code == 502
    assert response.json()["detail"] == "failed to delete account; nothing was changed, retry"
    restored = db_session.get(User, TEST_USER_ID)
    assert restored is not None and restored.credit_cash_balance == Decimal("12.00")
    assert db_session.get(type(holding), hid) is not None
    assert len(rows(db_session)) == 1 and rows(db_session)[0].user_deleted_at is None


def test_08_commit_failure_rolls_back_and_alerts_once(
    app_client: TestClient,
    db_session: Session,
    user: User,
    auth: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger.record_purchase(
        db_session, user_id=user.id, credits=Decimal("12.00"), transaction_id="txn", note="fixture"
    )
    db_session.commit()
    alert = MagicMock()
    monkeypatch.setattr("app.routers.me.send_ops_alert", alert, raising=False)
    monkeypatch.setattr(db_session, "commit", MagicMock(side_effect=RuntimeError("fixture")))
    response = app_client.post(PATH, json=body("12.00", proof(app_client)))
    assert response.status_code == 500
    restored = db_session.get(User, TEST_USER_ID)
    assert restored is not None and restored.credit_cash_balance == Decimal("12.00")
    assert len(rows(db_session)) == 1
    auth.assert_called_once()
    alert.assert_called_once_with(
        "self-deletion commit failed after Auth delete", str(TEST_USER_ID)
    )


@pytest.mark.parametrize("kind", ["seed", "created-invites"])
def test_09_shared_refusals(
    app_client: TestClient,
    db_session: Session,
    user: User,
    auth: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    if kind == "seed":
        monkeypatch.setattr(get_settings(), "DEV_USER_ID", str(user.id))
    else:
        create_invite(db_session, created_by=user.id)
    response = app_client.post(PATH, json=body())
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "refusing to delete the seed user"
        if kind == "seed"
        else "user created invites; revoke or reassign first"
    )
    assert db_session.get(User, TEST_USER_ID) is not None
    auth.assert_not_called()


def test_10_ops_cash_refusal_unchanged(
    app_client: TestClient, db_session: Session, user: User, auth: MagicMock
) -> None:
    ledger.record_purchase(
        db_session, user_id=user.id, credits=Decimal("12.00"), transaction_id="txn", note="fixture"
    )
    response = app_client.delete(
        f"/admin/users/{user.id}", headers=_headers(), params={"confirm": user.email}
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "user has a cash balance; refund or adjust it to zero first"
    auth.assert_not_called()


@pytest.mark.parametrize("route", ["self", "ops"])
def test_10b_waitlist_and_invite_cleanup(
    app_client: TestClient, db_session: Session, user: User, route: str
) -> None:
    other = seed_user(db_session, uuid4(), "other@example.com")
    entries = [
        WaitlistEntry(email=" User@Example.com ", locale="en"),
        WaitlistEntry(email=other.email, locale="en"),
    ]
    db_session.add_all(entries)
    db_session.flush()
    invites = [
        Invite(
            token_hash=str(uuid4()),
            created_by=other.id,
            email=" USER@example.COM ",
            waitlist_entry_id=entries[0].id,
            used_by_user_id=user.id,
            used_at=NOW,
            letter_sent_at=NOW,
            letter_unsubscribed_at=NOW,
            expires_at=NOW + timedelta(days=1),
        ),
        Invite(
            token_hash=str(uuid4()),
            created_by=other.id,
            email="old@example.com",
            used_by_user_id=user.id,
            used_at=NOW,
            expires_at=NOW + timedelta(days=1),
        ),
        Invite(
            token_hash=str(uuid4()),
            created_by=other.id,
            email=other.email,
            waitlist_entry_id=entries[1].id,
            expires_at=NOW + timedelta(days=1),
        ),
    ]
    db_session.add_all(invites)
    db_session.flush()
    ids = [i.id for i in invites]
    wid, unrelated = entries[0].id, entries[1].id
    if route == "self":
        response = app_client.post(PATH, json=body())
    else:
        response = app_client.delete(
            f"/admin/users/{user.id}", headers=_headers(), params={"confirm": user.email}
        )
    assert response.status_code == (204 if route == "self" else 200)
    if route == "ops":
        assert response.json()["deleted"]["waitlist_entries"] == 1
        assert response.json()["deleted"]["invite_emails_cleared"] == 2
    assert db_session.get(WaitlistEntry, wid) is None
    assert db_session.get(WaitlistEntry, unrelated) is not None
    for iid in ids[:2]:
        row = db_session.get(Invite, iid)
        assert row is not None
        db_session.refresh(row)
        assert (row.email, row.waitlist_entry_id, row.used_by_user_id) == (None, None, None)
        assert row.used_at == NOW
    row = db_session.get(Invite, ids[0])
    assert row is not None and row.letter_sent_at == NOW and row.letter_unsubscribed_at == NOW
    row = db_session.get(Invite, ids[2])
    assert row is not None and row.email == other.email and row.waitlist_entry_id == unrelated


def test_10c_signup_fingerprint_survives(
    app_client: TestClient, db_session: Session, user: User
) -> None:
    grant = ledger.grant_signup_credits(db_session, user)
    assert grant is not None
    key = grant.idempotency_key
    assert app_client.post(PATH, json=body()).status_code == 204
    new = seed_user(db_session, uuid4(), "USER@Example.com")
    assert ledger.grant_signup_credits(db_session, new) is None
    assert (
        len(
            list(
                db_session.scalars(
                    select(CreditLedgerEntry).where(CreditLedgerEntry.idempotency_key == key)
                )
            )
        )
        == 1
    )


def test_10d_personal_token_cannot_delete(
    app_client: TestClient, db_session: Session, user: User, auth: MagicMock
) -> None:
    from app.services.api_tokens import new_token

    _token, plaintext = new_token(db_session, user.id, "fixture", None)
    app.dependency_overrides.pop(current_principal)
    response = app_client.post(PATH, json=body(), headers={"Authorization": f"Bearer {plaintext}"})
    assert response.status_code == 401
    assert db_session.get(User, TEST_USER_ID) is not None
    auth.assert_not_called()
