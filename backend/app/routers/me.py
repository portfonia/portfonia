from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import Principal, current_principal
from app.models.email_verification import EmailVerification
from app.models.holding import Holding
from app.models.user import User
from app.models.user_investment_context import UserInvestmentContext
from app.schemas.me import MeOut, PendingVerificationOut

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
    )


class UpdateReportLanguageBody(BaseModel):
    # Literal, not a bare str + DB CheckConstraint fallback: a bad value gets
    # a clean 422 here rather than an IntegrityError bubbling into a 500 —
    # same discipline as admin.py's UpdateCadenceBody. Keep in sync with
    # app.models.user.VALID_REPORT_LANGUAGES by hand; Pydantic Literal
    # members must be compile-time, not derived from that tuple.
    report_language: Literal["en", "zh"]


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
