from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.dialects.postgresql import insert as pg_insert
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
    record wholesale (Concept §4.2), never merges partial fields.

    Atomic upsert, not read-then-branch (PR #212 review finding): a
    SELECT-then-INSERT-or-UPDATE has a race on two concurrent FIRST
    submissions for the same user — both see no row, both attempt INSERT,
    the loser hits the PK constraint as an unhandled IntegrityError (500)
    instead of succeeding as an overwrite. `INSERT ... ON CONFLICT DO
    UPDATE` is one atomic statement with no such window. `EncryptedString`
    still applies here — it's attached to the column's type at the table
    level, not to the ORM Session API used to reach it.
    """
    questionnaire_dict = body.questionnaire.model_dump()
    now = datetime.now(tz=UTC)
    stmt = (
        pg_insert(UserInvestmentContext)
        .values(
            user_id=principal.user_id,
            questionnaire=questionnaire_dict,
            questionnaire_version=QUESTIONNAIRE_VERSION,
            free_text=body.free_text,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[UserInvestmentContext.user_id],
            set_={
                "questionnaire": questionnaire_dict,
                "questionnaire_version": QUESTIONNAIRE_VERSION,
                "free_text": body.free_text,
                "updated_at": now,
            },
        )
    )
    session.execute(stmt)
    session.commit()
    ctx = session.get(UserInvestmentContext, principal.user_id)
    assert ctx is not None  # just upserted, by definition present
    return ctx
