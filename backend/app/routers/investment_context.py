from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import Principal, current_principal
from app.models.user_investment_context import UserInvestmentContext
from app.schemas.questionnaire import (
    InvestmentContextIn,
    InvestmentContextOut,
)
from app.services.questionnaire_taxonomy import QUESTIONNAIRE_VERSION

router = APIRouter()


@router.get("", response_model=InvestmentContextOut)
def get_investment_context(
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> UserInvestmentContext:
    """The user's own answers only — no system-inferred conclusion, no B1
    basis (§8.4/§1.4). A user with no questionnaire on file gets a 404, not
    an empty/default row: "no answer yet" and "answered with defaults" are
    different states, and only the frontend's own pre-filled defaults (never
    persisted until submit) should stand in for the former."""
    ctx = session.get(UserInvestmentContext, principal.user_id)
    if ctx is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No investment context on file."
        )
    return ctx


@router.put("", response_model=InvestmentContextOut)
def put_investment_context(
    body: InvestmentContextIn,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> UserInvestmentContext:
    """Full overwrite — re-answering the questionnaire replaces the prior
    record wholesale (Concept §4.2), never merges partial fields."""
    ctx = session.get(UserInvestmentContext, principal.user_id)
    questionnaire_dict = body.questionnaire.model_dump()
    if ctx is None:
        ctx = UserInvestmentContext(
            user_id=principal.user_id,
            questionnaire=questionnaire_dict,
            questionnaire_version=QUESTIONNAIRE_VERSION,
            free_text=body.free_text,
        )
        session.add(ctx)
    else:
        ctx.questionnaire = questionnaire_dict
        ctx.questionnaire_version = QUESTIONNAIRE_VERSION
        ctx.free_text = body.free_text
        ctx.updated_at = datetime.now(tz=UTC)
    session.commit()
    session.refresh(ctx)
    return ctx
