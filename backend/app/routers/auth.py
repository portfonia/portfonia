"""User-facing auth endpoints. Invite-gated signup; login is the Auth provider."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_session
from app.core.rate_limit import (
    UNAVAILABLE_DETAIL,
    guard_known_invite_token,
    rate_limit_forgot_password,
    rate_limit_signup,
)
from app.models.user import User
from app.services.altcha_challenge import (
    create_forgot_password_challenge,
    verify_forgot_password_solution,
)
from app.services.auth_provider import (
    AuthProviderError,
    create_auth_user,
    delete_auth_user,
    request_password_reset,
)
from app.services.email_sender import send_ops_alert
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
    # Required true, never defaulted (Ring 1-Onboarding.md §2.5) — Literal[True]
    # rejects both an omitted field and an explicit `false` with one 422,
    # instead of a `= True` default that would silently accept an omission.
    tos_accepted: Literal[True]


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
            # New users default to weekly, not mwf (Ring 1-Onboarding.md §一.6).
            # Multi-cadence Beat/fan-out wiring is a follow-up issue.
            report_cadence="weekly",
            tos_accepted_at=datetime.now(UTC),
        )
        session.add(user)
        session.flush()
        backfill_news_surfaced_before(session, new_id, cold_start_watermark(datetime.now(tz=UTC)))
        session.commit()
    except InviteRejected:
        session.rollback()
        # Expected background noise (especially post-#190 rate limiting) — a
        # distinct tag, not a full traceback, so it doesn't drown out the
        # auth_provider_error/integrity_error alerting below (issue #225).
        # The tag lives in the message itself, not `extra=`: app/main.py's
        # logging.basicConfig format string never interpolates extras (no
        # JSON formatter here), so `extra=` would set a LogRecord attribute
        # that never reaches the actual production log line (review, PR
        # #246 round 1).
        logger.info("signup rejected signup_failure_reason=invite_rejected")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=INVITE_REJECTED_MESSAGE
        ) from None
    except Exception as exc:
        session.rollback()
        if sub is not None:
            try:
                delete_auth_user(sub)
            except AuthProviderError:
                # Compensation itself failed: the Auth user created above is
                # now a real orphan (no matching `users` row will ever be
                # committed for it) with only this log line as a trace unless
                # someone finds it — issue #225's ops alert closes that gap.
                logger.exception("signup compensation: failed to delete auth user")
                send_ops_alert(
                    subject="[Portfonia] signup compensation failed — orphaned Auth user",
                    body=(
                        f"Auth user sub={sub} email={email} was created during signup, "
                        f"a later step failed ({exc!r}), and the compensating "
                        f"delete_auth_user() call also failed. This account is likely "
                        f"orphaned in Supabase Auth with no matching local users row — "
                        # No local `users` row was ever committed (rollback ran above),
                        # so `sub` — not a `users.id` that never existed — is the only
                        # usable identifier for the orphan-purge path (review, PR #246
                        # round 1: this previously emitted a literal "{id}" f-string
                        # escape, an uncopyable URL).
                        f"clean up via DELETE /admin/users/{sub}?confirm={email} "
                        f"(issue #225 orphan-purge path) or the Supabase Dashboard."
                    ),
                    idempotency_key=f"ops-signup-compensation-{sub}",
                )
        if isinstance(exc, AuthProviderError | IntegrityError):
            reason = (
                "auth_provider_error" if isinstance(exc, AuthProviderError) else "integrity_error"
            )
            logger.exception("signup failed signup_failure_reason=%s", reason)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=INVITE_REJECTED_MESSAGE
            ) from None
        raise
    # §4.1 signup hook (issue #262): enqueue only after the try/except above —
    # the account is fully created here, so an enqueue failure must NOT fall
    # into the compensation path (delete_auth_user would destroy it) or fail
    # the signup response (Ring 1-Profile Page.md §8.7). Best-effort: a lost
    # enqueue means the automatic email never goes out, recoverable via the
    # Profile page's resend (§8.3) or the Ops API; the task itself runs on
    # Celery because create_verification makes a synchronous Resend HTTP call
    # (15s timeout) that must never sit on the signup response.
    from app.tasks.email_verification_tasks import send_account_email_verification_task

    try:
        send_account_email_verification_task.delay(str(new_id))
    except Exception:
        logger.exception("signup: failed to enqueue account-email verification for user %s", new_id)
    return SignupResponse(id=user.id, email=user.email)


class ForgotPasswordRequest(BaseModel):
    email: str
    # Base64-encoded Altcha v1 solution payload, from the widget's own
    # hidden form field (default field name "altcha").
    altcha: str


class ForgotPasswordResponse(BaseModel):
    # Deliberate deviation from OWASP ASVS enumeration-resistance guidance,
    # confirmed twice by the product owner (issue #231): unlike a stock
    # Supabase resetPasswordForEmail() integration, this response states
    # plainly whether the account exists rather than returning an identical
    # response either way.
    account_found: bool


@router.get("/altcha-challenge")
def altcha_challenge() -> dict[str, object]:
    """Self-hosted Altcha PoW challenge for the /forgot-password widget.

    Stateless: the challenge signs its own expiry, so nothing is written to
    Redis/DB here — verification in forgot_password() below recomputes it
    from the same HMAC key.
    """
    return create_forgot_password_challenge()


@router.post("/forgot-password", response_model=ForgotPasswordResponse)
def forgot_password(
    req: ForgotPasswordRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> ForgotPasswordResponse:
    """Backend-mediated trigger for Supabase's password-recovery email
    (issue #231). Architecture: verify PoW -> rate limit (IP + email) ->
    look up the LOCAL `users` table (Supabase's own /recover response
    cannot be used for existence, it deliberately looks identical either
    way) -> trigger Supabase's mailer only on a match -> respond with the
    real exists/not-exists answer.

    The consumption side (/reset-password) is client-direct to Supabase,
    same as login — no backend involvement, no PoW. See
    docs/mechanisms/identity-and-auth.md's issue #190 section for why this
    trigger endpoint is the one exception to "login/password-reset limiting
    is hosted Auth, not this issue".
    """
    email = req.email.strip().lower()
    if not verify_forgot_password_solution(req.altcha):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid captcha")
    rate_limit_forgot_password(request, email)
    exists = session.execute(select(User.id).where(User.email == email)).first() is not None
    if exists:
        try:
            request_password_reset(
                email, redirect_to=f"{get_settings().FRONTEND_URL}/reset-password"
            )
        except AuthProviderError:
            logger.exception("forgot-password: failed to trigger Supabase reset email")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNAVAILABLE_DETAIL
            ) from None
    return ForgotPasswordResponse(account_found=exists)
