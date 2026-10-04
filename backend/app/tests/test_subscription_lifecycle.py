"""Real-Postgres lifecycle acceptance tests for #596."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.credit_ledger import CreditLedgerEntry
from app.models.user import User
from app.services import subscription as s
from app.services.credit_ledger import adjust_by_admin, consume_credits
from app.services.user_scope import active_user_ids, active_users
from app.tests.conftest import seed_user


@pytest.fixture(autouse=True)
def _due_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.tasks.report_tasks.today_et", lambda: date(2026, 11, 21))


TODAY = date(2026, 11, 21)
OLD = date(2026, 11, 17)


def subscriber(
    db: Session,
    balance: str = "1.49",
    status: str = "active",
    plan: str = "weekly",
    verified: bool = True,
) -> User:
    u = seed_user(db, uuid.uuid4())
    u.email_verified_at = datetime(2026, 10, 17, tzinfo=ET) if verified else None
    u.subscription_status = status
    u.subscription_type = u.report_cadence = plan
    u.subscription_period_start = date(2026, 10, 17)
    u.subscription_expires_on = OLD
    u.subscription_anchor_day = 17
    db.flush()
    adjust_by_admin(
        db, user_id=u.id, amount=Decimal(balance), note="fixture", idempotency_key=str(u.id)
    )
    db.commit()
    return u


def charges(db: Session, u: User) -> list[CreditLedgerEntry]:
    return list(
        db.scalars(
            select(CreditLedgerEntry)
            .where(CreditLedgerEntry.user_id == u.id, CreditLedgerEntry.reason == "subscription")
            .order_by(CreditLedgerEntry.id)
        )
    )


def expected_notice(u: User, balance: str, expiry: date = OLD) -> dict[str, object]:
    return dict(
        locale="zh", plan="weekly", expires_on=expiry, fee=Decimal("0.99"), balance=Decimal(balance)
    )


def test_examples_1_2_7_renewal_and_retry(db_session: Session) -> None:
    u = subscriber(db_session)
    with patch.object(s, "send_subscription_notice") as notice:
        s.run_cadence_checks(db_session, "weekly", date(2026, 11, 14))
        assert not charges(db_session, u)
        notice.assert_not_called()
        s.run_cadence_checks(db_session, "weekly", TODAY)
        assert (
            u.subscription_period_start,
            u.subscription_expires_on,
            u.subscription_anchor_day,
        ) == (OLD, date(2026, 12, 17), 17)
        assert u.credit_gift_balance == Decimal("0.50")
        (row,) = charges(db_session, u)
        assert (row.amount, row.bucket, row.balance_after, row.idempotency_key, row.reference) == (
            Decimal("-0.99"),
            "gift",
            Decimal("0.50"),
            s.charge_key(u.id, OLD, "weekly"),
            "weekly",
        )
        notice.assert_called_once_with(
            u.email, "low_balance", **expected_notice(u, "0.50", date(2026, 12, 17))
        )
        s.run_cadence_checks(db_session, "weekly", TODAY)
        assert len(charges(db_session, u)) == 1
        assert notice.call_count == 1


def test_examples_3_4_5_expire_wait_resume(db_session: Session) -> None:
    u = subscriber(db_session, "0.40")
    with patch.object(s, "send_subscription_notice") as notice:
        s.run_cadence_checks(db_session, "weekly", TODAY)
        assert (
            u.subscription_status,
            u.subscription_type,
            u.report_cadence,
            u.subscription_expires_on,
            u.subscription_anchor_day,
        ) == ("expired", "weekly", "weekly", OLD, 17)
        assert not charges(db_session, u)
        notice.assert_called_once_with(u.email, "expired", **expected_notice(u, "0.40"))
        s.run_cadence_checks(db_session, "weekly", date(2026, 11, 28))
        assert notice.call_count == 1
        assert active_users(db_session, "weekly") == []
        adjust_by_admin(
            db_session,
            user_id=u.id,
            amount=Decimal("5.00"),
            note="top up",
            idempotency_key="resume",
        )
        db_session.commit()
        s.run_cadence_checks(db_session, "weekly", date(2026, 12, 5))
        assert (
            u.subscription_status,
            u.subscription_period_start,
            u.subscription_expires_on,
            u.subscription_anchor_day,
        ) == ("active", date(2026, 12, 5), date(2027, 1, 5), 5)
        assert u.credit_gift_balance == Decimal("4.41")
        (row,) = charges(db_session, u)
        assert row.idempotency_key == s.charge_key(u.id, date(2026, 12, 5), "weekly")
        assert notice.call_count == 1


def test_example_6_and_dispatch_gate(db_session: Session) -> None:
    u = subscriber(db_session)
    u.subscription_cancel_pending = True
    for state in ("inactive", "expired", "cancelled"):
        other = seed_user(db_session, uuid.uuid4())
        other.report_cadence = "weekly"
        other.email_verified_at = datetime(2026, 10, 17, tzinfo=ET)
        other.subscription_status = state
    db_session.commit()
    assert active_user_ids(db_session, "weekly") == [u.id]
    assert [x.id for x in active_users(db_session, "weekly")] == [u.id]
    with patch.object(s, "send_subscription_notice") as notice:
        s.run_cadence_checks(db_session, "weekly", TODAY)
        assert (
            u.subscription_status,
            u.subscription_type,
            u.report_cadence,
            u.subscription_cancel_pending,
            u.subscription_expires_on,
        ) == ("cancelled", None, "none", False, OLD)
        assert not charges(db_session, u)
        notice.assert_not_called()
    assert active_users(db_session, "weekly") == []


@pytest.mark.parametrize("state,expected", [("active", "cancelled"), ("expired", "expired")])
def test_unverified_rules_3_5(db_session: Session, state: str, expected: str) -> None:
    u = subscriber(db_session, "5.00", state, verified=False)
    with patch.object(s, "send_subscription_notice") as notice:
        s.run_cadence_checks(db_session, "weekly", TODAY)
        assert u.subscription_status == expected
        assert u.credit_gift_balance == Decimal("5.00")
        assert not charges(db_session, u)
        notice.assert_not_called()


def test_population_without_holdings_and_disabled_account(db_session: Session) -> None:
    users = [
        subscriber(db_session, "5.00", state, "mwf") for state in ("active", "expired", "active")
    ]
    users[2].status = "suspended"
    db_session.commit()
    assert active_users(db_session, "mwf") == []
    s.run_cadence_checks(db_session, "mwf", TODAY)
    for u in users[:2]:
        assert u.subscription_status == "active"
        assert u.credit_gift_balance == Decimal("3.01")
        assert len(charges(db_session, u)) == 1
    assert not charges(db_session, users[2])
    assert users[2].credit_gift_balance == Decimal("5.00")


def test_one_failure_continues_next_user(db_session: Session) -> None:
    users = sorted([subscriber(db_session), subscriber(db_session)], key=lambda u: u.id)
    original = consume_credits
    with (
        patch.object(
            s, "consume_credits", side_effect=[RuntimeError("counterexample"), None]
        ) as consume,
        patch.object(s, "send_ops_alert") as alert,
    ):
        consume.side_effect = lambda *args, **kwargs: (
            (_ for _ in ()).throw(RuntimeError("counterexample"))
            if kwargs["user_id"] == users[0].id
            else original(*args, **kwargs)
        )
        s.run_cadence_checks(db_session, "weekly", TODAY)
    assert not charges(db_session, users[0])
    assert users[0].subscription_expires_on == OLD
    assert users[1].subscription_expires_on == date(2026, 12, 17)
    assert len(charges(db_session, users[1])) == 1
    alert.assert_called_once()
    assert str(users[0].id) in str(alert.call_args)


@pytest.mark.parametrize("balance,state,count", [("5.00", "active", 1), ("0.40", "expired", 0)])
def test_long_gap_one_charge_and_retry(
    db_session: Session, balance: str, state: str, count: int
) -> None:
    u = subscriber(db_session, balance)
    u.subscription_expires_on = date(2026, 8, 17)
    u.subscription_period_start = date(2026, 7, 17)
    db_session.commit()
    with patch.object(s, "send_subscription_notice") as notice:
        s.run_cadence_checks(db_session, "weekly", TODAY)
        assert u.subscription_status == state
        if count:
            assert (
                u.subscription_period_start,
                u.subscription_expires_on,
                u.subscription_anchor_day,
            ) == (TODAY, date(2026, 12, 21), 21)
            (row,) = charges(db_session, u)
            assert (row.amount, row.idempotency_key, row.balance_after) == (
                Decimal("-0.99"),
                s.charge_key(u.id, TODAY, "weekly"),
                Decimal("4.01"),
            )
            notice.assert_not_called()
        else:
            notice.assert_called_once_with(
                u.email, "expired", **expected_notice(u, "0.40", date(2026, 8, 17))
            )
        s.run_cadence_checks(db_session, "weekly", TODAY)
        assert len(charges(db_session, u)) == count
        assert notice.call_count == (0 if count else 1)


def test_reminder_conditions_and_verified_delivery(db_session: Session) -> None:
    u = subscriber(db_session, "0.40")
    u.delivery_email = "delivery@example.com"
    u.delivery_email_verified_at = datetime(2026, 10, 17, tzinfo=ET)
    db_session.commit()
    with patch.object(s, "send_subscription_notice") as notice:
        s.maybe_send_low_balance_reminder(db_session, u.id)
        notice.assert_called_once_with(
            "delivery@example.com", "low_balance", **expected_notice(u, "0.40")
        )
        notice.reset_mock()
        u.subscription_cancel_pending = True
        db_session.flush()
        s.maybe_send_low_balance_reminder(db_session, u.id)
        u.subscription_cancel_pending = False
        u.email_verified_at = u.delivery_email_verified_at = None
        db_session.flush()
        s.maybe_send_low_balance_reminder(db_session, u.id)
        notice.assert_not_called()


def test_subscribe_reminder_after_commit(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.tests.conftest import TEST_USER_ID

    u = seed_user(db_session, TEST_USER_ID)
    u.email_verified_at = datetime(2026, 10, 17, tzinfo=ET)
    db_session.flush()
    adjust_by_admin(
        db_session,
        user_id=u.id,
        amount=Decimal("1.49"),
        note="fixture",
        idempotency_key="subscribe",
    )
    db_session.commit()
    monkeypatch.setattr("app.routers.me.today_et", lambda: TODAY)
    with patch.object(s, "send_subscription_notice") as notice:
        response = app_client.post("/me/subscription", json={"type": "weekly"})
        assert response.status_code == 200
        db_session.refresh(u)
        assert u.credit_gift_balance == Decimal("0.50")
        notice.assert_called_once_with(
            u.email, "low_balance", **expected_notice(u, "0.50", date(2026, 12, 21))
        )


def test_purchase_short_balance_reminder_and_replay(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.routers import paddle_webhooks
    from app.tests.test_paddle_payments import PRICE, _purchase_payload, _settings, _signed

    settings = _settings()
    settings.PADDLE_CREDIT_PACKS = {PRICE: Decimal("0.10")}
    monkeypatch.setattr(paddle_webhooks, "get_settings", lambda: settings)
    u = subscriber(db_session, "0.40")
    raw, headers = _signed(_purchase_payload(str(u.id)))
    with patch.object(s, "send_subscription_notice") as notice:
        for _ in range(2):
            assert (
                app_client.post("/webhooks/paddle", content=raw, headers=headers).status_code == 200
            )
        db_session.refresh(u)
        assert u.credit_cash_balance == Decimal("0.10")
        notice.assert_called_once_with(u.email, "low_balance", **expected_notice(u, "0.50"))
        rows = list(
            db_session.scalars(
                select(CreditLedgerEntry).where(CreditLedgerEntry.reason == "recharge")
            )
        )
        (row,) = rows
        assert (row.amount, row.bucket, row.idempotency_key) == (
            Decimal("0.10"),
            "cash",
            "recharge:paddle:txn_A",
        )


def test_activation_dry_run_apply_twice_all_rows(
    db_session: Session, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.scripts import activate_existing_subscriptions as activation

    monkeypatch.setattr(activation, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(activation, "today_et", lambda: date(2026, 10, 5))
    monkeypatch.setattr("sys.argv", ["activate_existing_subscriptions"])

    active = subscriber(db_session, "5.00", "inactive", "mwf")
    unverified = subscriber(db_session, "5.00", "inactive", verified=False)
    insufficient = subscriber(db_session, "0.40", "inactive")
    processed = subscriber(db_session, "5.00")
    disabled = subscriber(db_session, "5.00", "inactive")
    disabled.status = "suspended"
    no_cadence = subscriber(db_session, "5.00", "inactive")
    no_cadence.report_cadence = "none"
    db_session.commit()
    emails = [u.email for u in (active, unverified, insufficient, processed, disabled, no_cadence)]
    before = list(db_session.scalars(select(CreditLedgerEntry.id)))
    with patch.object(s, "send_subscription_notice") as notice:
        activation.main()
        assert list(db_session.scalars(select(CreditLedgerEntry.id))) == before
        assert active.subscription_status == "inactive" and unverified.report_cadence == "weekly"
        monkeypatch.setattr("sys.argv", ["activate_existing_subscriptions", "--apply"])
        activation.main()
        for user in (active, unverified, insufficient, processed, disabled, no_cadence):
            db_session.add(user)
            db_session.refresh(user)
        assert (
            active.subscription_status,
            active.subscription_type,
            active.subscription_period_start,
            active.subscription_expires_on,
            active.subscription_anchor_day,
            active.subscription_adjusted_on,
        ) == ("active", "mwf", date(2026, 10, 5), date(2026, 11, 5), 5, None)
        assert active.credit_gift_balance == Decimal("3.01")
        (row,) = charges(db_session, active)
        assert (row.amount, row.idempotency_key) == (
            Decimal("-1.99"),
            s.charge_key(active.id, date(2026, 10, 5), "mwf"),
        )
        for u in (unverified, insufficient, disabled, no_cadence):
            assert (u.subscription_status, u.report_cadence) == ("inactive", "none")
            assert not charges(db_session, u)
        assert processed.subscription_expires_on == OLD and not charges(db_session, processed)
        after = list(db_session.scalars(select(CreditLedgerEntry.id)))
        monkeypatch.setattr("sys.argv", ["activate_existing_subscriptions", "--apply"])
        activation.main()
        assert list(db_session.scalars(select(CreditLedgerEntry.id))) == after
        notice.assert_not_called()
    output = capsys.readouterr().out
    assert "insufficient" in output and "1.99" in output
    for email in emails:
        assert email in output


@pytest.mark.parametrize("recipient_state", ["expired", "active"])
def test_task_checks_empty_and_all_failed_retry(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, recipient_state: str
) -> None:
    from app.tasks import report_tasks as task

    u = subscriber(db_session, "5.00", recipient_state)
    monkeypatch.setattr(task, "today_et", lambda: TODAY, raising=False)
    with (
        patch(
            "app.services.report_generator.generate_report",
            side_effect=RuntimeError("report failed"),
        ) as generate,
        patch.object(
            task.generate_incremental_report, "retry", side_effect=RuntimeError("retry")
        ) as retry,
    ):
        if recipient_state == "active":
            with pytest.raises(RuntimeError, match="retry"):
                task.generate_incremental_report.run(cadences=["weekly"])
            retry.assert_called_once()
        else:
            with pytest.raises(RuntimeError, match="retry"):
                task.generate_incremental_report.run(cadences=["weekly"])
            retry.assert_called_once()
            generate.assert_called_once()
            assert generate.call_args.kwargs["user_id"] == u.id
        db_session.refresh(u)
        assert u.subscription_status == "active"
        assert len(charges(db_session, u)) == 1
        if recipient_state == "active":
            with pytest.raises(RuntimeError, match="retry"):
                task.generate_incremental_report.run(cadences=["weekly"])
            assert len(charges(db_session, u)) == 1
        else:
            (row,) = charges(db_session, u)
            assert row.idempotency_key == s.charge_key(u.id, TODAY, "weekly")
            assert u.credit_gift_balance == Decimal("4.01")
            assert (
                u.subscription_period_start,
                u.subscription_expires_on,
                u.subscription_anchor_day,
            ) == (TODAY, date(2026, 12, 21), 21)
            retry.reset_mock()
            with pytest.raises(RuntimeError, match="retry"):
                task.generate_incremental_report.run(cadences=["weekly"])
            retry.assert_called_once()
            db_session.refresh(u)
            assert len(charges(db_session, u)) == 1
            assert u.credit_gift_balance == Decimal("4.01")
            assert generate.call_count == 2


def test_task_stale_trigger_skips_checks(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.tasks import report_tasks as task

    u = subscriber(db_session)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "Clock":
            return cls(2026, 11, 21, 22, tzinfo=ET)

    monkeypatch.setattr(task, "datetime", Clock)
    with patch.object(s, "run_cadence_checks") as check:
        assert task.generate_incremental_report.run(
            cadences=["weekly"], trigger_hour=19, trigger_minute=0
        ) == {"status": "skipped_stale_trigger"}
        check.assert_not_called()
        assert not charges(db_session, u)


def test_task_check_phase_failure_preserves_report_result(db_session: Session) -> None:
    from types import SimpleNamespace

    from app.tasks import report_tasks as task

    subscriber(db_session)
    report = SimpleNamespace(id=uuid.uuid4(), status="success", report_inputs={})
    with (
        patch.object(s, "run_cadence_checks", side_effect=RuntimeError("phase failed")),
        patch("app.services.report_generator.generate_report", return_value=report),
        patch.object(task, "send_ops_alert") as alert,
    ):
        result = task.generate_incremental_report.run(cadences=["weekly"])
        assert result["status"] == "completed" and result["results"][0]["status"] == "success"
        assert alert.call_count == 2
        assert all("phase failed" in str(call) for call in alert.call_args_list)


@pytest.mark.parametrize("locale", ["en", "zh", "zh-Hant", "fr"])
@pytest.mark.parametrize("kind", ["low_balance", "expired"])
def test_notice_copy_and_locale_fallback(locale: str, kind: str) -> None:
    from app.services import email_sender as email

    with patch.object(httpx, "Client") as client:
        post = client.return_value.__enter__.return_value.post
        post.return_value.json.return_value = {"id": "provider-id"}
        assert (
            email.send_subscription_notice(
                "verified@example.com",
                kind,
                locale=locale,
                plan="weekly",
                expires_on=OLD,
                fee=Decimal("0.99"),
                balance=Decimal("0.40"),
            )
            == "provider-id"
        )
        payload = post.call_args.kwargs["json"]
        english_subject = {
            "low_balance": "Portfonia - your credit balance will not cover the next renewal",
            "expired": "Portfonia - your subscription has ended",
        }[kind]
        if locale in ("en", "fr"):
            assert payload["subject"] == english_subject
        else:
            copy = email._SUBSCRIPTION_NOTICE_COPY[locale]
            assert payload["subject"] == copy[f"{kind}_subject"] != english_subject
        assert payload["to"] == ["verified@example.com"]
        assert (
            "2026-11-17" in payload["text"]
            and "0.99" in payload["text"]
            and "0.40" in payload["text"]
            and "/profile" in payload["text"]
        )
        if locale in ("en", "fr"):
            assert "Weekly" in payload["text"]


def test_notice_failure_and_missing_provider_id_return_none() -> None:
    from app.services import email_sender as email

    for fail in (True, False):
        with patch.object(httpx, "Client") as client:
            post = client.return_value.__enter__.return_value.post
            if fail:
                post.side_effect = RuntimeError("provider down")
            else:
                post.return_value.json.return_value = {}
            assert (
                email.send_subscription_notice(
                    "verified@example.com",
                    "expired",
                    locale="en",
                    plan="weekly",
                    expires_on=OLD,
                    fee=Decimal("0.99"),
                    balance=Decimal("0.40"),
                )
                is None
            )


@pytest.mark.parametrize("today,charged", [(OLD, False), (date(2026, 12, 17), True)])
def test_inclusive_expiry_and_normal_renewal_boundary_ignores_daily_lock(
    db_session: Session, today: date, charged: bool
) -> None:
    u = subscriber(db_session, "5.00")
    u.subscription_adjusted_on = today
    db_session.commit()
    s.run_cadence_checks(db_session, "weekly", today)
    assert u.subscription_status == "active"
    assert u.subscription_adjusted_on == today
    assert len(charges(db_session, u)) == int(charged)
    assert u.subscription_anchor_day == 17
    if charged:
        assert u.subscription_period_start == OLD
        assert u.subscription_expires_on == date(2026, 12, 17)
        assert u.credit_gift_balance == Decimal("4.01")
    else:
        assert u.subscription_expires_on == OLD
        assert u.credit_gift_balance == Decimal("5.00")


@pytest.mark.parametrize("balance,state,rows", [("1.49", "active", 1), ("0.40", "expired", 0)])
def test_notice_provider_failure_keeps_committed_state(
    db_session: Session, balance: str, state: str, rows: int
) -> None:
    from app.services.email_sender import send_subscription_notice

    u = subscriber(db_session, balance)
    with (
        patch.object(s, "send_subscription_notice", side_effect=send_subscription_notice),
        patch.object(httpx, "Client") as client,
    ):
        client.return_value.__enter__.return_value.post.side_effect = RuntimeError(
            "provider unavailable"
        )
        s.run_cadence_checks(db_session, "weekly", TODAY)
    db_session.refresh(u)
    assert u.subscription_status == state
    assert len(charges(db_session, u)) == rows
    assert u.credit_gift_balance == Decimal("0.50" if rows else "0.40")
    s.run_cadence_checks(db_session, "weekly", TODAY)
    assert len(charges(db_session, u)) == rows


@pytest.mark.parametrize(
    "today,balance,pending,state,remaining,notice_kind",
    [
        (date(2026, 11, 14), "1.49", False, "active", "1.49", None),
        (TODAY, "1.49", False, "active", "0.50", "low_balance"),
        (TODAY, "0.40", False, "expired", "0.40", "expired"),
        (TODAY, "1.49", True, "cancelled", "1.49", None),
    ],
)
def test_task_examples_1_2_3_6_report_precedes_check(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    today: date,
    balance: str,
    pending: bool,
    state: str,
    remaining: str,
    notice_kind: str | None,
) -> None:
    from types import SimpleNamespace

    from app.tasks import report_tasks as task

    u = subscriber(db_session, balance)
    u.subscription_cancel_pending = pending
    db_session.commit()
    monkeypatch.setattr(task, "today_et", lambda: today)
    user_id = u.id
    report = SimpleNamespace(id=uuid.uuid4(), status="success", report_inputs={})

    def generated(session: Session, **kwargs: Any) -> SimpleNamespace:
        checked = session.get(User, kwargs["user_id"])
        assert checked is not None
        assert checked.subscription_status == "active" and checked.subscription_expires_on == OLD
        assert kwargs["user_id"] == user_id
        return report

    with (
        patch("app.services.report_generator.generate_report", side_effect=generated) as generate,
        patch.object(s, "send_subscription_notice") as notice,
    ):
        result = task.generate_incremental_report.run(cadences=["weekly"])
        assert result == {
            "status": "completed",
            "results": [{"user_id": str(u.id), "report_id": str(report.id), "status": "success"}],
        }
        generate.assert_called_once()
        db_session.refresh(u)
        assert u.subscription_status == state
        assert u.credit_gift_balance == Decimal(remaining)
        assert len(charges(db_session, u)) == int(notice_kind == "low_balance")
        if notice_kind:
            expiry = date(2026, 12, 17) if notice_kind == "low_balance" else OLD
            notice.assert_called_once_with(
                u.email, notice_kind, **expected_notice(u, remaining, expiry)
            )
        else:
            notice.assert_not_called()
        if state in ("expired", "cancelled"):
            generate.reset_mock()
            assert task.generate_incremental_report.run(cadences=["weekly"]) == {
                "status": "no_active_users",
                "results": [],
            }
            generate.assert_not_called()


@pytest.mark.parametrize("balance,remaining", [("5.40", "4.41"), ("1.49", "0.50")])
def test_task_expired_resume_receives_same_batch(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, balance: str, remaining: str
) -> None:
    from app.tasks import report_tasks as task

    today = date(2026, 12, 5)
    expiry = date(2027, 1, 5)
    u = subscriber(db_session, balance, "expired")
    user_id = u.id
    monkeypatch.setattr(task, "today_et", lambda: today)
    report = SimpleNamespace(id=uuid.uuid4(), status="success", report_inputs={})

    def generated(session: Session, **kwargs: Any) -> SimpleNamespace:
        checked = session.get(User, kwargs["user_id"])
        assert checked is not None and checked.subscription_status == "active"
        assert (
            checked.subscription_period_start,
            checked.subscription_expires_on,
            checked.subscription_anchor_day,
        ) == (today, expiry, 5)
        assert checked.credit_gift_balance == Decimal(remaining)
        (row,) = charges(session, checked)
        assert (row.amount, row.bucket, row.balance_after, row.idempotency_key, row.reference) == (
            Decimal("-0.99"),
            "gift",
            Decimal(remaining),
            s.charge_key(user_id, today, "weekly"),
            "weekly",
        )
        return report

    with (
        patch("app.services.report_generator.generate_report", side_effect=generated) as generate,
        patch.object(s, "send_subscription_notice") as notice,
    ):
        result = task.generate_incremental_report.run(cadences=["weekly"])
        assert result == {
            "status": "completed",
            "results": [{"user_id": str(u.id), "report_id": str(report.id), "status": "success"}],
        }
        generate.assert_called_once()
        kwargs = generate.call_args.kwargs
        assert (
            kwargs["user_id"],
            kwargs["output_lang"],
            kwargs["base_currency"],
            kwargs["report_type"],
            kwargs["session_node"],
            kwargs["users_remaining"],
        ) == (
            u.id,
            u.locale,
            u.base_currency,
            "incremental",
            "weekend_snapshot",
            1,
        )
        db_session.refresh(u)
        assert (
            u.subscription_status,
            u.subscription_type,
            u.report_cadence,
            u.subscription_period_start,
            u.subscription_expires_on,
            u.subscription_anchor_day,
        ) == ("active", "weekly", "weekly", today, expiry, 5)
        assert u.credit_gift_balance == Decimal(remaining)
        assert u.credit_cash_balance == Decimal("0.00")
        assert len(charges(db_session, u)) == 1
        if remaining == "0.50":
            notice.assert_called_once_with(
                u.email, "low_balance", **expected_notice(u, remaining, expiry)
            )
        else:
            notice.assert_not_called()


@pytest.mark.parametrize("balance,verified", [("0.40", True), ("5.40", False)])
def test_task_expired_not_resumed_is_not_dispatched(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, balance: str, verified: bool
) -> None:
    from app.tasks import report_tasks as task

    u = subscriber(db_session, balance, "expired", verified=verified)
    monkeypatch.setattr(task, "today_et", lambda: date(2026, 12, 5))
    with (
        patch("app.services.report_generator.generate_report") as generate,
        patch.object(s, "send_subscription_notice") as notice,
    ):
        assert task.generate_incremental_report.run(cadences=["weekly"]) == {
            "status": "no_active_users",
            "results": [],
        }
        generate.assert_not_called()
        notice.assert_not_called()
    db_session.refresh(u)
    assert (
        u.subscription_status,
        u.subscription_type,
        u.report_cadence,
        u.subscription_period_start,
        u.subscription_expires_on,
        u.subscription_anchor_day,
    ) == (
        "expired",
        "weekly",
        "weekly",
        date(2026, 10, 17),
        OLD,
        17,
    )
    assert u.credit_gift_balance == Decimal(balance)
    assert u.credit_cash_balance == Decimal("0.00")
    assert not charges(db_session, u)


def test_expired_only_check_leaves_overdue_active_untouched(db_session: Session) -> None:
    active = subscriber(db_session, "5.40")
    expired = subscriber(db_session, "5.40", "expired")
    with patch.object(s, "send_subscription_notice") as notice:
        assert s.run_cadence_checks(db_session, "weekly", TODAY, statuses=("expired",)) == [
            s.CheckOutcome(expired.id, "resumed")
        ]
        db_session.refresh(active)
        assert (
            active.subscription_status,
            active.subscription_period_start,
            active.subscription_expires_on,
            active.subscription_anchor_day,
        ) == (
            "active",
            date(2026, 10, 17),
            OLD,
            17,
        )
        assert active.credit_gift_balance == Decimal("5.40")
        assert not charges(db_session, active)
        assert expired.subscription_status == "active"
        assert expired.credit_gift_balance == Decimal("4.41")
        (row,) = charges(db_session, expired)
        assert row.idempotency_key == s.charge_key(expired.id, TODAY, "weekly")
        outcomes = s.run_cadence_checks(db_session, "weekly", TODAY)
        assert {item.user_id: item.outcome for item in outcomes} == {
            active.id: "renewed",
            expired.id: "unchanged",
        }
        assert (
            active.subscription_period_start,
            active.subscription_expires_on,
            active.subscription_anchor_day,
        ) == (OLD, date(2026, 12, 17), 17)
        assert active.credit_gift_balance == Decimal("4.41")
        (row,) = charges(db_session, active)
        assert row.idempotency_key == s.charge_key(active.id, OLD, "weekly")
        assert len(charges(db_session, expired)) == 1
        notice.assert_not_called()


def test_task_pre_dispatch_failure_falls_back_to_post_loop_resume(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.tasks import report_tasks as task

    active = subscriber(db_session, "5.40")
    expired = subscriber(db_session, "5.40", "expired")
    monkeypatch.setattr(task, "today_et", lambda: TODAY)
    active_id, expired_id = active.id, expired.id
    original = s.run_cadence_checks
    report = SimpleNamespace(id=uuid.uuid4(), status="success", report_inputs={})

    def checks(
        session: Session,
        cadence: str,
        today: date,
        *,
        statuses: tuple[str, ...] = ("active", "expired"),
    ) -> list[s.CheckOutcome]:
        if statuses == ("expired",):
            raise RuntimeError("pre-dispatch failed")
        return original(session, cadence, today, statuses=statuses)

    def generated(session: Session, **kwargs: Any) -> SimpleNamespace:
        assert kwargs["user_id"] == active_id
        checked = session.get(User, expired_id)
        assert checked is not None and checked.subscription_status == "expired"
        checked_active = session.get(User, active_id)
        assert checked_active is not None
        assert not charges(session, checked_active) and not charges(session, checked)
        return report

    with (
        patch.object(s, "run_cadence_checks", side_effect=checks) as check,
        patch("app.services.report_generator.generate_report", side_effect=generated) as generate,
        patch.object(task, "send_ops_alert") as alert,
        patch.object(s, "send_subscription_notice") as notice,
        patch.object(task.generate_incremental_report, "retry") as retry,
    ):
        result = task.generate_incremental_report.run(cadences=["weekly"])
        assert result == {
            "status": "completed",
            "results": [
                {"user_id": str(active.id), "report_id": str(report.id), "status": "success"}
            ],
        }
        generate.assert_called_once()
        assert generate.call_args.kwargs["user_id"] == active.id
        assert [call.kwargs["statuses"] for call in check.call_args_list] == [
            ("expired",),
            ("active", "expired"),
        ]
        alert.assert_called_once()
        assert "pre-dispatch failed" in str(alert.call_args)
        retry.assert_not_called()
        notice.assert_not_called()
    for u, start, expiry, anchor in (
        (active, OLD, date(2026, 12, 17), 17),
        (expired, TODAY, date(2026, 12, 21), 21),
    ):
        db_session.refresh(u)
        assert (
            u.subscription_status,
            u.subscription_period_start,
            u.subscription_expires_on,
            u.subscription_anchor_day,
        ) == (
            "active",
            start,
            expiry,
            anchor,
        )
        assert u.credit_gift_balance == Decimal("4.41")
        (row,) = charges(db_session, u)
        assert (row.amount, row.idempotency_key) == (
            Decimal("-0.99"),
            s.charge_key(u.id, start, "weekly"),
        )
