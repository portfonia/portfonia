"""Paddle purchase and refund contracts against the migrated Postgres ledger."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.deps import Principal, current_principal
from app.core.timezones import ET
from app.main import app
from app.models.credit_ledger import CreditLedgerEntry
from app.routers import admin, paddle_webhooks, payments
from app.services.credit_ledger import consume_credits, record_purchase
from app.services.paddle_client import PaddleApiError
from app.tests.conftest import TEST_USER_ID, seed_user
from app.tests.test_admin_router import _headers

PRICE = "pri_test10"


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        PADDLE_ENVIRONMENT="production",
        PADDLE_CLIENT_SIDE_TOKEN="live_test",
        PADDLE_CREDIT_PACKS={PRICE: Decimal("10")},
        PADDLE_WEBHOOK_SECRET=SecretStr("test_webhook_secret"),
    )


def _signed(payload: dict[str, object], *, valid: bool = True) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    digest = hmac.new(b"test_webhook_secret", b"123:" + raw, hashlib.sha256).hexdigest()
    return raw, {"Paddle-Signature": f"ts=123;h1={digest if valid else '0' * 64}"}


def _purchase_payload(user_id: str | None, *, price_id: str = PRICE) -> dict[str, object]:
    return {
        "event_type": "transaction.completed",
        "data": {
            "id": "txn_A",
            "status": "completed",
            "customer_id": "ctm_test",
            "custom_data": {"user_id": user_id} if user_id else {},
            "currency_code": "HKD",
            "details": {"totals": {"total": "7800"}},
            "items": [{"price": {"id": price_id}, "quantity": 1}],
        },
    }


def test_signed_purchase_redelivery_and_invalid_signature(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paddle_webhooks, "get_settings", _settings)
    user = seed_user(db_session, uuid.uuid4())
    raw, headers = _signed(_purchase_payload(str(user.id)))
    assert app_client.post("/webhooks/paddle", content=raw, headers=headers).status_code == 200
    assert app_client.post("/webhooks/paddle", content=raw, headers=headers).status_code == 200
    bad_raw, bad_headers = _signed(_purchase_payload(str(user.id)), valid=False)
    assert (
        app_client.post("/webhooks/paddle", content=bad_raw, headers=bad_headers).status_code == 401
    )
    rows = db_session.scalars(
        select(CreditLedgerEntry).where(CreditLedgerEntry.user_id == user.id)
    ).all()
    assert len(rows) == 1
    assert (rows[0].amount, rows[0].reference, rows[0].note) == (Decimal("10"), "txn_A", "HKD 7800")
    db_session.refresh(user)
    assert user.credit_cash_balance == Decimal("10")


def test_unmatched_and_external_adjustment_alert_without_writing(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paddle_webhooks, "get_settings", _settings)
    alert = MagicMock()
    monkeypatch.setattr(paddle_webhooks, "send_ops_alert", alert)
    raw, headers = _signed(_purchase_payload(None))
    assert app_client.post("/webhooks/paddle", content=raw, headers=headers).status_code == 200
    assert alert.call_count == 1
    raw, headers = _signed(
        {
            "event_type": "adjustment.created",
            "data": {
                "id": "adj_external",
                "reason": "dashboard",
                "action": "refund",
                "transaction_id": "txn_A",
            },
        }
    )
    assert app_client.post("/webhooks/paddle", content=raw, headers=headers).status_code == 200
    assert alert.call_count == 2
    raw, headers = _signed(
        {
            "event_type": "adjustment.created",
            "data": {
                "id": "adj_ours",
                "reason": "portfonia-refund:refund:txn_A:k1",
            },
        }
    )
    assert app_client.post("/webhooks/paddle", content=raw, headers=headers).status_code == 200
    assert alert.call_count == 2
    assert db_session.scalars(select(CreditLedgerEntry)).first() is None


def test_unknown_price_credits_known_items_and_unset_secret_rejects(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    monkeypatch.setattr(paddle_webhooks, "get_settings", lambda: settings)
    alert = MagicMock()
    monkeypatch.setattr(paddle_webhooks, "send_ops_alert", alert)
    user = seed_user(db_session, uuid.uuid4())
    payload = _purchase_payload(str(user.id))
    data = payload["data"]
    assert isinstance(data, dict)
    data["items"] = [
        {"price": {"id": PRICE}, "quantity": 1},
        {"price": {"id": "pri_unknown"}, "quantity": 1},
    ]
    raw, headers = _signed(payload)
    assert app_client.post("/webhooks/paddle", content=raw, headers=headers).status_code == 200
    assert alert.call_count == 1
    db_session.refresh(user)
    assert user.credit_cash_balance == Decimal("10")
    settings.PADDLE_WEBHOOK_SECRET = None
    assert app_client.post("/webhooks/paddle", content=raw, headers=headers).status_code == 503
    settings.PADDLE_WEBHOOK_SECRET = SecretStr("test_webhook_secret")
    settings.PADDLE_ENVIRONMENT = None
    assert app_client.post("/webhooks/paddle", content=raw, headers=headers).status_code == 503


def test_checkout_config_is_authenticated_and_exposes_only_public_values(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    settings.PADDLE_CREDIT_PACKS = {"pri_20": Decimal("20"), PRICE: Decimal("10")}
    monkeypatch.setattr(payments, "get_settings", lambda: settings)
    app.dependency_overrides[current_principal] = lambda: Principal(
        user_id=uuid.uuid4(), email="a@example.com"
    )
    response = app_client.get("/payments/checkout-config")
    assert response.status_code == 200
    assert response.json()["packs"] == [
        {"price_id": PRICE, "credits": "10.00"},
        {"price_id": "pri_20", "credits": "20.00"},
    ]
    assert "test_webhook_secret" not in response.text
    settings.PADDLE_CLIENT_SIDE_TOKEN = None
    assert app_client.get("/payments/checkout-config").status_code == 503
    app.dependency_overrides.pop(current_principal)
    assert app_client.get("/payments/checkout-config").status_code == 401


def test_purchase_status_reports_own_credited_purchase(
    app_client: TestClient, db_session: Session
) -> None:
    user = seed_user(db_session, TEST_USER_ID)
    record_purchase(
        db_session, user_id=user.id, credits=Decimal("10"), transaction_id="txn_A", note="HKD 7800"
    )
    response = app_client.get("/payments/purchases/txn_A")
    assert response.status_code == 200
    assert response.json() == {"transaction_id": "txn_A", "credited": True, "credits": "10.00"}


def test_purchase_status_unknown_transaction_is_not_credited(app_client: TestClient) -> None:
    response = app_client.get("/payments/purchases/txn_missing")
    assert response.status_code == 200
    assert response.json() == {
        "transaction_id": "txn_missing",
        "credited": False,
        "credits": None,
    }


def test_purchase_status_hides_another_users_purchase(
    app_client: TestClient, db_session: Session
) -> None:
    owner = seed_user(db_session, uuid.uuid4())
    record_purchase(
        db_session,
        user_id=owner.id,
        credits=Decimal("10"),
        transaction_id="txn_B",
        note="HKD 7800",
    )
    response = app_client.get("/payments/purchases/txn_B")
    assert response.status_code == 200
    assert response.json() == {"transaction_id": "txn_B", "credited": False, "credits": None}


def test_purchase_status_requires_auth_and_txn_prefix(app_client: TestClient) -> None:
    app.dependency_overrides.pop(current_principal)
    assert app_client.get("/payments/purchases/txn_A").status_code == 401
    app.dependency_overrides[current_principal] = lambda: Principal(
        user_id=uuid.uuid4(), email="a@example.com"
    )
    assert app_client.get("/payments/purchases/not-a-txn").status_code == 422


def test_refund_partial_replay_and_rejected_reversal(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = seed_user(db_session, uuid.uuid4())
    record_purchase(
        db_session, user_id=user.id, credits=Decimal("10"), transaction_id="txn_A", note="HKD 7800"
    )
    get_transaction = MagicMock(
        return_value={
            "status": "completed",
            "currency_code": "HKD",
            "details": {
                "line_items": [
                    {"id": "txnitm_1", "totals": {"total": "7800"}},
                ]
            },
        }
    )
    create_adjustment = MagicMock(return_value={"id": "adj_X", "status": "pending_approval"})
    monkeypatch.setattr(admin, "get_transaction", get_transaction)
    monkeypatch.setattr(admin, "create_refund_adjustment", create_adjustment)
    body = {
        "transaction_id": "txn_A",
        "credits": "4.00",
        "note": "customer request",
        "idempotency_key": "k1",
    }
    first = app_client.post("/admin/payments/refunds", headers=_headers(), json=body)
    assert first.status_code == 200, first.text
    assert first.json()["refund_amount"] == "3120"
    assert create_adjustment.call_args.kwargs["full"] is False
    assert create_adjustment.call_args.kwargs["amount"] == "3120"
    replay = app_client.post("/admin/payments/refunds", headers=_headers(), json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert replay.json()["adjustment_id"] == "adj_X"
    assert all(
        replay.json()[field] is None
        for field in ("refund_amount", "currency_code", "adjustment_status")
    )
    assert get_transaction.call_count == create_adjustment.call_count == 1
    too_many = app_client.post(
        "/admin/payments/refunds",
        headers=_headers(),
        json={**body, "credits": "7", "idempotency_key": "k2"},
    )
    assert too_many.status_code == 409
    assert too_many.json()["detail"] == "credits exceed unrefunded purchase"

    monkeypatch.setattr(paddle_webhooks, "get_settings", _settings)
    raw, headers = _signed(
        {"event_type": "adjustment.updated", "data": {"id": "adj_X", "status": "rejected"}}
    )
    assert app_client.post("/webhooks/paddle", content=raw, headers=headers).status_code == 200
    assert app_client.post("/webhooks/paddle", content=raw, headers=headers).status_code == 200
    db_session.refresh(user)
    assert user.credit_cash_balance == Decimal("10")
    rows = db_session.scalars(
        select(CreditLedgerEntry).where(CreditLedgerEntry.user_id == user.id)
    ).all()
    assert [row.amount for row in rows] == [Decimal("10"), Decimal("-4"), Decimal("4")]


def test_refund_api_failure_rolls_back_debit(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = seed_user(db_session, uuid.uuid4())
    record_purchase(
        db_session, user_id=user.id, credits=Decimal("10"), transaction_id="txn_B", note="USD 1000"
    )
    db_session.commit()
    monkeypatch.setattr(
        admin,
        "get_transaction",
        MagicMock(
            return_value={
                "status": "completed",
                "currency_code": "USD",
                "details": {
                    "line_items": [
                        {"id": "txnitm_2", "totals": {"total": "1000"}},
                    ]
                },
            }
        ),
    )
    monkeypatch.setattr(
        admin,
        "create_refund_adjustment",
        MagicMock(side_effect=PaddleApiError(503, "down", "unavailable")),
    )
    response = app_client.post(
        "/admin/payments/refunds",
        headers=_headers(),
        json={
            "transaction_id": "txn_B",
            "credits": "10",
            "note": "request",
            "idempotency_key": "k2",
        },
    )
    assert response.status_code == 502
    db_session.refresh(user)
    assert user.credit_cash_balance == Decimal("10")
    assert response.json() == {"detail": "paddle error", "paddle_code": "down"}


def test_remaining_and_full_refunds_use_expected_paddle_payloads(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = seed_user(db_session, uuid.uuid4())
    record_purchase(
        db_session, user_id=user.id, credits=Decimal("10"), transaction_id="txn_A", note="HKD 7800"
    )
    record_purchase(
        db_session, user_id=user.id, credits=Decimal("20"), transaction_id="txn_B", note="USD 2000"
    )
    transaction = MagicMock(
        side_effect=lambda txn: {
            "status": "completed",
            "currency_code": "HKD" if txn == "txn_A" else "USD",
            "details": {
                "line_items": [
                    {
                        "id": "txnitm_1",
                        "totals": {
                            "total": "7800" if txn == "txn_A" else "2000",
                        },
                    }
                ]
            },
        }
    )
    adjustment = MagicMock(
        side_effect=[
            {"id": "adj_1", "status": "pending_approval"},
            {"id": "adj_2", "status": "pending_approval"},
            {"id": "adj_3", "status": "pending_approval"},
        ]
    )
    monkeypatch.setattr(admin, "get_transaction", transaction)
    monkeypatch.setattr(admin, "create_refund_adjustment", adjustment)

    def refund(txn: str, credits: str, key: str) -> dict[str, object]:
        response = app_client.post(
            "/admin/payments/refunds",
            headers=_headers(),
            json={
                "transaction_id": txn,
                "credits": credits,
                "note": "customer request",
                "idempotency_key": key,
            },
        )
        assert response.status_code == 200, response.text
        return cast(dict[str, object], response.json())

    assert refund("txn_A", "4", "k1")["refund_amount"] == "3120"
    assert refund("txn_A", "6", "k2")["refund_amount"] == "4680"
    assert adjustment.call_args_list[1].kwargs["full"] is False
    assert refund("txn_B", "20", "k3")["refund_amount"] == "2000"
    assert adjustment.call_args_list[2].kwargs["full"] is True
    db_session.refresh(user)
    assert user.credit_cash_balance == Decimal("0")


def test_refund_rejects_spent_cash_and_expired_purchase_before_paddle(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    spent = seed_user(db_session, uuid.uuid4())
    old = seed_user(db_session, uuid.uuid4())
    record_purchase(
        db_session,
        user_id=spent.id,
        credits=Decimal("20"),
        transaction_id="txn_spent",
        note="USD 2000",
    )
    record_purchase(
        db_session, user_id=old.id, credits=Decimal("10"), transaction_id="txn_old", note="USD 1000"
    )
    consume_credits(
        db_session,
        user_id=spent.id,
        amount=Decimal("8"),
        reason="subscription",
        idempotency_key="spent",
    )
    db_session.execute(
        update(CreditLedgerEntry)
        .where(CreditLedgerEntry.idempotency_key == "recharge:paddle:txn_old")
        .values(
            created_at=datetime.now(ET) - timedelta(days=121),
        )
    )
    db_session.commit()
    paddle = MagicMock()
    monkeypatch.setattr(admin, "get_transaction", paddle)
    for txn, credits, expected in [
        ("txn_spent", "20", "insufficient cash balance"),
        ("txn_old", "10", "refund window closed"),
    ]:
        response = app_client.post(
            "/admin/payments/refunds",
            headers=_headers(),
            json={
                "transaction_id": txn,
                "credits": credits,
                "note": "request",
                "idempotency_key": txn,
            },
        )
        assert response.status_code == 409
        assert response.json()["detail"] == expected
    paddle.assert_not_called()


def test_webhook_handler_runs_in_threadpool_not_event_loop() -> None:
    # The handler does blocking DB work and sync httpx alert sends; as an
    # `async def` it would stall the event loop for every other request.
    import inspect

    assert not inspect.iscoroutinefunction(paddle_webhooks.paddle_webhook)
