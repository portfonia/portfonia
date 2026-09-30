"""Subscription core contract against real Postgres (issue #595)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.credit_ledger import CreditLedgerEntry
from app.models.user import User
from app.services import subscription
from app.services.credit_ledger import (
    IdempotencyConflict,
    adjust_by_admin,
    record_purchase,
    return_subscription_credits,
)
from app.tests.conftest import seed_user
from app.tests.test_credit_ledger import assert_balanced


@pytest.mark.parametrize(
    "start,anchor,expected",
    [
        (date(2026, 10, 15), 15, date(2026, 11, 15)),
        (date(2027, 1, 31), 31, date(2027, 2, 28)),
        (date(2027, 2, 28), 31, date(2027, 3, 31)),
        (date(2028, 1, 30), 30, date(2028, 2, 29)),
    ],
)
def test_next_expiry(start: date, anchor: int, expected: date) -> None:
    assert subscription.next_expiry(start, anchor) == expected


def test_subscription_defaults(db_session: Session) -> None:
    user = seed_user(db_session, uuid.uuid4())
    assert user.subscription_status == "inactive"
    assert user.subscription_type is None
    assert user.subscription_expires_on is None
    assert user.subscription_period_start is None
    assert user.subscription_anchor_day is None
    assert user.subscription_adjusted_on is None
    assert user.subscription_cancel_pending is False


def test_return_credits_replay_conflict_and_zero(db_session: Session) -> None:
    user = seed_user(db_session, uuid.uuid4())
    key = f"subscription_return:{user.id}"
    result = return_subscription_credits(
        db_session,
        user_id=user.id,
        cash=Decimal("1.49"),
        gift=Decimal("0.44"),
        idempotency_key=key,
        reference="old-charge",
    )
    assert [
        (r.bucket, r.amount, r.reason, r.idempotency_key, r.actor_type, r.reference)
        for r in result.entries
    ] == [
        ("cash", Decimal("1.49"), "subscription_return", key, "system", "old-charge"),
        ("gift", Decimal("0.44"), "subscription_return", key, "system", "old-charge"),
    ]
    replay = return_subscription_credits(
        db_session,
        user_id=user.id,
        cash=Decimal("1.49"),
        gift=Decimal("0.44"),
        idempotency_key=key,
        reference="old-charge",
    )
    assert replay.replayed and [r.id for r in replay.entries] == [r.id for r in result.entries]
    with pytest.raises(IdempotencyConflict):
        return_subscription_credits(
            db_session,
            user_id=user.id,
            cash=Decimal("1.50"),
            gift=Decimal("0.44"),
            idempotency_key=key,
            reference="old-charge",
        )
    other = seed_user(db_session, uuid.uuid4())
    with pytest.raises(IdempotencyConflict):
        return_subscription_credits(
            db_session,
            user_id=other.id,
            cash=Decimal("1.49"),
            gift=Decimal("0.44"),
            idempotency_key=key,
            reference="old-charge",
        )
    zero = return_subscription_credits(
        db_session,
        user_id=user.id,
        cash=Decimal("0"),
        gift=Decimal("0"),
        idempotency_key=key + ":zero",
        reference="old-charge",
    )
    assert zero.entries == []
    assert (
        len(
            db_session.scalars(
                select(CreditLedgerEntry).where(CreditLedgerEntry.user_id == user.id)
            ).all()
        )
        == 2
    )
    assert_balanced(db_session, user)


def funded_user(session: Session, gift: str = "5.00", cash: str = "0.00") -> User:
    user = seed_user(session, uuid.uuid4())
    user.email_verified_at = datetime(2026, 10, 15, tzinfo=ET)
    session.flush()
    if Decimal(gift):
        adjust_by_admin(
            session,
            user_id=user.id,
            amount=Decimal(gift),
            note="test grant",
            idempotency_key=f"seed:{user.id}",
        )
    if Decimal(cash):
        record_purchase(
            session,
            user_id=user.id,
            credits=Decimal(cash),
            transaction_id=f"seed:{user.id}",
            note="test purchase",
        )
    return user


def subscription_rows(session: Session, user: User) -> list[tuple[str, Decimal, str, str]]:
    return [
        (r.bucket, r.amount, r.reason, r.idempotency_key)
        for r in session.scalars(
            select(CreditLedgerEntry)
            .where(
                CreditLedgerEntry.user_id == user.id,
                CreditLedgerEntry.reason.in_(("subscription", "subscription_return")),
            )
            .order_by(CreditLedgerEntry.id)
        )
    ]


def assert_period(user: User, plan: str, start: date, end: date) -> None:
    assert user.subscription_status == "active"
    assert user.subscription_type == user.report_cadence == plan
    assert user.subscription_period_start == start
    assert user.subscription_expires_on == end
    assert user.subscription_anchor_day == start.day
    assert user.subscription_adjusted_on == start
    assert not user.subscription_cancel_pending


def test_worked_examples_subscribe_and_change(db_session: Session) -> None:
    user = funded_user(db_session)
    subscription.set_plan(db_session, user.id, date(2026, 10, 15), "weekly")
    assert_period(user, "weekly", date(2026, 10, 15), date(2026, 11, 15))
    old_key = subscription.charge_key(user.id, date(2026, 10, 15), "weekly")
    assert subscription_rows(db_session, user) == [
        ("gift", Decimal("-0.99"), "subscription", old_key)
    ]
    assert user.credit_gift_balance == Decimal("4.01")
    subscription.set_plan(db_session, user.id, date(2026, 10, 31), "mwf")
    assert_period(user, "mwf", date(2026, 10, 31), date(2026, 11, 30))
    assert subscription_rows(db_session, user)[1:] == [
        ("gift", Decimal("0.50"), "subscription_return", f"subscription_return:{old_key}"),
        (
            "gift",
            Decimal("-1.99"),
            "subscription",
            subscription.charge_key(user.id, date(2026, 10, 31), "mwf"),
        ),
    ]
    assert user.credit_gift_balance == Decimal("2.52")
    assert_balanced(db_session, user)


@pytest.mark.parametrize("day,cash_return,gift_return", [(31, "1.00", "0"), (16, "1.49", "0.44")])
def test_worked_example_cash_first_return(
    db_session: Session, day: int, cash_return: str, gift_return: str
) -> None:
    user = funded_user(db_session, "0.50", "10.00")
    subscription.set_plan(db_session, user.id, date(2026, 10, 15), "mwf")
    key = subscription.charge_key(user.id, date(2026, 10, 15), "mwf")
    assert subscription_rows(db_session, user) == [
        ("gift", Decimal("-0.50"), "subscription", key),
        ("cash", Decimal("-1.49"), "subscription", key),
    ]
    subscription.set_plan(db_session, user.id, date(2026, 10, day), "weekly")
    rows = subscription_rows(db_session, user)[2:]
    expected = [("cash", Decimal(cash_return), "subscription_return", f"subscription_return:{key}")]
    if Decimal(gift_return):
        expected.append(
            ("gift", Decimal(gift_return), "subscription_return", f"subscription_return:{key}")
        )
    new_key = subscription.charge_key(user.id, date(2026, 10, day), "weekly")
    if Decimal(gift_return):
        expected.append(("gift", -Decimal(gift_return), "subscription", new_key))
    expected.append(("cash", -(Decimal("0.99") - Decimal(gift_return)), "subscription", new_key))
    assert rows == expected
    assert_period(user, "weekly", date(2026, 10, day), date(2026, 11, min(day, 30)))
    assert_balanced(db_session, user)


def test_worked_example_cancel_resume_and_daily_limit(db_session: Session) -> None:
    user = funded_user(db_session)
    subscription.set_plan(db_session, user.id, date(2026, 10, 15), "weekly")
    before = subscription_rows(db_session, user)
    subscription.cancel(db_session, user.id, date(2026, 10, 20))
    assert user.subscription_cancel_pending and user.subscription_status == "active"
    assert user.subscription_type == user.report_cadence == "weekly"
    assert user.subscription_expires_on == date(2026, 11, 15)
    with pytest.raises(subscription.SubscriptionError, match="daily_limit"):
        subscription.resume(db_session, user.id, date(2026, 10, 20))
    subscription.resume(db_session, user.id, date(2026, 10, 21))
    assert not user.subscription_cancel_pending
    assert user.subscription_adjusted_on == date(2026, 10, 21)
    assert subscription_rows(db_session, user) == before


def test_worked_example_overdue_change_no_return(db_session: Session) -> None:
    user = funded_user(db_session)
    subscription.set_plan(db_session, user.id, date(2026, 10, 15), "weekly")
    subscription.set_plan(db_session, user.id, date(2026, 11, 17), "mwf")
    assert_period(user, "mwf", date(2026, 11, 17), date(2026, 12, 17))
    assert len(subscription_rows(db_session, user)) == 2
    assert all(r[2] == "subscription" for r in subscription_rows(db_session, user))
    assert user.credit_gift_balance == Decimal("2.02")


@pytest.mark.parametrize("change", [False, True])
def test_insufficient_balance_leaves_state_and_ledger_unchanged(
    db_session: Session, change: bool
) -> None:
    user = funded_user(db_session, "0.98" if not change else "2.47")
    if change:
        subscription.set_plan(db_session, user.id, date(2026, 10, 15), "weekly")
    before = subscription_rows(db_session, user)
    state = (
        user.subscription_status,
        user.subscription_type,
        user.subscription_adjusted_on,
        user.credit_gift_balance,
    )
    with pytest.raises(subscription.SubscriptionError, match="insufficient_credits"):
        subscription.set_plan(
            db_session, user.id, date(2026, 10, 31), "mwf" if change else "weekly"
        )
    db_session.refresh(user)
    assert subscription_rows(db_session, user) == before
    assert (
        user.subscription_status,
        user.subscription_type,
        user.subscription_adjusted_on,
        user.credit_gift_balance,
    ) == state


def test_same_plan_and_email_preconditions(db_session: Session) -> None:
    user = funded_user(db_session)
    user.email_verified_at = None
    db_session.flush()
    with pytest.raises(subscription.SubscriptionError, match="email_unverified"):
        subscription.set_plan(db_session, user.id, date(2026, 10, 15), "weekly")
    user.email_verified_at = datetime(2026, 10, 15, tzinfo=ET)
    db_session.flush()
    subscription.set_plan(db_session, user.id, date(2026, 10, 15), "weekly")
    for day in (date(2026, 10, 16), date(2026, 11, 17)):
        with pytest.raises(subscription.SubscriptionError, match="no_change"):
            subscription.set_plan(db_session, user.id, day, "weekly")
    subscription.cancel(db_session, user.id, date(2026, 10, 17))
    user.email_verified_at = None
    db_session.flush()
    with pytest.raises(subscription.SubscriptionError, match="email_unverified"):
        subscription.resume(db_session, user.id, date(2026, 10, 18))
    with pytest.raises(subscription.SubscriptionError, match="no_subscription"):
        subscription.resume(db_session, user.id, date(2026, 11, 17))


def test_selecting_original_plan_resumes_without_charge(db_session: Session) -> None:
    user = funded_user(db_session)
    subscription.set_plan(db_session, user.id, date(2026, 10, 15), "weekly")
    subscription.cancel(db_session, user.id, date(2026, 10, 16))
    before = subscription_rows(db_session, user)
    subscription.set_plan(db_session, user.id, date(2026, 11, 15), "weekly")
    assert not user.subscription_cancel_pending
    assert subscription_rows(db_session, user) == before
    assert user.subscription_expires_on == date(2026, 11, 15)


def test_concurrent_adjustments_refresh_daily_lock(
    session_test_db: None,
    request: pytest.FixtureRequest,
) -> None:
    """Two independent sessions preload state; only one daily adjustment commits."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event

    from sqlalchemy import delete

    from app.core.database import get_engine

    engine = get_engine()
    with Session(engine) as setup:
        user = funded_user(setup)
        uid = user.id
        subscription.set_plan(setup, uid, date(2026, 10, 15), "weekly")
        setup.commit()

    def cleanup() -> None:
        with Session(engine) as session:
            session.execute(delete(CreditLedgerEntry).where(CreditLedgerEntry.user_id == uid))
            session.execute(delete(User).where(User.id == uid))
            session.commit()

    request.addfinalizer(cleanup)
    loaded = Barrier(2)
    written, attempted, release = Event(), Event(), Event()

    def first() -> None:
        with Session(engine) as session:
            assert session.get(User, uid) is not None
            loaded.wait(timeout=10)
            subscription.cancel(session, uid, date(2026, 10, 20))
            written.set()
            assert release.wait(timeout=10)
            session.commit()

    def second() -> str:
        with Session(engine) as session:
            assert session.get(User, uid) is not None
            loaded.wait(timeout=10)
            assert written.wait(timeout=10)
            attempted.set()
            try:
                subscription.set_plan(session, uid, date(2026, 10, 20), "mwf")
            except subscription.SubscriptionError as exc:
                session.rollback()
                return exc.code
            session.commit()
            return "unexpected success"

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(first), pool.submit(second)
        try:
            assert attempted.wait(timeout=10)
        finally:
            release.set()
        a.result(timeout=10)
        assert b.result(timeout=10) == "daily_limit"
    with Session(engine) as check:
        checked_user = check.get(User, uid)
        assert checked_user is not None and checked_user.subscription_cancel_pending
        assert checked_user.subscription_adjusted_on == date(2026, 10, 20)
        assert len(subscription_rows(check, checked_user)) == 1
        assert_balanced(check, checked_user)
