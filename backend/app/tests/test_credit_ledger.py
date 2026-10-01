"""Credit ledger write-path contract against real Postgres."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier, Event

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_engine
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


@pytest.mark.parametrize(
    "initial_grant,amount,expected_balance,second_rejected",
    [
        (False, Decimal("10.00"), Decimal("20.00"), False),
        (True, Decimal("-5.00"), Decimal("0.00"), True),
    ],
)
def test_row_lock_refreshes_preloaded_user_across_connections(
    session_test_db: None,
    request: pytest.FixtureRequest,
    initial_grant: bool,
    amount: Decimal,
    expected_balance: Decimal,
    second_rejected: bool,
) -> None:
    """Two live connections preload the same balance before serialized writes."""
    engine = get_engine()
    user_id = uuid.uuid4()
    with Session(engine) as setup:
        user = seed_user(setup, user_id)
        if initial_grant:
            grant_signup_credits(setup, user)
        setup.commit()

    def cleanup() -> None:
        # These independent commits are outside db_session's rollback fixture.
        with Session(engine) as session:
            session.execute(delete(CreditLedgerEntry).where(CreditLedgerEntry.user_id == user_id))
            session.execute(delete(User).where(User.id == user_id))
            session.commit()

    request.addfinalizer(cleanup)

    both_loaded = Barrier(2)
    first_written = Event()
    second_attempted = Event()
    release_first = Event()

    def first_writer() -> None:
        with Session(engine) as session:
            preloaded = session.get(User, user_id)
            assert preloaded is not None
            both_loaded.wait(timeout=10)
            adjust_by_admin(
                session,
                user_id=user_id,
                amount=amount,
                note="first",
                idempotency_key=f"{user_id}:first",
            )
            first_written.set()
            assert release_first.wait(timeout=10)
            session.commit()

    def second_writer() -> bool:
        with Session(engine) as session:
            preloaded = session.get(User, user_id)
            assert preloaded is not None
            both_loaded.wait(timeout=10)
            assert first_written.wait(timeout=10)
            second_attempted.set()
            try:
                adjust_by_admin(
                    session,
                    user_id=user_id,
                    amount=amount,
                    note="second",
                    idempotency_key=f"{user_id}:second",
                )
            except InsufficientCredits:
                session.rollback()
                return True
            session.commit()
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_writer)
        second = pool.submit(second_writer)
        try:
            assert second_attempted.wait(timeout=10)
        finally:
            release_first.set()
        first.result(timeout=10)
        assert second.result(timeout=10) is second_rejected

    with Session(engine) as check:
        checked_user = check.get(User, user_id)
        assert checked_user is not None
        assert_balanced(check, checked_user)
        assert checked_user.credit_gift_balance == expected_balance
        rows = (
            check.execute(
                select(CreditLedgerEntry)
                .where(CreditLedgerEntry.user_id == user_id)
                .order_by(CreditLedgerEntry.id)
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert rows[-1].balance_after == expected_balance


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
    before_replay = len(
        db_session.execute(
            select(CreditLedgerEntry.id).where(CreditLedgerEntry.user_id == user.id)
        ).all()
    )
    replay = consume_credits(
        db_session, user_id=user.id, amount=Decimal("14.00"), reason="qa", idempotency_key="qa:abc"
    )
    assert replay.replayed is True
    assert [entry.id for entry in replay.entries] == [entry.id for entry in result.entries]
    assert (
        len(
            db_session.execute(
                select(CreditLedgerEntry.id).where(CreditLedgerEntry.user_id == user.id)
            ).all()
        )
        == before_replay
    )
    assert user.credit_gift_balance == Decimal("0.00")
    assert user.credit_cash_balance == Decimal("18.00")
    with pytest.raises(InsufficientCredits):
        consume_credits(
            db_session,
            user_id=user.id,
            amount=Decimal("18.01"),
            reason="qa",
            idempotency_key="qa:short",
        )
    assert (
        db_session.execute(
            select(CreditLedgerEntry).where(CreditLedgerEntry.idempotency_key == "qa:short")
        )
        .scalars()
        .all()
        == []
    )
    assert (
        len(
            db_session.execute(
                select(CreditLedgerEntry.id).where(CreditLedgerEntry.user_id == user.id)
            ).all()
        )
        == before_replay
    )
    assert user.credit_gift_balance == Decimal("0.00")
    assert user.credit_cash_balance == Decimal("18.00")
    cash_only = consume_credits(
        db_session,
        user_id=user.id,
        amount=Decimal("1.00"),
        reason="qa",
        idempotency_key="qa:cash",
    )
    assert len(cash_only.entries) == 1
    assert cash_only.entries[0].bucket == "cash"
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
    assert first.json()["replayed"] is False
    assert first.json()["entry"]["amount"] == "10.00"
    assert first.json()["entry"]["balance_after"] == "15.00"
    replay = app_client.post(url, json=body, headers=headers)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["entry"]["id"] == first.json()["entry"]["id"]
    assert replay.json()["gift_balance"] == "15.00"
    row_ids = (
        db_session.execute(select(CreditLedgerEntry.id).where(CreditLedgerEntry.user_id == user.id))
        .scalars()
        .all()
    )
    assert len(row_ids) == 2
    assert (
        app_client.post(url, json={**body, "amount": "20.00"}, headers=headers).status_code == 409
    )
    db_session.refresh(user)
    assert user.credit_gift_balance == Decimal("15.00")
    assert user.credit_cash_balance == Decimal("0.00")
    assert (
        db_session.execute(select(CreditLedgerEntry.id).where(CreditLedgerEntry.user_id == user.id))
        .scalars()
        .all()
        == row_ids
    )
    short = app_client.post(
        url, json={**body, "amount": "-20.00", "idempotency_key": "two"}, headers=headers
    )
    assert short.status_code == 409 and short.json()["detail"] == "insufficient balance"
    db_session.refresh(user)
    assert user.credit_gift_balance == Decimal("15.00")
    assert user.credit_cash_balance == Decimal("0.00")
    assert (
        db_session.execute(select(CreditLedgerEntry.id).where(CreditLedgerEntry.user_id == user.id))
        .scalars()
        .all()
        == row_ids
    )
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
    assert old_user.credit_cash_balance == Decimal("0.00")
    old_id = old_user.id
    rows = (
        db_session.execute(select(CreditLedgerEntry).where(CreditLedgerEntry.user_id == old_id))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].amount == Decimal("5.00")
    assert rows[0].balance_after == Decimal("5.00")
    assert rows[0].reason == "signup_grant"
    assert rows[0].actor_type == "system"
    assert rows[0].idempotency_key == signup_grant_key("alice@example.com")
    total_before_purge = len(db_session.execute(select(CreditLedgerEntry.id)).all())
    purge = app_client.delete(
        "/admin/users/by-email",
        params={"email": "alice@example.com", "confirm": "alice@example.com"},
        headers=headers,
    )
    assert purge.status_code == 200, purge.text
    assert purge.json()["deleted"]["credit_ledger_flagged"] == 1
    db_session.expire_all()
    assert db_session.get(User, old_id) is None
    assert len(db_session.execute(select(CreditLedgerEntry.id)).all()) == total_before_purge
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
