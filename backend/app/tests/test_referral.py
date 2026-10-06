"""Issue #675 referral acceptance against real PostgreSQL."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import rate_limit
from app.core.config import get_settings
from app.core.timezones import ET
from app.models.credit_ledger import CreditLedgerEntry
from app.models.user import User
from app.models.waitlist_entry import WaitlistEntry
from app.services import credit_ledger as ledger
from app.services import invites, subscription
from app.services.user_purge import purge_user
from app.tests.conftest import TEST_USER_ID, seed_user
from app.tests.test_admin_router import _headers
from app.tests.test_paddle_payments import _purchase_payload, _settings, _signed


def pair(session: Session) -> tuple[User, User]:
    parent = seed_user(session, uuid.uuid4())
    child = seed_user(session, TEST_USER_ID)
    child.invited_by = parent.id
    child.email_verified_at = datetime(2026, 10, 5, tzinfo=ET)
    session.flush()
    return parent, child


def rows(session: Session, reason: str) -> list[CreditLedgerEntry]:
    return list(
        session.scalars(
            select(CreditLedgerEntry)
            .where(CreditLedgerEntry.reason == reason)
            .order_by(CreditLedgerEntry.id)
        )
    )


def balanced(session: Session, user: User) -> None:
    session.refresh(user)
    for bucket in ("cash", "gift"):
        entries = list(
            session.scalars(
                select(CreditLedgerEntry)
                .where(CreditLedgerEntry.user_id == user.id, CreditLedgerEntry.bucket == bucket)
                .order_by(CreditLedgerEntry.id)
            )
        )
        value = getattr(user, f"credit_{bucket}_balance")
        assert value == sum((r.amount for r in entries), Decimal("0"))
        assert value == (entries[-1].balance_after if entries else Decimal("0"))
    assert user.credit_gift_balance >= 0


def test_submission_new_and_private_noops(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = seed_user(db_session, TEST_USER_ID)
    existing = seed_user(db_session, uuid.uuid4())
    notice = MagicMock()
    monkeypatch.setattr("app.tasks.admin_tasks.send_admin_alert_task.delay", notice)
    body = {"email": " New@Example.com ", "locale": "zh-Hant"}
    response = app_client.post("/me/referrals", json=body)
    assert response.status_code == 200 and response.json() == {"received": True}
    entry = db_session.scalar(select(WaitlistEntry).where(WaitlistEntry.email == "new@example.com"))
    assert entry is not None
    assert (entry.source, entry.status, entry.referrer_user_id, entry.locale) == (
        "referral",
        "pending",
        user.id,
        "zh-Hant",
    )
    assert notice.call_count == 1
    assert f"Referred by: {user.email}" in notice.call_args.args[1]
    for email in [user.email, existing.email, "new@example.com"]:
        response = app_client.post("/me/referrals", json={**body, "email": email})
        assert response.status_code == 200 and response.json() == {"received": True}
    assert notice.call_count == 1
    assert len(list(db_session.scalars(select(WaitlistEntry)))) == 1


def test_submission_limit_validation_and_redis(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_user(db_session, TEST_USER_ID)
    body = {"email": "", "locale": "en"}
    assert app_client.post("/me/referrals", json=body).status_code == 422
    for n in range(50):
        email = "not-an-email" if n == 0 else f"a{n}@example.com"
        assert app_client.post("/me/referrals", json={**body, "email": email}).status_code == 200
    assert (
        app_client.post("/me/referrals", json={**body, "email": "limit@example.com"}).status_code
        == 429
    )
    monkeypatch.setattr("app.core.rate_limit.today_et", lambda: date(2026, 10, 6))
    assert (
        app_client.post("/me/referrals", json={**body, "email": "tomorrow@example.com"}).status_code
        == 200
    )
    monkeypatch.setattr(
        rate_limit.get_backend(),
        "incr_with_ttl",
        MagicMock(side_effect=rate_limit.RateLimitUnavailable),
    )
    assert (
        app_client.post("/me/referrals", json={**body, "email": "redis@example.com"}).status_code
        == 503
    )


@pytest.mark.parametrize(
    "kind", ["referral", "null_parent", "organic", "plain", "purged", "real_creator"]
)
def test_signup_attribution(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    root = uuid.UUID(get_settings().ADMIN_ID)
    parent = seed_user(db_session, uuid.uuid4())
    grand = uuid.uuid4()
    parent.invited_by = None if kind == "null_parent" else grand
    db_session.flush()
    entry = None
    if kind not in ("plain", "real_creator"):
        entry = WaitlistEntry(
            email="referee@example.com",
            locale="en",
            source="organic" if kind == "organic" else "referral",
            referrer_user_id=parent.id,
        )
        db_session.add(entry)
        db_session.flush()
    invite = invites.create_invite(
        db_session,
        created_by=parent.id if kind == "real_creator" else root,
        waitlist_entry_id=entry.id if entry else None,
    )
    if kind == "purged":
        purge_user(db_session, parent.id)
    monkeypatch.setattr("app.routers.auth.create_auth_user", lambda *_: str(uuid.uuid4()))
    response = app_client.post(
        "/auth/signup",
        json={
            "invite_token": invite.token,
            "email": "referee@example.com",
            "password": "a-long-password",
            "tos_accepted": True,
        },
    )
    assert response.status_code == 201, response.text
    child = db_session.get(User, uuid.UUID(response.json()["id"]))
    assert child is not None
    expected = (
        (parent.id, root if kind == "null_parent" else grand)
        if kind in ("referral", "null_parent")
        else (root, root)
    )
    assert (child.invited_by, child.grand_invited_by) == expected
    assert db_session.get(User, root) is None


@pytest.mark.parametrize("grant,expected", [("5.00", "1.00"), ("10.00", "2.00"), ("0", "0")])
def test_first_subscription_amount_and_later_calls(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, grant: str, expected: str
) -> None:
    parent, child = pair(db_session)
    ledger.adjust_by_admin(
        db_session, user_id=child.id, amount=Decimal("20"), note="test gift", idempotency_key="gift"
    )
    monkeypatch.setattr(get_settings(), "SIGNUP_GRANT_CREDITS", Decimal(grant))
    subscription.set_plan(db_session, child.id, date(2026, 10, 5), "weekly")
    assert parent.credit_gift_balance == Decimal(expected)
    monkeypatch.setattr(get_settings(), "SIGNUP_GRANT_CREDITS", Decimal("5"))
    subscription.set_plan(db_session, child.id, date(2026, 10, 6), "mwf")
    subscription._fresh_subscribe(db_session, child, date(2026, 12, 6), "weekly")
    db_session.flush()
    child.subscription_status = "expired"
    db_session.flush()
    assert subscription._check_user(db_session, child, date(2027, 1, 7)) == "resumed"
    db_session.flush()
    assert subscription._check_user(db_session, child, date(2027, 5, 7)) == "renewed"
    assert parent.credit_gift_balance == Decimal(expected)
    assert len(rows(db_session, "referral_bonus")) == (0 if grant == "0" else 1)
    balanced(db_session, parent)


def test_first_bonus_hash_survives_purge_and_root_is_not_paid(db_session: Session) -> None:
    parent, child = pair(db_session)
    email = child.email
    ledger.adjust_by_admin(
        db_session, user_id=child.id, amount=Decimal("5"), note="test", idempotency_key="gift"
    )
    subscription._fresh_subscribe(db_session, child, date(2026, 10, 5), "weekly")
    db_session.flush()
    assert ledger.referral_subscription_bonus(db_session, child) is None
    purge_user(db_session, child.id)
    replacement = seed_user(db_session, uuid.uuid4(), email)
    replacement.invited_by = parent.id
    db_session.flush()
    ledger.adjust_by_admin(
        db_session,
        user_id=replacement.id,
        amount=Decimal("5"),
        note="test",
        idempotency_key="new-gift",
    )
    subscription._fresh_subscribe(db_session, replacement, date(2026, 10, 6), "weekly")
    db_session.flush()
    assert parent.credit_gift_balance == Decimal("1")
    root_child = seed_user(db_session, uuid.uuid4())
    root_child.invited_by = uuid.UUID(get_settings().ADMIN_ID)
    db_session.flush()
    ledger.adjust_by_admin(
        db_session,
        user_id=root_child.id,
        amount=Decimal("5"),
        note="test",
        idempotency_key="root-gift",
    )
    subscription._fresh_subscribe(db_session, root_child, date(2026, 10, 5), "weekly")
    assert (
        ledger.referral_recharge_bonus(db_session, root_child.id, "root-tx", Decimal("10")) is None
    )
    assert len(rows(db_session, "referral_bonus")) == 1


def test_webhook_bonus_replay_and_atomic_failure(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent, child = pair(db_session)
    monkeypatch.setattr("app.routers.paddle_webhooks.get_settings", _settings)
    raw, headers = _signed(_purchase_payload(str(child.id)))
    for _ in range(2):
        assert app_client.post("/webhooks/paddle", content=raw, headers=headers).status_code == 200
    assert parent.credit_cash_balance == Decimal("1.50")
    assert rows(db_session, "referral_bonus")[0].idempotency_key == "referral_recharge:txn_A"
    db_session.commit()
    broken = _purchase_payload(str(child.id))
    data = broken["data"]
    assert isinstance(data, dict)
    data["id"] = "txn_fail"
    raw, headers = _signed(broken)
    monkeypatch.setattr(
        "app.routers.paddle_webhooks.referral_recharge_bonus",
        MagicMock(side_effect=RuntimeError("injected bonus failure")),
    )
    with pytest.raises(RuntimeError, match="injected bonus failure"):
        app_client.post("/webhooks/paddle", content=raw, headers=headers)
    assert (
        db_session.scalar(
            select(CreditLedgerEntry).where(
                CreditLedgerEntry.idempotency_key == "recharge:paddle:txn_fail"
            )
        )
        is None
    )
    balanced(db_session, parent)


def test_partial_clawback_negative_reversal_and_no_bonus(db_session: Session) -> None:
    parent, child = pair(db_session)
    ledger.record_purchase(
        db_session, user_id=child.id, credits=Decimal("10"), transaction_id="txn_T", note="test"
    )
    ledger.referral_recharge_bonus(db_session, child.id, "txn_T", Decimal("10"))
    ledger.consume_credits(
        db_session, user_id=parent.id, amount=Decimal("1.50"), reason="qa", idempotency_key="spent"
    )
    for key, amount in [("one", "3"), ("two", "7")]:
        debit = ledger.debit_refund(
            db_session,
            transaction_id="txn_T",
            credits=Decimal(amount),
            request_key=key,
            note="test",
        )
        debit.entries[0].reference = f"adj_{key}"
        db_session.flush()
        ledger.referral_clawback(db_session, "txn_T", ledger.refund_key("txn_T", key))
        ledger.referral_clawback(db_session, "txn_T", ledger.refund_key("txn_T", key))
    assert [r.amount for r in rows(db_session, "referral_clawback")] == [
        Decimal("-0.45"),
        Decimal("-1.05"),
    ]
    assert parent.credit_cash_balance == Decimal("-1.50")
    assert rows(db_session, "referral_clawback")[-1].balance_after == Decimal("-1.50")
    ledger.reverse_refund(db_session, adjustment_id="adj_two")
    ledger.reverse_referral_clawback(db_session, "refund:txn_T:two", "txn_T", "adj_two")
    ledger.reverse_referral_clawback(db_session, "refund:txn_T:two", "txn_T", "adj_two")
    assert [r.amount for r in rows(db_session, "referral_clawback")] == [
        Decimal("-0.45"),
        Decimal("-1.05"),
        Decimal("1.05"),
    ]
    assert parent.credit_cash_balance == Decimal("-0.45")
    assert ledger.referral_clawback(db_session, "no-bonus", "refund:no-bonus:key").write is None
    balanced(db_session, parent)
    balanced(db_session, child)


def test_negative_cash_consumption_and_positive_writes(
    app_client: TestClient, db_session: Session
) -> None:
    parent, child = pair(db_session)
    ledger._post(
        db_session,
        parent,
        bucket="cash",
        amount=Decimal("-0.50"),
        reason="referral_clawback",
        actor_type="system",
        idempotency_key="negative",
        allow_negative=True,
    )
    ledger.adjust_by_admin(
        db_session, user_id=parent.id, amount=Decimal("3"), note="test", idempotency_key="gift"
    )
    with pytest.raises(ledger.InsufficientCredits):
        ledger.consume_credits(
            db_session,
            user_id=parent.id,
            amount=Decimal("2.99"),
            reason="subscription",
            idempotency_key="too-much",
        )
    ledger.consume_credits(
        db_session,
        user_id=parent.id,
        amount=Decimal("2.49"),
        reason="subscription",
        idempotency_key="enough",
    )
    assert parent.credit_gift_balance == Decimal("0.51")
    with pytest.raises(ledger.InsufficientCredits):
        ledger.adjust_by_admin(
            db_session,
            user_id=parent.id,
            amount=Decimal("-0.01"),
            note="test",
            idempotency_key="cash-debit",
            bucket="cash",
        )
    ledger.record_purchase(
        db_session, user_id=parent.id, credits=Decimal("0.10"), transaction_id="small", note="test"
    )
    assert parent.credit_cash_balance == Decimal("-0.40")
    with pytest.raises(ledger.InsufficientCredits):
        ledger.debit_refund(
            db_session,
            transaction_id="small",
            credits=Decimal("0.01"),
            request_key="bad",
            note="test",
        )
    ledger.record_purchase(
        db_session, user_id=child.id, credits=Decimal("1"), transaction_id="reward", note="test"
    )
    ledger.referral_recharge_bonus(db_session, child.id, "reward", Decimal("1"))
    assert parent.credit_cash_balance == Decimal("-0.25")
    balanced(db_session, parent)
    ledger._post(
        db_session,
        child,
        bucket="cash",
        amount=Decimal("-1.50"),
        reason="referral_clawback",
        actor_type="system",
        idempotency_key="child-negative",
        allow_negative=True,
    )
    assert app_client.get("/me").json()["credit_balance"] == "-0.50"


@pytest.mark.parametrize("cash", ["-0.01", "0.01"])
def test_ops_purge_nonzero_cash(app_client: TestClient, db_session: Session, cash: str) -> None:
    user = seed_user(db_session, TEST_USER_ID)
    ledger._post(
        db_session,
        user,
        bucket="cash",
        amount=Decimal(cash),
        reason="referral_clawback",
        actor_type="system",
        idempotency_key="cash",
        allow_negative=True,
    )
    for url, params in [
        ("/admin/users/by-email", {"email": user.email, "confirm": user.email}),
        (f"/admin/users/{user.id}", {"confirm": user.email}),
    ]:
        response = app_client.delete(url, params=params, headers=_headers())
        assert (
            response.status_code == 409
            and response.json()["detail"]
            == "user has a non-zero cash balance; settle it to zero first"
        )
    assert db_session.get(User, user.id) is user


def test_self_deletion_negative_and_positive_and_pointers(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent, child = pair(db_session)
    parent.invited_by = child.id
    parent.grand_invited_by = child.id
    db_session.flush()
    ledger._post(
        db_session,
        child,
        bucket="cash",
        amount=Decimal("-0.01"),
        reason="referral_clawback",
        actor_type="system",
        idempotency_key="cash",
        allow_negative=True,
    )
    delete = MagicMock()
    monkeypatch.setattr("app.routers.me.delete_auth_user", delete)
    response = app_client.post(
        "/me/account-deletion", json={"confirm_email": child.email, "relinquish_cash": "-0.01"}
    )
    assert (
        response.status_code == 409
        and response.json()["detail"]
        == "account has a negative balance; contact info@portfonia.com"
    )
    assert db_session.get(User, child.id) is child and not delete.called
    ledger.adjust_by_admin(
        db_session,
        user_id=child.id,
        amount=Decimal("1.01"),
        bucket="cash",
        note="settled",
        idempotency_key="settle",
    )
    monkeypatch.setattr("app.routers.me.verify_account_deletion_solution", lambda _: True)
    assert (
        app_client.post(
            "/me/account-deletion",
            json={"confirm_email": child.email, "relinquish_cash": "1.00", "altcha": "mock"},
        ).status_code
        == 204
    )
    assert rows(db_session, "relinquish")[0].amount == Decimal("-1")
    db_session.refresh(parent)
    assert parent.invited_by is None and parent.grand_invited_by is None
    assert delete.call_count == 1


def test_ops_waitlist_views(app_client: TestClient, db_session: Session) -> None:
    parent, _ = pair(db_session)
    entry = WaitlistEntry(
        email="entry@example.com", locale="en", source="referral", referrer_user_id=parent.id
    )
    db_session.add(entry)
    db_session.flush()
    for url in [
        "/admin/waitlist",
        f"/admin/waitlist/{entry.id}",
        "/admin/waitlist/by-email?email=entry@example.com",
    ]:
        response = app_client.get(url, headers=_headers())
        assert response.status_code == 200
        result = response.json()
        state = result[0] if isinstance(result, list) else result
        assert state["source"] == "referral" and state["referrer_email"] == parent.email
    purge_user(db_session, parent.id)
    assert (
        app_client.get(f"/admin/waitlist/{entry.id}", headers=_headers()).json()["referrer_email"]
        is None
    )


def test_purged_referrer_refund_alert_and_rejected_no_reversal(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent, child = pair(db_session)
    ledger.record_purchase(
        db_session, user_id=child.id, credits=Decimal("10"), transaction_id="txn_T", note="test"
    )
    ledger.referral_recharge_bonus(db_session, child.id, "txn_T", Decimal("10"))
    ledger.consume_credits(
        db_session, user_id=parent.id, amount=Decimal("1.50"), reason="qa", idempotency_key="spent"
    )
    purge_user(db_session, parent.id)
    db_session.commit()
    monkeypatch.setattr(
        "app.routers.admin.get_transaction",
        lambda _: {
            "status": "completed",
            "currency_code": "USD",
            "details": {"line_items": [{"id": "item", "totals": {"total": "1000"}}]},
        },
    )
    monkeypatch.setattr(
        "app.routers.admin.create_refund_adjustment",
        lambda **_: {"id": "adj_T", "status": "pending_approval"},
    )
    notice = MagicMock()
    monkeypatch.setattr("app.tasks.admin_tasks.send_admin_alert_task.delay", notice)
    body = {"transaction_id": "txn_T", "credits": "3", "note": "request", "idempotency_key": "one"}
    for _ in range(2):
        response = app_client.post("/admin/payments/refunds", json=body, headers=_headers())
        assert response.status_code == 200, response.text
    assert len(rows(db_session, "refund")) == 1 and rows(db_session, "refund")[0].amount == Decimal(
        "-3"
    )
    assert rows(db_session, "referral_clawback") == []
    assert notice.call_count == 1
    assert notice.call_args.args[0] == "Portfonia referral: reward not recovered after refund"
    for value in [
        child.email,
        "txn_T",
        "3.00",
        "0.45",
        "The referrer's account was deleted, so nothing was clawed back.",
    ]:
        assert value in notice.call_args.args[1]
    assert notice.call_args.kwargs == {
        "severity": "INFO",
        "idempotency_key": "referral-clawback-skipped:txn_T:one",
    }
    monkeypatch.setattr("app.routers.paddle_webhooks.get_settings", _settings)
    raw, headers = _signed(
        {"event_type": "adjustment.updated", "data": {"id": "adj_T", "status": "rejected"}}
    )
    assert app_client.post("/webhooks/paddle", content=raw, headers=headers).status_code == 200
    assert rows(db_session, "referral_clawback") == []
    assert child.credit_cash_balance == Decimal("10")


def test_admin_alert_task_preserves_idempotency(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tasks.admin_tasks import send_admin_alert_task

    alert = MagicMock()
    monkeypatch.setattr("app.tasks.admin_tasks.send_ops_alert", alert)
    send_admin_alert_task.run("subject", "body", severity="INFO", idempotency_key="referral-test")
    alert.assert_called_once_with(
        "subject", "body", severity="INFO", idempotency_key="referral-test"
    )


def test_ops_zero_cash_purge_clears_attribution(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent, child = pair(db_session)
    child.grand_invited_by = parent.id
    db_session.flush()
    auth = MagicMock(return_value=True)
    monkeypatch.setattr("app.routers.admin.delete_auth_user", auth)
    response = app_client.delete(
        "/admin/users/by-email",
        params={"email": parent.email, "confirm": parent.email},
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["deleted"]["users_invited_by_cleared"] == 1
    assert response.json()["deleted"]["users_grand_invited_by_cleared"] == 1
    db_session.refresh(child)
    assert child.invited_by is None and child.grand_invited_by is None
    assert db_session.get(User, parent.id) is None
    assert auth.call_count == 1


def test_refund_routes_clawback_and_signed_rejection(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent, child = pair(db_session)
    ledger.record_purchase(
        db_session, user_id=child.id, credits=Decimal("10"), transaction_id="txn_T", note="test"
    )
    ledger.referral_recharge_bonus(db_session, child.id, "txn_T", Decimal("10"))
    ledger.consume_credits(
        db_session, user_id=parent.id, amount=Decimal("1.50"), reason="qa", idempotency_key="spent"
    )
    monkeypatch.setattr(
        "app.routers.admin.get_transaction",
        lambda _: {
            "status": "completed",
            "currency_code": "USD",
            "details": {"line_items": [{"id": "item", "totals": {"total": "1000"}}]},
        },
    )
    monkeypatch.setattr(
        "app.routers.admin.create_refund_adjustment",
        MagicMock(
            side_effect=[
                {"id": "adj_one", "status": "pending_approval"},
                {"id": "adj_two", "status": "pending_approval"},
            ]
        ),
    )
    for key, amount in [("one", "3"), ("two", "7")]:
        response = app_client.post(
            "/admin/payments/refunds",
            json={
                "transaction_id": "txn_T",
                "credits": amount,
                "note": "request",
                "idempotency_key": key,
            },
            headers=_headers(),
        )
        assert response.status_code == 200, response.text
    assert [r.amount for r in rows(db_session, "referral_clawback")] == [
        Decimal("-0.45"),
        Decimal("-1.05"),
    ]
    assert parent.credit_cash_balance == Decimal("-1.50")
    monkeypatch.setattr("app.routers.paddle_webhooks.get_settings", _settings)
    raw, headers = _signed(
        {"event_type": "adjustment.updated", "data": {"id": "adj_two", "status": "rejected"}}
    )
    for _ in range(2):
        assert app_client.post("/webhooks/paddle", content=raw, headers=headers).status_code == 200
    assert [r.amount for r in rows(db_session, "referral_clawback")] == [
        Decimal("-0.45"),
        Decimal("-1.05"),
        Decimal("1.05"),
    ]
    assert parent.credit_cash_balance == Decimal("-0.45")
    assert child.credit_cash_balance == Decimal("7")
    balanced(db_session, parent)
    balanced(db_session, child)
