"""Jade D10 and A1-A4 acceptance on real Postgres."""

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.user import User
from app.services import subscription as s
from app.services.credit_ledger import adjust_by_admin
from app.tests.conftest import TEST_USER_ID, seed_user
from app.tests.test_subscription import funded_user, subscription_rows

DAY = date(2026, 10, 16)


@pytest.mark.parametrize(
    "status,plan,cadence,pending,target,expected",
    [
        ("inactive", None, "none", False, "jade", "daily"),
        ("cancelled", None, "none", False, "jade", "daily"),
        ("expired", "weekly", "weekly", False, "jade", "daily"),
        ("active", "mwf", "mwf", True, "jade", "mwf"),
        ("expired", "jade", "weekly", False, "jade", "weekly"),
        ("active", "jade", "mwf", False, "jade", "mwf"),
        *[("inactive", None, "none", False, p, p) for p in ("weekly", "mwf", "daily")],
    ],
)
def test_a1_resulting_cadence(
    status: str, plan: str | None, cadence: str, pending: bool, target: str, expected: str
) -> None:
    user = User(
        subscription_status=status,
        subscription_type=plan,
        report_cadence=cadence,
        subscription_cancel_pending=pending,
    )
    assert s.resulting_cadence(user, target) == expected


def test_d10_1_weekly_change(db_session: Session) -> None:
    u = funded_user(db_session, "20.99")
    s.set_plan(db_session, u.id, date(2026, 10, 1), "weekly")
    assert u.credit_gift_balance == Decimal("20.00")
    q = s.quote(db_session, u.id, DAY, "jade")
    assert (q.cadence, q.returned, q.fee) == ("weekly", "0.53", "9.99")
    assert u.subscription_expires_on is not None and u.subscription_period_start is not None
    assert (u.subscription_expires_on - u.subscription_period_start).days + 1 == 32
    assert (u.subscription_expires_on - DAY).days + 1 == 17
    s.set_plan(db_session, u.id, DAY, "jade")
    old = f"subscription:{u.id}:2026-10-01:weekly"
    assert subscription_rows(db_session, u)[1:] == [
        ("gift", Decimal("0.53"), "subscription_return", f"subscription_return:{old}"),
        ("gift", Decimal("-9.99"), "subscription", f"subscription:{u.id}:2026-10-16:jade"),
    ]
    assert (
        u.subscription_type,
        u.report_cadence,
        u.subscription_period_start,
        u.subscription_expires_on,
        u.subscription_anchor_day,
        u.subscription_adjusted_on,
    ) == ("jade", "weekly", DAY, date(2026, 11, 16), 16, DAY)
    assert u.credit_gift_balance == Decimal("10.54")


def test_d10_2_inactive_and_notice(db_session: Session) -> None:
    u = funded_user(db_session, "10.00")
    s.set_plan(db_session, u.id, DAY, "jade")
    assert u.report_cadence == "daily" and u.credit_gift_balance == Decimal("0.01")
    assert subscription_rows(db_session, u) == [
        ("gift", Decimal("-9.99"), "subscription", f"subscription:{u.id}:2026-10-16:jade")
    ]
    with patch.object(s, "send_subscription_notice") as notice:
        s.maybe_send_low_balance_reminder(db_session, u.id)
        notice.assert_called_once_with(
            u.email,
            "low_balance",
            locale=u.locale,
            plan="jade",
            expires_on=date(2026, 11, 16),
            fee=Decimal("9.99"),
            balance=Decimal("0.01"),
        )


