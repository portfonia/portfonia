"""Public confirm-flow endpoints for report-email unsubscribe (issue #257,
Ring 1-Email Validation design doc §3.7).

Unauthenticated by design — the HMAC-signed token itself is the credential,
same as /email-verifications. No Altcha: unsubscribe does not send mail
and the only protection needed against gateway prefetch is an inert GET
plus an explicit-click POST (design doc §3.7 step 4).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.services import user_activity
from app.services.unsubscribe import (
    UNSUBSCRIBE_REJECTED_MESSAGE,
    UnsubscribeRejected,
    confirm_unsubscribe,
    get_unsubscribe_status,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class StatusResponse(BaseModel):
    found: bool
    email: str | None


@router.get("/status", response_model=StatusResponse)
def status_lookup(token: str) -> StatusResponse:
    """Inert lookup for the confirm page's initial render — never writes."""
    result = get_unsubscribe_status(token)
    return StatusResponse(found=result.found, email=result.email)


class ConfirmRequest(BaseModel):
    token: str


class ConfirmResponse(BaseModel):
    email: str


@router.post("/confirm", response_model=ConfirmResponse)
def confirm(
    req: ConfirmRequest, request: Request, session: Session = Depends(get_session)
) -> ConfirmResponse:
    try:
        claims = confirm_unsubscribe(session, token=req.token)
    except UnsubscribeRejected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=UNSUBSCRIBE_REJECTED_MESSAGE
        ) from None
    user_activity.record(
        session,
        claims.user_id,
        "email",
        {"action": "unsubscribed", "purpose": claims.purpose},
        request,
    )
    try:
        session.commit()
    except Exception:
        logger.error("Account activity commit failed")
        session.rollback()
    return ConfirmResponse(email=claims.email)
