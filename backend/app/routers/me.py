from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, field_validator
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import Principal, current_principal
from app.core.timezones import ET, today_et
from app.models.email_verification import EmailVerification
from app.models.holding import Holding
from app.models.user import User
from app.models.user_investment_context import UserInvestmentContext
from app.schemas.holdings import VALID_CURRENCIES
from app.schemas.me import (
    MeOut,
    PendingVerificationOut,
    SubscriptionBody,
    SubscriptionOut,
    SubscriptionQuoteOut,
)
from app.services import credit_ledger, subscription
from app.services.altcha_challenge import (
    create_account_deletion_challenge,
    create_change_password_challenge,
    verify_account_deletion_solution,
    verify_change_password_solution,
)
from app.services.auth_provider import AuthProviderError, delete_auth_user
from app.services.email_sender import send_ops_alert
from app.services.report_currency import apply_report_currency_change
from app.services.user_purge import _normalize_email, purge_user, refuse_protected_user

router = APIRouter()


@router.get("", response_model=MeOut)
def get_me(
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> MeOut:
    """Account summary for the Profile page (issue #220), full #221 shape.

    `missing` only ever lists "questionnaire"/"holdings" — `tos_accepted_at`
    is audit-only and never turns into a gap entry (Ring 1-Onboarding.md
    §2.6: existing NULL users get no re-accept flow). This PR's Profile page
    does not render `missing` as a gap card yet; that's #221.
    """
    user = session.get(User, principal.user_id)
    assert user is not None  # current_principal already required this row

    has_questionnaire = session.execute(
        select(exists().where(UserInvestmentContext.user_id == principal.user_id))
    ).scalar_one()
    has_holdings = session.execute(
        select(exists().where(Holding.user_id == principal.user_id))
    ).scalar_one()

    missing: list[str] = []
    if not has_questionnaire:
        missing.append("questionnaire")
    if not has_holdings:
        missing.append("holdings")

    # issue #262 §8.2: actionable verification rows for the Profile page.
    # "undeliverable" is listed alongside "pending" — a typo'd address that
    # bounced would otherwise look like "nothing waiting" (Profile Page.md
    # §8.2, 2026-08-30 production-testing gap). expired/superseded/verified
    # are history, not actionable, and stay off the list.
    pending_verifications = (
        session.execute(
            select(EmailVerification)
            .where(
                EmailVerification.user_id == principal.user_id,
                EmailVerification.purpose.in_(["account_email", "delivery_email"]),
                EmailVerification.status.in_(["pending", "undeliverable"]),
            )
            .order_by(EmailVerification.last_sent_at.desc())
        )
        .scalars()
        .all()
    )

    return MeOut(
        email=user.email,
        credit_balance=user.credit_cash_balance + user.credit_gift_balance,
        subscription=subscription.summary(user, today_et()),
        delivery_email=user.delivery_email,
        email_verified_at=user.email_verified_at,
        delivery_email_verified_at=user.delivery_email_verified_at,
        tos_accepted_at=user.tos_accepted_at,
        has_questionnaire=has_questionnaire,
        has_holdings=has_holdings,
        missing=missing,
        pending_email_verifications=[
            PendingVerificationOut(
                id=str(record.id),
                purpose=record.purpose,
                email=record.email,
                status=record.status,
                expires_at=record.expires_at,
                last_sent_at=record.last_sent_at,
            )
            for record in pending_verifications
        ],
        report_language=user.locale,
        report_currency=user.base_currency,
    )


class UpdateReportLanguageBody(BaseModel):
    # Literal, not a bare str + DB CheckConstraint fallback: a bad value gets
    # a clean 422 here rather than an IntegrityError bubbling into a 500 —
    # same discipline as admin.py's UpdateCadenceBody. Keep in sync with
    # app.models.user.VALID_REPORT_LANGUAGES by hand; Pydantic Literal
    # members must be compile-time, not derived from that tuple.
    report_language: Literal["en", "zh", "zh-Hant"]


class UpdateReportLanguageOut(BaseModel):
    report_language: str


@router.patch("/report-language", response_model=UpdateReportLanguageOut)
def update_report_language(
    body: UpdateReportLanguageBody,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> UpdateReportLanguageOut:
    """Self-service write of the caller's own report language (issue #308).

    Writes users.locale for the caller's own row only — no rate limiting
    (a plain authenticated write with no external side effect and no abuse
    surface, unlike the email-verification endpoints).
    """
    user = session.get(User, principal.user_id)
    assert user is not None  # current_principal already required this row
    user.locale = body.report_language
    session.commit()
    return UpdateReportLanguageOut(report_language=user.locale)


class UpdateReportCurrencyBody(BaseModel):
    # Not a Literal (unlike UpdateReportLanguageBody's 2-value report_
    # language): VALID_CURRENCIES has 15 members and is the actual
    # cross-checked source of truth used elsewhere for this field (e.g.
    # app/schemas/holdings.py's currency validator) — checking membership
    # against it directly here avoids adding a THIRD hand-kept 15-value
    # whitelist that could drift from the other two.
    report_currency: str

    @field_validator("report_currency")
    @classmethod
    def _validate_report_currency(cls, v: str) -> str:
        if v not in VALID_CURRENCIES:
            raise ValueError(f"unrecognized currency {v!r} — not in VALID_CURRENCIES")
        return v


class UpdateReportCurrencyOut(BaseModel):
    report_currency: str


@router.patch("/report-currency", response_model=UpdateReportCurrencyOut)
def update_report_currency(
    body: UpdateReportCurrencyBody,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> UpdateReportCurrencyOut:
    """Self-service write of the caller's own report/base currency (issue
    #350 item 1) — same shape as update_report_language above: writes
    users.base_currency for the caller's own row only, no rate limiting.

    A real change also appends `report_currency_changes` (issue #372).
    Historical `portfolio_value_snapshots.base_currency` is not rewritten.
    """
    user = session.get(User, principal.user_id)
    assert user is not None  # current_principal already required this row
    apply_report_currency_change(
        session,
        user,
        body.report_currency,
        source="self",
        actor_user_id=user.id,
    )
    session.commit()
    return UpdateReportCurrencyOut(report_currency=user.base_currency)


@router.get("/change-password/altcha-challenge")
def change_password_altcha_challenge(
    _principal: Principal = Depends(current_principal),
) -> dict[str, object]:
    """Self-hosted Altcha PoW challenge for the authenticated
    `/profile/change-password` widget (issue #393).

    Authed because the page is session-only; proxy.ts injects the Bearer
    token on the browser GET through `/api/me/...`. Stateless: the
    challenge signs its own expiry, same as GET /auth/altcha-challenge.
    """
    return create_change_password_challenge()


class ChangePasswordAltchaVerifyBody(BaseModel):
    # Base64-encoded Altcha v1 solution payload, from the widget's own
    # hidden form field (default field name "altcha").
    altcha: str


@router.post("/change-password/altcha-verify", status_code=status.HTTP_204_NO_CONTENT)
def verify_change_password_altcha(
    body: ChangePasswordAltchaVerifyBody,
    _principal: Principal = Depends(current_principal),
) -> Response:
    """Verify a solved change-password PoW payload. The Next.js server
    action calls this before talking to the Auth provider — a False
    result must not proceed to `updateUser`.
    """
    if not verify_change_password_solution(body.altcha):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid captcha")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/subscription/quote", response_model=SubscriptionQuoteOut)
def subscription_quote(
    type: Literal["weekly", "mwf", "daily"],
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> SubscriptionQuoteOut:
    return subscription.quote(session, principal.user_id, today_et(), type)


@router.post("/subscription", response_model=SubscriptionOut)
def set_subscription(
    body: SubscriptionBody,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> SubscriptionOut:
    today = today_et()
    try:
        user = subscription.set_plan(session, principal.user_id, today, body.type)
    except subscription.SubscriptionError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=exc.code) from None
    session.commit()
    subscription.maybe_send_low_balance_reminder(session, principal.user_id)
    return subscription.summary(user, today)


@router.post("/subscription/cancel", response_model=SubscriptionOut)
def cancel_subscription(
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> SubscriptionOut:
    today = today_et()
    try:
        user = subscription.cancel(session, principal.user_id, today)
    except subscription.SubscriptionError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=exc.code) from None
    session.commit()
    return subscription.summary(user, today)


@router.post("/subscription/resume", response_model=SubscriptionOut)
def resume_subscription(
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> SubscriptionOut:
    today = today_et()
    try:
        user = subscription.resume(session, principal.user_id, today)
    except subscription.SubscriptionError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=exc.code) from None
    session.commit()
    return subscription.summary(user, today)


class AccountDeletionOut(BaseModel):
    cash_balance: str
    refundable_cash: str
    gift_balance: str
    subscription_active: bool


class AccountDeletionBody(BaseModel):
    confirm_email: str
    relinquish_cash: Decimal = Decimal("0.00")
    altcha: str | None = None


@router.get("/account-deletion", response_model=AccountDeletionOut)
def account_deletion_summary(
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> AccountDeletionOut:
    """Dialog disclosure; the POST rechecks the balance under the ledger lock."""
    user = session.get(User, principal.user_id)
    assert user is not None
    return AccountDeletionOut(
        cash_balance=f"{user.credit_cash_balance:.2f}",
        refundable_cash=f"{credit_ledger.refundable_cash(session, user.id, datetime.now(ET)):.2f}",
        gift_balance=f"{user.credit_gift_balance:.2f}",
        subscription_active=user.subscription_status == "active",
    )


@router.get("/account-deletion/altcha-challenge")
def account_deletion_challenge(
    _principal: Principal = Depends(current_principal),
) -> dict[str, object]:
    return create_account_deletion_challenge()


@router.post("/account-deletion", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    body: AccountDeletionBody,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> Response:
    """Relinquish and purge in one transaction, then delete Auth before commit."""
    user = credit_ledger._lock_user(session, principal.user_id)
    refuse_protected_user(session, user)
    if _normalize_email(body.confirm_email) != _normalize_email(user.email):
        raise HTTPException(status_code=409, detail="confirm does not match account email")
    if body.relinquish_cash != user.credit_cash_balance:
        raise HTTPException(status_code=409, detail="balance_changed")
    user_id, auth_subject = user.id, user.auth_subject
    if user.credit_cash_balance > 0:
        if not verify_account_deletion_solution(body.altcha):
            raise HTTPException(status_code=400, detail="invalid captcha")
        credit_ledger.relinquish_cash(
            session,
            user,
            amount=user.credit_cash_balance,
            refundable_part=credit_ledger.refundable_cash(session, user.id, datetime.now(ET)),
        )
        session.flush()
    purge_user(session, user_id)
    session.flush()
    if auth_subject is not None:
        try:
            delete_auth_user(auth_subject)
        except AuthProviderError:
            session.rollback()
            raise HTTPException(
                status_code=502, detail="failed to delete account; nothing was changed, retry"
            ) from None
    try:
        session.commit()
    except Exception:
        session.rollback()
        logging.getLogger(__name__).error("self-deletion commit failed; user_id=%s", user_id)
        if auth_subject is not None:
            send_ops_alert("self-deletion commit failed after Auth delete", str(user_id))
        raise HTTPException(
            status_code=500, detail="failed to delete account; contact support"
        ) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
