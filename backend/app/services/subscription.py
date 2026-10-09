"""Subscription state and cadence writer (issue #595)."""

import logging
import uuid
from calendar import monthrange
from dataclasses import dataclass
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
    referral_subscription_bonus,
    return_subscription_credits,
)
from app.services.email_sender import send_ops_alert, send_subscription_notice
from app.services.user_directory import recipient_email_with_purpose
from app.services.user_scope import HOLDINGS_GATED_CADENCES
from app.tasks import next_occurrence_for_cadence

logger = logging.getLogger(__name__)

PLAN_FEES: dict[str, Decimal] = {
    "weekly": Decimal("0.99"),
    "mwf": Decimal("1.99"),
    "daily": Decimal("2.49"),
    "jade": Decimal("9.99"),
}
BRIEFING_PLANS: tuple[str, ...] = ("weekly", "mwf", "daily")
ADVANCED_SUBSCRIPTION_TYPES: tuple[str, ...] = ("daily", "jade")


def is_advanced(user: User) -> bool:
    return (
        user.subscription_status == "active"
        and user.subscription_type in ADVANCED_SUBSCRIPTION_TYPES
    )


def is_jade(user: User) -> bool:
    return user.subscription_status == "active" and user.subscription_type == "jade"


def resulting_cadence(user: User, plan: str) -> str:
    if plan != "jade":
        return plan
    if user.report_cadence in BRIEFING_PLANS and (
        user.subscription_status == "active" or user.subscription_type == "jade"
    ):
        return user.report_cadence
    return "daily"


def set_jade_cadence(session: Session, user_id: uuid.UUID, cadence: str) -> User:
    user = session.execute(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    if not is_jade(user):
        raise SubscriptionError("not_jade")
    user.report_cadence = cadence
    session.flush()
    return user


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
    if is_jade(user) and plan != "jade":
        raise SubscriptionError("jade_managed")
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
            _fresh_subscribe(session, user, today, plan)
        except InsufficientCredits:
            raise SubscriptionError("insufficient_credits") from None
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
        cadence=user.report_cadence,
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
    cadence = resulting_cadence(user, plan)
    has_holdings = session.scalar(select(exists().where(Holding.user_id == user_id)))
    return SubscriptionQuoteOut(
        cadence=cadence,
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
        first_report_at=next_occurrence_for_cadence(cadence, datetime.now(ET)),
        needs_holdings=cadence in HOLDINGS_GATED_CADENCES and not has_holdings,
        blocked=blocked,
    )


def _fresh_subscribe(session: Session, user: User, today: date, plan: str) -> None:
    """Charge before editing the row refreshed by the ledger lock."""
    consume_credits(
        session,
        user_id=user.id,
        amount=PLAN_FEES[plan],
        reason="subscription",
        idempotency_key=charge_key(user.id, today, plan),
        reference=plan,
    )
    referral_subscription_bonus(session, user)
    cadence = resulting_cadence(user, plan)
    user.subscription_status = "active"
    user.subscription_type = plan
    user.report_cadence = cadence
    user.subscription_period_start = today
    user.subscription_anchor_day = today.day
    user.subscription_expires_on = next_expiry(today, today.day)
    user.subscription_cancel_pending = False


def _send_notice(session: Session, user: User, kind: str) -> None:
    recipient = recipient_email_with_purpose(session, user.id)
    if recipient is not None:
        assert user.subscription_type is not None and user.subscription_expires_on is not None
        send_subscription_notice(
            recipient[0],
            kind,
            locale=user.locale,
            plan=user.subscription_type,
            expires_on=user.subscription_expires_on,
            fee=PLAN_FEES[user.subscription_type],
            balance=user.credit_cash_balance + user.credit_gift_balance,
        )


def maybe_send_low_balance_reminder(session: Session, user_id: uuid.UUID) -> None:
    user = session.get(User, user_id)
    if (
        user is not None
        and user.subscription_status == "active"
        and not user.subscription_cancel_pending
        and user.subscription_type is not None
        and user.credit_cash_balance + user.credit_gift_balance < PLAN_FEES[user.subscription_type]
    ):
        _send_notice(session, user, "low_balance")


@dataclass(frozen=True)
class CheckOutcome:
    user_id: uuid.UUID
    outcome: str


def _check_user(session: Session, user: User, today: date) -> str:
    if user.subscription_status == "active":
        assert user.subscription_expires_on is not None
        if today <= user.subscription_expires_on:
            return "unchanged"
        if user.subscription_cancel_pending or not has_verified_email(user):
            user.subscription_status = "cancelled"
            user.subscription_type = None
            user.report_cadence = "none"
            user.subscription_cancel_pending = False
            return "cancelled"
    elif user.subscription_status == "expired":
        if not has_verified_email(user):
            return "unchanged"
        assert user.subscription_type is not None
        if user.credit_cash_balance + user.credit_gift_balance < PLAN_FEES[user.subscription_type]:
            return "unchanged"
    else:
        return "unchanged"
    assert user.subscription_type is not None
    plan = user.subscription_type
    try:
        if user.subscription_status == "active":
            assert (
                user.subscription_expires_on is not None
                and user.subscription_anchor_day is not None
            )
            old_expiry = user.subscription_expires_on
            expiry = next_expiry(old_expiry, user.subscription_anchor_day)
            if expiry >= today:
                consume_credits(
                    session,
                    user_id=user.id,
                    amount=PLAN_FEES[plan],
                    reason="subscription",
                    idempotency_key=charge_key(user.id, old_expiry, plan),
                    reference=plan,
                )
                user.subscription_period_start = old_expiry
                user.subscription_expires_on = expiry
            else:
                _fresh_subscribe(session, user, today, plan)
            return "renewed"
        _fresh_subscribe(session, user, today, plan)
        return "resumed"
    except InsufficientCredits:
        user.subscription_status = "expired"
        return "expired"


def run_cadence_checks(
    session: Session,
    cadence: str,
    today: date,
    *,
    statuses: tuple[str, ...] = ("active", "expired"),
) -> list[CheckOutcome]:
    ids = sorted(
        session.scalars(
            select(User.id).where(
                User.status == "active",
                User.report_cadence == cadence,
                User.subscription_status.in_(statuses),
            )
        ).all()
    )
    outcomes = []
    for user_id in ids:
        try:
            user = session.execute(
                select(User)
                .where(User.id == user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).scalar_one()
            outcome = _check_user(session, user, today)
            session.commit()
            outcomes.append(CheckOutcome(user_id, outcome))
            if outcome == "expired":
                _send_notice(session, user, "expired")
            elif outcome in ("renewed", "resumed"):
                maybe_send_low_balance_reminder(session, user_id)
        except Exception as exc:
            session.rollback()
            logger.exception("Subscription check failed for user %s", user_id)
            send_ops_alert(
                "Subscription check failed", f"user={user_id} error={type(exc).__name__}: {exc}"
            )
            outcomes.append(CheckOutcome(user_id, "failed"))
    return outcomes


def activate_existing(session: Session, user: User, today: date) -> str:
    user = session.execute(
        select(User)
        .where(User.id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    if user.subscription_status != "inactive":
        return "skipped"
    if user.status == "active" and has_verified_email(user) and user.report_cadence in PLAN_FEES:
        try:
            _fresh_subscribe(session, user, today, user.report_cadence)
            return "activated"
        except InsufficientCredits:
            user.report_cadence = "none"
            return "insufficient"
    user.report_cadence = "none"
    return "inactive"
