from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import Principal, current_principal
from app.models.holding import Holding
from app.models.user import User
from app.models.user_investment_context import UserInvestmentContext
from app.schemas.me import MeOut

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

    return MeOut(
        email=user.email,
        delivery_email=user.delivery_email,
        tos_accepted_at=user.tos_accepted_at,
        has_questionnaire=has_questionnaire,
        has_holdings=has_holdings,
        missing=missing,
    )
