"""Authenticated subscription API, quotes, and integration call sites."""

import uuid
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.credit_ledger import CreditLedgerEntry
from app.models.holding import Holding
from app.models.user import User
from app.services import subscription
from app.services.credit_ledger import adjust_by_admin
from app.services.unsubscribe_token import create_token
from app.tests.conftest import TEST_USER_ID, seed_user
from app.tests.test_admin_router import _headers


@pytest.fixture
def account(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> User:
    user = seed_user(db_session, TEST_USER_ID)
    user.email_verified_at = datetime(2026, 10, 15, tzinfo=ET)
    db_session.flush()
    adjust_by_admin(
        db_session, user_id=user.id, amount=Decimal("5.00"), note="test", idempotency_key="seed"
    )
    monkeypatch.setattr("app.routers.me.today_et", lambda: date(2026, 10, 31), raising=False)
    db_session.commit()
    return user


def test_quote_matches_change_and_writes_nothing(
    app_client: TestClient, db_session: Session, account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    class QuoteClock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> "QuoteClock":
            return cls(2026, 10, 31, 12, tzinfo=ET)

    monkeypatch.setattr(subscription, "datetime", QuoteClock)
    subscription.set_plan(db_session, account.id, date(2026, 10, 15), "weekly")
    before = db_session.scalars(select(CreditLedgerEntry.id)).all()
    response = app_client.get("/me/subscription/quote", params={"type": "mwf"})
    assert response.status_code == 200, response.text
    quote = response.json()
    assert {
        k: quote[k]
        for k in (
            "action",
            "type",
            "fee",
            "returned",
            "balance",
            "balance_after",
            "sufficient",
            "period_start",
            "expires_on",
            "needs_holdings",
            "blocked",
        )
    } == {
        "action": "change",
        "type": "mwf",
        "fee": "1.99",
        "returned": "0.50",
        "balance": "4.01",
        "balance_after": "2.52",
        "sufficient": True,
        "period_start": "2026-10-31",
        "expires_on": "2026-11-30",
        "needs_holdings": True,
        "blocked": None,
    }
    assert quote["first_report_at"] == "2026-11-02T17:00:00-05:00"
    assert db_session.scalars(select(CreditLedgerEntry.id)).all() == before
    assert account.subscription_adjusted_on == date(2026, 10, 15)
    result = app_client.post("/me/subscription", json={"type": "mwf"})
    assert result.status_code == 200
    assert result.json() == {
        "status": "active",
        "type": "mwf",
        "expires_on": "2026-11-30",
        "cancel_pending": False,
        "next_adjustment_at": "2026-11-01T00:00:00-04:00",
    }
    db_session.refresh(account)
    assert account.credit_gift_balance == Decimal(quote["balance_after"])
    assert app_client.get("/me").json()["subscription"] == result.json()
    blocked = app_client.get("/me/subscription/quote", params={"type": "weekly"}).json()
    assert blocked["blocked"] == "daily_limit"
    assert blocked["fee"] == "0.99" and blocked["returned"] == "1.99"
    assert app_client.post("/me/subscription/cancel").json()["detail"] == "daily_limit"
    monkeypatch.setattr("app.routers.me.today_et", lambda: date(2026, 11, 1))
    account.email_verified_at = None
    db_session.flush()
    blocked = app_client.get("/me/subscription/quote", params={"type": "weekly"}).json()
    assert blocked["blocked"] == "email_unverified"
    assert blocked["balance"] == "2.52" and blocked["returned"] == "1.93"


def test_quote_needs_holdings_and_invalid_type(
    app_client: TestClient, db_session: Session, account: User
) -> None:
    for plan, needs in (("mwf", True), ("weekly", False)):
        response = app_client.get("/me/subscription/quote", params={"type": plan})
        assert response.status_code == 200
        assert response.json()["needs_holdings"] is needs
    db_session.add(
        Holding(
            user_id=account.id,
            name="Cash",
            pricing_mode="manual",
            currency="USD",
            asset_class="CASH_EQUIV",
            current_value=Decimal("100"),
        )
    )
    db_session.flush()
    assert not app_client.get("/me/subscription/quote", params={"type": "mwf"}).json()[
        "needs_holdings"
    ]
    assert app_client.get("/me/subscription/quote", params={"type": "invalid"}).status_code == 422
    assert app_client.post("/me/subscription", json={"type": "invalid"}).status_code == 422


@pytest.mark.parametrize("change", [False, True])
def test_api_insufficient_credits_is_atomic(
    app_client: TestClient, db_session: Session, account: User, change: bool
) -> None:
    adjust_by_admin(
        db_session,
        user_id=account.id,
        amount=Decimal("-2.53") if change else Decimal("-4.02"),
        note="test",
        idempotency_key="reduce",
    )
    if change:
        subscription.set_plan(db_session, account.id, date(2026, 10, 15), "weekly")
    before = db_session.scalars(select(CreditLedgerEntry.id)).all()
    status = account.subscription_status
    db_session.commit()
    response = app_client.post("/me/subscription", json={"type": "mwf" if change else "weekly"})
    assert response.status_code == 409 and response.json()["detail"] == "insufficient_credits"
    assert db_session.scalars(select(CreditLedgerEntry.id)).all() == before
    db_session.refresh(account)
    assert account.subscription_status == status


def test_api_email_same_plan_cancel_resume_and_midnight(
    app_client: TestClient, db_session: Session, account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    account.email_verified_at = None
    db_session.commit()
    response = app_client.post("/me/subscription", json={"type": "weekly"})
    assert response.status_code == 409 and response.json()["detail"] == "email_unverified"
    account.delivery_email_verified_at = datetime(2026, 10, 15, tzinfo=ET)
    db_session.flush()
    subscription.set_plan(db_session, account.id, date(2026, 10, 15), "weekly")
    db_session.commit()
    response = app_client.post("/me/subscription", json={"type": "weekly"})
    assert response.status_code == 409 and response.json()["detail"] == "no_change"
    monkeypatch.setattr("app.routers.me.today_et", lambda: date(2026, 10, 20))
    response = app_client.post("/me/subscription/cancel")
    assert response.status_code == 200 and response.json()["cancel_pending"]
    assert response.json()["next_adjustment_at"] == "2026-10-21T00:00:00-04:00"
    response = app_client.post("/me/subscription/resume")
    assert response.status_code == 409 and response.json()["detail"] == "daily_limit"
    monkeypatch.setattr("app.routers.me.today_et", lambda: date(2026, 10, 21))
    account.delivery_email_verified_at = None
    db_session.commit()
    response = app_client.post("/me/subscription/resume")
    assert response.status_code == 409 and response.json()["detail"] == "email_unverified"
    account.email_verified_at = datetime(2026, 10, 15, tzinfo=ET)
    db_session.commit()
    response = app_client.post("/me/subscription/resume")
    assert response.status_code == 200 and not response.json()["cancel_pending"]
    assert (
        len(
            db_session.scalars(
                select(CreditLedgerEntry).where(CreditLedgerEntry.reason == "subscription")
            ).all()
        )
        == 1
    )


def test_ops_cadence_suspended_and_subscription_directory(
    app_client: TestClient, db_session: Session, account: User
) -> None:
    before = account.report_cadence
    response = app_client.post(
        f"/admin/users/{account.id}/cadence", headers=_headers(), json={"report_cadence": "weekly"}
    )
    assert response.status_code == 409
    assert (
        response.json()["detail"]
        == "cadence changes are suspended; cadence follows the subscription"
    )
    db_session.refresh(account)
    assert account.report_cadence == before
    # Signup's no-delivery cadence is also a valid directory filter.
    other = seed_user(db_session, uuid.uuid4())
    other.report_cadence = "none"
    db_session.flush()
    response = app_client.get("/admin/users", headers=_headers(), params={"report_cadence": "none"})
    assert response.status_code == 200
    row = next(r for r in response.json() if r["id"] == str(other.id))
    assert {
        k: row[k]
        for k in (
            "subscription_status",
            "subscription_type",
            "subscription_expires_on",
            "subscription_cancel_pending",
        )
    } == {
        "subscription_status": "inactive",
        "subscription_type": None,
        "subscription_expires_on": None,
        "subscription_cancel_pending": False,
    }


@pytest.mark.parametrize("other_verified", [False, True])
def test_unsubscribe_subscription_effect(
    app_client: TestClient, db_session: Session, account: User, other_verified: bool
) -> None:
    subscription.set_plan(db_session, account.id, date(2026, 10, 15), "weekly")
    account.delivery_email = "other@example.com"
    if other_verified:
        account.delivery_email_verified_at = datetime(2026, 10, 15, tzinfo=ET)
    db_session.flush()
    before = db_session.scalars(select(CreditLedgerEntry.id)).all()
    token = create_token(user_id=account.id, purpose="account_email", email=account.email)
    response = app_client.post("/unsubscribe/confirm", json={"token": token})
    assert response.status_code == 200
    db_session.refresh(account)
    assert account.email_verified_at is None
    assert account.subscription_cancel_pending is (not other_verified)
    assert account.subscription_adjusted_on == date(2026, 10, 15)
    assert account.subscription_type == account.report_cadence == "weekly"
    assert account.subscription_status == "active"
    assert account.subscription_expires_on == date(2026, 11, 15)
    assert db_session.scalars(select(CreditLedgerEntry.id)).all() == before
