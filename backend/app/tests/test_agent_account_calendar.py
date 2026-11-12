"""Account, subscription, calendar and API discovery contracts for #877."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.timezones import today_et
from app.models.holding import Holding
from app.models.user import User
from app.tests.conftest import TEST_USER_ID, seed_user


@pytest.fixture
def account_token(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> dict[str, str]:
    user = seed_user(db_session, TEST_USER_ID)
    user.subscription_status = "inactive"
    user.subscription_type = None
    user.report_cadence = "none"
    user.credit_cash_balance = Decimal("0.00")
    user.credit_gift_balance = Decimal("60.00")
    user.jade_discount_periods_used = 0
    db_session.commit()
    monkeypatch.setattr("app.core.agent_limits.quiet_retry_after", lambda _: None)
    token = app_client.post(
        "/me/api-tokens", json={"name": "account", "permission": "holdings_write"}
    )
    assert token.status_code == 201
    return {"Authorization": "Bearer " + token.json()["token"]}


def test_877_account(app_client: TestClient, account_token: dict[str, str]) -> None:
    response = app_client.get("/agent/v1/account", headers=account_token)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["subscription"]["status"] == "inactive"
    assert body["balance"] == {"cash": "0.00", "gift": "60.00", "total": "60.00"}
    assert [p["type"] for p in body["plans"]] == ["weekly", "mwf", "daily", "jade"]
    assert [p["fee"] for p in body["plans"]] == ["0.99", "1.99", "2.49", "6.99"]
    assert body["top_up_url"] == "https://portfonia.com/profile"


def test_877_subscription(
    app_client: TestClient,
    db_session: Session,
    account_token: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reminder = Mock()
    monkeypatch.setattr("app.services.subscription.maybe_send_low_balance_reminder", reminder)
    reader = app_client.post("/me/api-tokens", json={"name": "reader"}).json()
    denied = app_client.post(
        "/agent/v1/subscription",
        json={"type": "weekly"},
        headers={"Authorization": "Bearer " + reader["token"]},
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "holdings_write_required"
    response = app_client.post(
        "/agent/v1/subscription", json={"type": "weekly"}, headers=account_token
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["subscription"]["status"] == "active"
    assert body["subscription"]["type"] == "weekly"
    assert body["balance"]["gift"] == "59.01"
    assert (
        db_session.scalar(
            text("SELECT count(*) FROM credit_ledger WHERE user_id=:uid AND reason='subscription'"),
            {"uid": TEST_USER_ID},
        )
        == 1
    )
    reminder.assert_called_once()
    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    user.subscription_status = "inactive"
    user.subscription_type = None
    user.subscription_adjusted_on = None
    user.credit_cash_balance = Decimal("0.00")
    user.credit_gift_balance = Decimal("0.00")
    db_session.commit()
    short = app_client.post(
        "/agent/v1/subscription", json={"type": "weekly"}, headers=account_token
    )
    assert short.status_code == 409
    assert short.json() == {"detail": "insufficient_credits"}


def test_877_calendar(
    app_client: TestClient,
    db_session: Session,
    account_token: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    day = today_et()
    other = uuid4()
    seed_user(db_session, other)
    for uid, ticker in [(TEST_USER_ID, "AAPL"), (TEST_USER_ID, "TQQQ"), (other, "MSFT")]:
        db_session.add(
            Holding(
                user_id=uid,
                ticker=ticker,
                name=ticker,
                currency="USD",
                pricing_mode="auto",
                asset_type="stock",
                asset_class="STOCK",
                market="US",
                capture_supported=True,
            )
        )
    for kind, name, ticker, source in [
        ("macro", "FOMC", "", "fomc"),
        ("earnings", "Apple", "AAPL", "yfinance"),
        ("earnings", "Microsoft", "MSFT", "yfinance"),
        ("earnings", "Underlying", "QQQ", "yfinance"),
    ]:
        db_session.execute(
            text(
                "INSERT INTO forward_events (event_type,name,ticker,scheduled_date,source) VALUES (:kind,:name,:ticker,:day,:source)"
            ),
            {"kind": kind, "name": name, "ticker": ticker, "day": day, "source": source},
        )
    db_session.commit()
    monkeypatch.setattr(
        "app.routers.agent.load_leverage_entries",
        lambda _: {"TQQQ": SimpleNamespace(underlying_ticker="QQQ")},
        raising=False,
    )
    response = app_client.get("/agent/v1/calendar", headers=account_token)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["start"] == str(day)
    assert body["end"] == str(day + timedelta(days=14))
    assert body["coverage"] == "us"
    assert [(e["type"], e["name"], e["ticker"]) for e in body["events"]] == [
        ("earnings", "Apple", "AAPL"),
        ("earnings", "Underlying", "QQQ"),
        ("macro", "FOMC", None),
    ]
    too_long = app_client.get(
        "/agent/v1/calendar",
        headers=account_token,
        params={"start": str(day), "end": str(day + timedelta(days=31))},
    )
    assert too_long.status_code == 422


def test_877_api_root(app_client: TestClient) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    assert response.json() == {
        "service": "Portfonia API",
        "agent_docs": "https://portfonia.com/llms.txt",
        "agent_openapi": "https://api.portfonia.com/agent/openapi.json",
    }