@pytest.fixture
def jade_api(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> User:
    u = seed_user(db_session, TEST_USER_ID)
    from datetime import datetime

    from app.core.timezones import ET

    u.email_verified_at = datetime(2026, 10, 1, tzinfo=ET)
    db_session.flush()
    adjust_by_admin(
        db_session,
        user_id=u.id,
        amount=Decimal("10.00"),
        note="fixture",
        idempotency_key="jade-fixture",
    )
    s.set_plan(db_session, u.id, DAY, "jade")
    db_session.commit()
    monkeypatch.setattr("app.routers.me.today_et", lambda: DAY)
    return u


def test_d10_3_a3_cadence_bypasses_lock(
    app_client: TestClient, db_session: Session, jade_api: User
) -> None:
    before = subscription_rows(db_session, jade_api)
    for cadence in ("weekly", "mwf", "mwf"):
        r = app_client.patch("/me/jade/cadence", json={"cadence": cadence})
        assert r.status_code == 200, r.text
        assert r.json()["cadence"] == cadence
        db_session.refresh(jade_api)
        assert jade_api.subscription_adjusted_on == DAY
        assert subscription_rows(db_session, jade_api) == before
    for endpoint, body in (
        ("/me/subscription", {"type": "jade"}),
        ("/me/subscription/cancel", None),
        ("/me/subscription/resume", None),
    ):
        r = app_client.post(endpoint, json=body)
        assert r.status_code == 409 and r.json() == {"detail": "daily_limit"}
    assert app_client.patch("/me/jade/cadence", json={"cadence": "none"}).status_code == 422


@pytest.mark.parametrize("day,code", [(DAY, "daily_limit"), (date(2026, 10, 17), "jade_managed")])
def test_d10_4_error_precedence(
    app_client: TestClient,
    db_session: Session,
    jade_api: User,
    monkeypatch: pytest.MonkeyPatch,
    day: date,
    code: str,
) -> None:
    monkeypatch.setattr("app.routers.me.today_et", lambda: day)
    before = subscription_rows(db_session, jade_api)
    r = app_client.post("/me/subscription", json={"type": "weekly"})
    assert r.status_code == 409 and r.json() == {"detail": code}
    db_session.refresh(jade_api)
    assert (
        jade_api.subscription_type,
        jade_api.report_cadence,
        jade_api.subscription_adjusted_on,
    ) == ("jade", "daily", DAY)
    assert subscription_rows(db_session, jade_api) == before


def test_d10_5_non_jade_refused(app_client: TestClient, db_session: Session) -> None:
    u = seed_user(db_session, TEST_USER_ID)
    u.subscription_status = "active"
    u.subscription_type = u.report_cadence = "weekly"
    db_session.commit()
    r = app_client.patch("/me/jade/cadence", json={"cadence": "daily"})
    assert r.status_code == 409 and r.json() == {"detail": "not_jade"}
    db_session.refresh(u)
    assert u.report_cadence == "weekly" and subscription_rows(db_session, u) == []


def jade_user(db: Session, balance: str = "12.00", cadence: str = "mwf") -> User:
    u = funded_user(db, balance)
    u.subscription_status = "active"
    u.subscription_type = "jade"
    u.report_cadence = cadence
    u.subscription_period_start = DAY
    u.subscription_expires_on = date(2026, 11, 16)
    u.subscription_anchor_day = 16
    db.commit()
    return u


def test_d10_6_renewal(db_session: Session) -> None:
    u = jade_user(db_session)
    assert s.run_cadence_checks(db_session, "mwf", date(2026, 11, 16)) == [
        s.CheckOutcome(u.id, "unchanged")
    ]
    assert s.run_cadence_checks(db_session, "mwf", date(2026, 11, 18)) == [
        s.CheckOutcome(u.id, "renewed")
    ]
    assert subscription_rows(db_session, u) == [
        ("gift", Decimal("-9.99"), "subscription", f"subscription:{u.id}:2026-11-16:jade")
    ]
    assert (
        u.subscription_period_start,
        u.subscription_expires_on,
        u.report_cadence,
        u.credit_gift_balance,
    ) == (date(2026, 11, 16), date(2026, 12, 16), "mwf", Decimal("2.01"))


def test_d10_7_expired_and_same_batch_recovery(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from app.models.holding import Holding
    from app.tasks import report_tasks as task

    u = jade_user(db_session, "3.00")
    with patch.object(s, "send_subscription_notice") as notice:
        assert s.run_cadence_checks(db_session, "mwf", date(2026, 11, 18)) == [
            s.CheckOutcome(u.id, "expired")
        ]
        assert (u.subscription_status, u.subscription_type, u.report_cadence) == (
            "expired",
            "jade",
            "mwf",
        )
        notice.assert_called_once_with(
            u.email,
            "expired",
            locale=u.locale,
            plan="jade",
            expires_on=date(2026, 11, 16),
            fee=Decimal("9.99"),
            balance=Decimal("3.00"),
        )
    adjust_by_admin(
        db_session,
        user_id=u.id,
        amount=Decimal("12.00"),
        note="top up",
        idempotency_key=str(uuid.uuid4()),
    )
    assert u.credit_gift_balance == Decimal("15.00")
    db_session.add(
        Holding(
            user_id=u.id,
            name="Cash",
            pricing_mode="manual",
            currency="USD",
            asset_class="CASH_EQUIV",
            current_value=Decimal("100"),
        )
    )
    db_session.commit()
    monkeypatch.setattr(task, "today_et", lambda: date(2026, 11, 20))
    report = SimpleNamespace(id=uuid.uuid4(), status="success", report_inputs={})
    with patch("app.services.report_generator.generate_report", return_value=report) as generate:
        result = task.generate_incremental_report.run(cadences=["mwf"])
        assert result["status"] == "completed"
        generate.assert_called_once()
        assert generate.call_args.kwargs["user_id"] == u.id
    assert (
        u.subscription_status,
        u.subscription_type,
        u.report_cadence,
        u.credit_gift_balance,
    ) == ("active", "jade", "mwf", Decimal("5.01"))
    assert subscription_rows(db_session, u) == [
        ("gift", Decimal("-9.99"), "subscription", f"subscription:{u.id}:2026-11-20:jade")
    ]


def test_d10_8_cancel_to_expiry(db_session: Session) -> None:
    u = jade_user(db_session)
    u.subscription_cancel_pending = True
    db_session.commit()
    assert s.run_cadence_checks(db_session, "mwf", date(2026, 11, 18)) == [
        s.CheckOutcome(u.id, "cancelled")
    ]
    assert (u.subscription_type, u.report_cadence) == (None, "none")
    assert not s.is_advanced(u) and not s.is_jade(u)
    assert subscription_rows(db_session, u) == []


def test_a2_jade_quotes(db_session: Session) -> None:
    u = funded_user(db_session, "20.00")
    q = s.quote(db_session, u.id, DAY, "jade")
    assert (q.cadence, q.needs_holdings, q.fee) == ("daily", True, "9.99")
    s.set_plan(db_session, u.id, date(2026, 10, 1), "daily")
    q = s.quote(db_session, u.id, DAY, "jade")
    assert (q.action, q.fee, q.cadence) == ("change", "9.99", "daily")


def test_a4_check_population(db_session: Session) -> None:
    weekly = jade_user(db_session, cadence="weekly")
    daily = jade_user(db_session, cadence="daily")
    assert s.run_cadence_checks(db_session, "weekly", date(2026, 11, 18)) == [
        s.CheckOutcome(weekly.id, "renewed")
    ]
    assert subscription_rows(db_session, daily) == []


def test_overdue_same_plan_is_no_change(db_session: Session) -> None:
    u = jade_user(db_session)
    with pytest.raises(s.SubscriptionError, match="no_change"):
        s.set_plan(db_session, u.id, date(2026, 11, 18), "jade")
    assert subscription_rows(db_session, u) == []
