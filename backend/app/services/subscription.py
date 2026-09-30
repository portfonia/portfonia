"""Subscription state and cadence writer (issue #595)."""

import uuid
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from decimal import ROUND_UP, Decimal

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.credit_ledger import CreditLedgerEntry
from app.models.holding import Holding
from app.models.user import User
from app.schemas.me import SubscriptionOut, SubscriptionQuoteOut
from app.services.credit_ledger import (
    InsufficientCredits,
    consume_credits,
    return_subscription_credits,
)
from app.tasks import next_occurrence_for_cadence

PLAN_FEES: dict[str, Decimal] = {"weekly": Decimal("0.99"), "mwf": Decimal("1.99")}


def next_expiry(from_date: date, anchor_day: int) -> date:
    year = from_date.year + (from_date.month == 12)
    month = from_date.month % 12 + 1
    return date(year, month, min(anchor_day, monthrange(year, month)[1]))


class SubscriptionError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def has_verified_email(user: User) -> bool:
    return user.email_verified_at is not None or user.delivery_email_verified_at is not None


def charge_key(user_id: uuid.UUID, period_start: date, plan: str) -> str:
    return f"subscription:{user_id}:{period_start.isoformat()}:{plan}"


def _lock_user(session: Session, user_id: uuid.UUID, today: date) -> User:
    user = session.execute(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    if user.subscription_adjusted_on == today:
        raise SubscriptionError("daily_limit")
    return user


def _plan_change(
    session: Session, user: User, today: date, plan: str
) -> tuple[str, Decimal, Decimal, str | None]:
    """Select the operation and compute the original charge's return."""
    if user.subscription_status != "active":
        return "subscribe", Decimal("0.00"), Decimal("0.00"), None
    if plan == user.subscription_type and not user.subscription_cancel_pending:
        return "none", Decimal("0.00"), Decimal("0.00"), None
    assert user.subscription_expires_on is not None
    if today > user.subscription_expires_on:
        return "subscribe", Decimal("0.00"), Decimal("0.00"), None
    if plan == user.subscription_type:
        return "resume", Decimal("0.00"), Decimal("0.00"), None
    assert user.subscription_period_start is not None and user.subscription_type is not None
    key = charge_key(user.id, user.subscription_period_start, user.subscription_type)
    rows = session.scalars(
        select(CreditLedgerEntry).where(CreditLedgerEntry.idempotency_key == key)
    ).all()
    cash_paid = abs(sum((r.amount for r in rows if r.bucket == "cash"), Decimal("0.00")))
    gift_paid = abs(sum((r.amount for r in rows if r.bucket == "gift"), Decimal("0.00")))
    paid = cash_paid + gift_paid
    total_days = (user.subscription_expires_on - user.subscription_period_start).days + 1
    unused_days = (user.subscription_expires_on - today).days + 1
    returned = min(
        paid, (paid * unused_days / total_days).quantize(Decimal("0.01"), rounding=ROUND_UP)
    )
    cash = min(returned, cash_paid)
    return "change", cash, returned - cash, key


def set_plan(session: Session, user_id: uuid.UUID, today: date, plan: str) -> User:
    user = _lock_user(session, user_id, today)
    if not has_verified_email(user):
        raise SubscriptionError("email_unverified")
    action, cash, gift, old_key = _plan_change(session, user, today, plan)
    if action == "none":
        raise SubscriptionError("no_change")
    if action == "resume":
        user.subscription_cancel_pending = False
    else:
        if user.credit_cash_balance + user.credit_gift_balance + cash + gift < PLAN_FEES[plan]:
            raise SubscriptionError("insufficient_credits")
        if old_key is not None:
            return_subscription_credits(
                session,
                user_id=user.id,
                cash=cash,
                gift=gift,
                idempotency_key=f"subscription_return:{old_key}",
                reference=old_key,
            )
        try:
            consume_credits(
                session,
                user_id=user.id,
                amount=PLAN_FEES[plan],
                reason="subscription",
                idempotency_key=charge_key(user.id, today, plan),
                reference=plan,
            )
        except InsufficientCredits:
            raise SubscriptionError("insufficient_credits") from None
        user.subscription_status = "active"
        user.subscription_type = user.report_cadence = plan
        user.subscription_period_start = today
        user.subscription_anchor_day = today.day
        user.subscription_expires_on = next_expiry(today, today.day)
        user.subscription_cancel_pending = False
    user.subscription_adjusted_on = today
    session.flush()
    return user


def cancel(session: Session, user_id: uuid.UUID, today: date) -> User:
    user = _lock_user(session, user_id, today)
    if user.subscription_status != "active":
        raise SubscriptionError("no_subscription")
    if user.subscription_cancel_pending:
        raise SubscriptionError("no_change")
    user.subscription_cancel_pending = True
    user.subscription_adjusted_on = today
    session.flush()
    return user


def resume(session: Session, user_id: uuid.UUID, today: date) -> User:
    user = _lock_user(session, user_id, today)
    if user.subscription_status != "active" or (
        user.subscription_expires_on is not None and today > user.subscription_expires_on
    ):
        raise SubscriptionError("no_subscription")
    if not user.subscription_cancel_pending:
        raise SubscriptionError("no_change")
    if not has_verified_email(user):
        raise SubscriptionError("email_unverified")
    user.subscription_cancel_pending = False
    user.subscription_adjusted_on = today
    session.flush()
    return user


def cancel_for_no_verified_email(user: User) -> None:
    if user.subscription_status == "active" and not user.subscription_cancel_pending:
        user.subscription_cancel_pending = True


def summary(user: User, today: date) -> SubscriptionOut:
    next_adjustment = (
        datetime.combine(today + timedelta(days=1), time.min, tzinfo=ET)
        if user.subscription_adjusted_on == today
        else None
    )
    return SubscriptionOut(
        status=user.subscription_status,
        type=user.subscription_type,
        expires_on=user.subscription_expires_on,
        cancel_pending=user.subscription_cancel_pending,
        next_adjustment_at=next_adjustment,
    )


def quote(session: Session, user_id: uuid.UUID, today: date, plan: str) -> SubscriptionQuoteOut:
    user = session.get(User, user_id)
    assert user is not None
    action, cash, gift, _ = _plan_change(session, user, today, plan)
    balance = user.credit_cash_balance + user.credit_gift_balance
    returned = cash + gift
    charged = PLAN_FEES[plan] if action in ("subscribe", "change") else Decimal("0.00")
    blocked = (
        "daily_limit"
        if user.subscription_adjusted_on == today
        else ("email_unverified" if not has_verified_email(user) else None)
    )
    has_holdings = session.scalar(select(exists().where(Holding.user_id == user_id)))
    return SubscriptionQuoteOut(
        action=action,
        type=plan,
        fee=f"{PLAN_FEES[plan]:.2f}",
        returned=f"{returned:.2f}",
        balance=f"{balance:.2f}",
        balance_after=f"{balance + returned - charged:.2f}",
        sufficient=balance + returned >= charged,
        period_start=today if action in ("subscribe", "change") else user.subscription_period_start,
        expires_on=next_expiry(today, today.day)
        if action in ("subscribe", "change")
        else user.subscription_expires_on,
        first_report_at=next_occurrence_for_cadence(plan, datetime.now(ET)),
        needs_holdings=plan == "mwf" and not has_holdings,
        blocked=blocked,
    )
