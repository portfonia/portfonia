"""User-facing auth endpoints. Invite-gated signup; login is the Auth provider."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.rate_limit import guard_known_invite_token, rate_limit_signup
from app.models.user import User
from app.services.auth_provider import AuthProviderError, create_auth_user, delete_auth_user
from app.services.invites import (
    INVITE_REJECTED_MESSAGE,
    InviteRejected,
    redeem_invite,
    signup_email_taken,
)
from app.services.window_data import backfill_news_surfaced_before, cold_start_watermark

logger = logging.getLogger(__name__)

router = APIRouter()


class SignupRequest(BaseModel):
    invite_token: str
    email: str
    password: SecretStr = Field(min_length=8)


class SignupResponse(BaseModel):
    id: uuid.UUID
    email: str


@router.post("/signup", response_model=SignupResponse, status_code=status.HTTP_201_CREATED)
def signup(
    req: SignupRequest,
    session: Session = Depends(get_session),
    _: None = Depends(rate_limit_signup),
) -> SignupResponse:
    """Invite-gated registration. Password is forwarded to the Auth provider
    in memory and is never logged or persisted.
    """
    email = req.email.strip().lower()
    new_id = uuid.uuid4()
    sub: str | None = None
    try:
        guard_known_invite_token(session, req.invite_token)
        if signup_email_taken(session, email):
            raise InviteRejected(INVITE_REJECTED_MESSAGE)
        redeem_invite(session, req.invite_token, used_by=new_id, email=email)
        sub = create_auth_user(email, req.password.get_secret_value())
        user = User(
            id=new_id,
            auth_provider="supabase",
            auth_subject=sub,
            email=email,
            status="active",
            locale="zh",
            base_currency="USD",
            report_cadence="mwf",
        )
        session.add(user)
        session.flush()
        backfill_news_surfaced_before(session, new_id, cold_start_watermark(datetime.now(tz=UTC)))
        session.commit()
    except InviteRejected:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=INVITE_REJECTED_MESSAGE
        ) from None
    except Exception as exc:
        session.rollback()
        if sub is not None:
            try:
                delete_auth_user(sub)
            except AuthProviderError:
                logger.exception("signup compensation: failed to delete auth user")
        if isinstance(exc, AuthProviderError | IntegrityError):
            logger.exception("signup failed")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=INVITE_REJECTED_MESSAGE
            ) from None
        raise
    return SignupResponse(id=user.id, email=user.email)
