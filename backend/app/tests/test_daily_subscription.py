"""Daily billing and holdings acceptance for issue #650."""

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.holding import Holding
from app.services import subscription as s
from app.services.user_scope import active_user_ids
from app.tests.test_subscription import funded_user, subscription_rows


def test_daily_acceptance_2_charge_and_insufficient(db_session: Session) -> None:
    assert s.PLAN_FEES.get("daily") == Decimal("2.49")
    today = date(2026, 10, 7)
    user = funded_user(db_session, "2.49")
    s.set_plan(db_session, user.id, today, "daily")
    assert (user.subscription_status, user.subscription_type, user.report_cadence) == (
        "active",
        "daily",
        "daily",
    )
    assert user.credit_gift_balance == Decimal("0.00")
    assert subscription_rows(db_session, user) == [
        ("gift", Decimal("-2.49"), "subscription", f"subscription:{user.id}:2026-10-07:daily")
    ]
    short = funded_user(db_session, "2.48")
    with pytest.raises(s.SubscriptionError, match="insufficient_credits"):
        s.set_plan(db_session, short.id, today, "daily")
    assert subscription_rows(db_session, short) == []
    assert short.subscription_status == "inactive"
    assert short.credit_gift_balance == Decimal("2.48")


def test_daily_acceptance_3_change_to_weekly(db_session: Session) -> None:
    user = funded_user(db_session)
    s.set_plan(db_session, user.id, date(2026, 10, 1), "daily")
    old_key = f"subscription:{user.id}:2026-10-01:daily"
    s.set_plan(db_session, user.id, date(2026, 10, 17), "weekly")
    # 16 unused days out of 32, rounded up: 2.49 * 16 / 32 = 1.25.
    assert subscription_rows(db_session, user)[1:] == [
        ("gift", Decimal("1.25"), "subscription_return", f"subscription_return:{old_key}"),
        ("gift", Decimal("-0.99"), "subscription", f"subscription:{user.id}:2026-10-17:weekly"),
    ]
    assert user.subscription_type == user.report_cadence == "weekly"
    assert user.credit_gift_balance == Decimal("2.77")


def test_daily_acceptance_4_quote_and_dispatch_holdings_gate(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = funded_user(db_session)
    monkeypatch.setattr(
        s, "next_occurrence_for_cadence", lambda *_: datetime(2026, 10, 7, 17, tzinfo=ET)
    )
    assert s.quote(db_session, user.id, date(2026, 10, 7), "daily").needs_holdings is True
    s.set_plan(db_session, user.id, date(2026, 10, 7), "daily")
    assert user.id not in active_user_ids(db_session, "daily")
    db_session.add(
        Holding(
            user_id=user.id,
            name="Cash",
            pricing_mode="manual",
            currency="USD",
            asset_class="CASH_EQUIV",
            current_value=Decimal("100"),
        )
    )
    db_session.flush()
    assert s.quote(db_session, user.id, date(2026, 10, 7), "daily").needs_holdings is False
    assert user.id in active_user_ids(db_session, "daily")
