"""Credit ledger write-path contract against real Postgres."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.credit_ledger import CreditLedgerEntry
from app.models.user import User
from app.scripts.backfill_signup_grants import backfill_signup_grants
from app.services.credit_ledger import (
    IdempotencyConflict,
    InsufficientCredits,
    _post,
    adjust_by_admin,
    consume_credits,
    grant_signup_credits,
    signup_grant_key,
)
from app.services.invites import create_invite
from app.tests.conftest import seed_user


def assert_balanced(session: Session, user: User) -> None:
    session.flush()
    session.refresh(user)
    for bucket in ("cash", "gift"):
        rows = (
            session.execute(
                select(CreditLedgerEntry)
                .where(CreditLedgerEntry.user_id == user.id, CreditLedgerEntry.bucket == bucket)
                .order_by(CreditLedgerEntry.id)
            )
            .scalars()
            .all()
        )
        balance = getattr(user, f"credit_{bucket}_balance")
        assert balance == sum((row.amount for row in rows), Decimal("0.00"))
        assert balance == (rows[-1].balance_after if rows else Decimal("0.00"))
        assert balance >= 0


def test_grant_once_and_zero_setting(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    user = seed_user(db_session, uuid.uuid4(), " Alice@Example.com ")
    row = grant_signup_credits(db_session, user)
    assert row is not None
    assert row.amount == Decimal("5.00")
    assert row.bucket == "gift"
    assert row.balance_after == Decimal("5.00")
    assert row.idempotency_key == signup_grant_key("alice@example.com")
    assert grant_signup_credits(db_session, user) is None
    assert_balanced(db_session, user)

    zero_user = seed_user(db_session, uuid.uuid4(), "zero@example.com")
    monkeypatch.setattr(
        "app.services.credit_ledger.get_settings",
        lambda: type("S", (), {"SIGNUP_GRANT_CREDITS": Decimal("0")})(),
    )
    assert grant_signup_credits(db_session, zero_user) is None
    assert_balanced(db_session, zero_user)


def test_admin_replay_conflict_and_overdraft(db_session: Session) -> None:
    user = seed_user(db_session, uuid.uuid4())
    grant_signup_credits(db_session, user)
    first = adjust_by_admin(
        db_session, user_id=user.id, amount=Decimal("10.00"), note="thanks", idempotency_key="one"
    )
    assert first.replayed is False
    assert first.entries[0].balance_after == Decimal("15.00")
    again = adjust_by_admin(
        db_session, user_id=user.id, amount=Decimal("10.00"), note="thanks", idempotency_key="one"
    )
    assert again.replayed is True
    assert again.entries[0].id == first.entries[0].id
    with pytest.raises(IdempotencyConflict):
        adjust_by_admin(
            db_session,
            user_id=user.id,
            amount=Decimal("20.00"),
            note="thanks",
            idempotency_key="one",
        )
    with pytest.raises(InsufficientCredits):
        adjust_by_admin(
            db_session,
            user_id=user.id,
            amount=Decimal("-20.00"),
            note="reversal",
            idempotency_key="two",
        )
    adjust_by_admin(
        db_session,
        user_id=user.id,
        amount=Decimal("-3.00"),
        note="reversal",
        idempotency_key="three",
    )
    assert_balanced(db_session, user)
    assert user.credit_gift_balance == Decimal("12.00")
    assert user.credit_cash_balance == Decimal("0.00")


def test_consumption_split_and_single_bucket(db_session: Session) -> None:
    user = seed_user(db_session, uuid.uuid4())
    grant_signup_credits(db_session, user)
    adjust_by_admin(
        db_session, user_id=user.id, amount=Decimal("7.00"), note="grant", idempotency_key="seed"
    )
    _post(
        db_session,
        user,
        bucket="cash",
        amount=Decimal("20.00"),
        reason="recharge",
        actor_type="system",
        idempotency_key="recharge:seed",
    )
    result = consume_credits(
        db_session,
        user_id=user.id,
        amount=Decimal("14.00"),
        reason="qa",
        idempotency_key="qa:abc",
        reference="qa:abc",
    )
    assert [(r.bucket, r.amount, r.balance_after) for r in result.entries] == [
        ("gift", Decimal("-12.00"), Decimal("0.00")),
        ("cash", Decimal("-2.00"), Decimal("18.00")),
    ]
    assert consume_credits(
        db_session, user_id=user.id, amount=Decimal("14.00"), reason="qa", idempotency_key="qa:abc"
    ).replayed
    with pytest.raises(InsufficientCredits):
        consume_credits(
            db_session,
            user_id=user.id,
            amount=Decimal("18.01"),
            reason="qa",
            idempotency_key="qa:short",
        )
    assert (
        consume_credits(
            db_session,
            user_id=user.id,
            amount=Decimal("1.00"),
            reason="qa",
            idempotency_key="qa:cash",
        )
        .entries[0]
        .bucket
        == "cash"
    )
    adjust_by_admin(
        db_session, user_id=user.id, amount=Decimal("2.00"), note="grant", idempotency_key="gift"
    )
    assert (
        len(
            consume_credits(
                db_session,
                user_id=user.id,
                amount=Decimal("1.00"),
                reason="qa",
                idempotency_key="qa:gift",
            ).entries
        )
        == 1
    )
    assert_balanced(db_session, user)


@pytest.mark.parametrize(
    "bucket,amount,reason",
    [
        ("cash", "1.00", "signup_grant"),
        ("cash", "-1.00", "recharge"),
        ("gift", "1.00", "qa"),
        ("gift", "1.001", "signup_grant"),
        ("gift", "0", "signup_grant"),
    ],
)
def test_post_rejects_rule_violations(
    db_session: Session, bucket: str, amount: str, reason: str
) -> None:
    user = seed_user(db_session, uuid.uuid4())
    with pytest.raises(ValueError):
        _post(
            db_session,
            user,
            bucket=bucket,
            amount=Decimal(amount),
            reason=reason,
            actor_type="system",
            idempotency_key=f"test:{uuid.uuid4()}",
        )
    assert_balanced(db_session, user)


def test_admin_endpoint_contract(app_client: TestClient, db_session: Session) -> None:
    user = seed_user(db_session, uuid.uuid4(), "bob@example.com")
    grant_signup_credits(db_session, user)
    url = "/admin/users/by-email/credit-adjustments"
    headers = {"Authorization": f"Bearer {get_settings().ADMIN_API_TOKEN.get_secret_value()}"}
    body = {
        "email": " Bob@Example.com ",
        "amount": "10.00",
        "note": " beta tester thanks ",
        "idempotency_key": "2026-09-27-bob-1",
    }
    assert app_client.post(url, json=body).status_code == 401
    first = app_client.post(url, json=body, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["gift_balance"] == "15.00"
    assert first.json()["cash_balance"] == "0.00"
    assert first.json()["entry"]["amount"] == "10.00"
    replay = app_client.post(url, json=body, headers=headers)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["entry"]["id"] == first.json()["entry"]["id"]
    assert (
        app_client.post(url, json={**body, "amount": "20.00"}, headers=headers).status_code == 409
    )
    short = app_client.post(
        url, json={**body, "amount": "-20.00", "idempotency_key": "two"}, headers=headers
    )
    assert short.status_code == 409 and short.json()["detail"] == "insufficient gift balance"
    for bad in ({"amount": "0"}, {"amount": "1.001"}, {"note": "   "}, {"idempotency_key": "   "}):
        assert app_client.post(url, json={**body, **bad}, headers=headers).status_code == 422
    assert (
        app_client.post(
            url, json={**body, "email": "missing@example.com"}, headers=headers
        ).status_code
        == 404
    )
    assert_balanced(db_session, user)


def test_signup_purge_and_resignup(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.routers.auth.create_auth_user", lambda *_: str(uuid.uuid4()))
    monkeypatch.setattr("app.routers.auth.delete_auth_user", lambda *_: True)
    monkeypatch.setattr("app.routers.admin.delete_auth_user", lambda *_: True)
    headers = {"Authorization": f"Bearer {get_settings().ADMIN_API_TOKEN.get_secret_value()}"}

    def signup(email: str) -> User:
        invite = create_invite(db_session, created_by=uuid.uuid4())
        db_session.flush()
        response = app_client.post(
            "/auth/signup",
            json={
                "invite_token": invite.token,
                "email": email,
                "password": "a-long-enough-password",
                "tos_accepted": True,
            },
        )
        assert response.status_code == 201, response.text
        user = db_session.get(User, uuid.UUID(response.json()["id"]))
        assert user is not None
        return user

    old_user = signup("alice@example.com")
    assert old_user is not None
    assert_balanced(db_session, old_user)
    assert old_user.credit_gift_balance == Decimal("5.00")
    old_id = old_user.id
    rows = (
        db_session.execute(select(CreditLedgerEntry).where(CreditLedgerEntry.user_id == old_id))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    purge = app_client.delete(
        "/admin/users/by-email",
        params={"email": "alice@example.com", "confirm": "alice@example.com"},
        headers=headers,
    )
    assert purge.status_code == 200, purge.text
    assert purge.json()["deleted"]["credit_ledger_flagged"] == 1
    db_session.expire_all()
    assert db_session.get(User, old_id) is None
    flagged = db_session.get(CreditLedgerEntry, rows[0].id)
    assert flagged is not None and flagged.user_deleted_at is not None
    new_user = signup(" Alice@Example.com ")
    assert new_user is not None
    assert_balanced(db_session, new_user)
    assert new_user.credit_gift_balance == Decimal("0.00")
    assert (
        len(
            db_session.execute(
                select(CreditLedgerEntry).where(
                    CreditLedgerEntry.idempotency_key == signup_grant_key("alice@example.com")
                )
            )
            .scalars()
            .all()
        )
        == 1
    )


def test_signup_with_zero_grant(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.routers.auth.create_auth_user", lambda *_: str(uuid.uuid4()))
    monkeypatch.setattr(
        "app.services.credit_ledger.get_settings",
        lambda: type("S", (), {"SIGNUP_GRANT_CREDITS": Decimal("0")})(),
    )
    invite = create_invite(db_session, created_by=uuid.uuid4())
    db_session.flush()
    response = app_client.post(
        "/auth/signup",
        json={
            "invite_token": invite.token,
            "email": "zero-signup@example.com",
            "password": "a-long-enough-password",
            "tos_accepted": True,
        },
    )
    assert response.status_code == 201, response.text
    user = db_session.get(User, uuid.UUID(response.json()["id"]))
    assert user is not None
    assert_balanced(db_session, user)
    assert user.credit_gift_balance == Decimal("0.00")


def test_backfill_dry_run_apply_and_replay(
    db_session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    users = [seed_user(db_session, uuid.uuid4(), f"backfill-{n}@example.com") for n in range(3)]
    grant_signup_credits(db_session, users[2])
    assert backfill_signup_grants(db_session, apply_changes=False) == 2
    assert "would-grant" in capsys.readouterr().out
    for user in users:
        assert_balanced(db_session, user)
    assert backfill_signup_grants(db_session, apply_changes=True) == 2
    assert backfill_signup_grants(db_session, apply_changes=True) == 0
    for user in users:
        assert_balanced(db_session, user)
    rows = (
        db_session.execute(
            select(CreditLedgerEntry).where(
                CreditLedgerEntry.user_id.in_([u.id for u in users[:2]])
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert all(row.note == "backfill: registered before credit ledger" for row in rows)
