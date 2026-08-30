"""Public confirm-flow endpoints for the generic email-verification
mechanism (Ring 1-Email Validation design doc, issue #260).

Unauthenticated by design — the token itself is the credential, same as
Vigil's confirm-page precedent and this project's own /reset-password
(client-direct to Supabase, no session). No rate limiting is wired here yet:
the only caller that can CREATE a record is the Ops API (already gated by
ADMIN_API_TOKEN — see app/routers/admin.py), so there is no untrusted-facing
surface yet that could flood this router with garbage tokens to guess
against (the token is 128 bits regardless).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import Principal, current_principal
from app.core.rate_limit import rate_limit_enforce_resend_verification
from app.models.email_verification import EmailVerification
from app.services.altcha_challenge import create_email_verification_challenge
from app.services.email_verification import (
    ResendTooSoon,
    VerificationRejected,
    VerificationSendFailed,
    confirm_verification,
    create_verification,
    get_verification_status,
)

router = APIRouter()


@router.get("/altcha-challenge")
def altcha_challenge() -> dict[str, object]:
    """Self-hosted Altcha PoW challenge for the /verify-email page's widget.
    Stateless, same design as GET /auth/altcha-challenge."""
    return create_email_verification_challenge()


class StatusResponse(BaseModel):
    found: bool
    status: str | None
    email: str | None


@router.get("/status", response_model=StatusResponse)
def status_lookup(token: str, session: Session = Depends(get_session)) -> StatusResponse:
    """Inert lookup for the confirm page's initial render (design doc §3.3
    step 2) — never writes. `found=False` covers both "no such token" and
    "malformed token"; the page renders the same generic message either way.
    """
    result = get_verification_status(session, token=token)
    return StatusResponse(found=result.found, status=result.status, email=result.email)


class ConfirmRequest(BaseModel):
    token: str
    altcha: str


class ConfirmResponse(BaseModel):
    email: str


@router.post("/confirm", response_model=ConfirmResponse)
def confirm(req: ConfirmRequest, session: Session = Depends(get_session)) -> ConfirmResponse:
    """The one state-changing step (design doc §3.3 step 4). Any failure
    (bad token, expired, already used, failed Altcha) returns the same 400
    detail — never distinguishes which check failed."""
    try:
        record = confirm_verification(session, token=req.token, altcha_payload=req.altcha)
    except VerificationRejected as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return ConfirmResponse(email=record.email)


class ResendResponse(BaseModel):
    """Narrow create-ack shape, same as the Ops POST — never the plaintext
    token. The id is the NEW record's: resend supersedes the old row, so
    the requested id is no longer the live one (Profile Page.md §8.4 — the
    frontend re-fetches GET /me rather than patching the old id)."""

    id: uuid.UUID
    status: str
    expires_at: datetime


@router.post("/{verification_id}/resend", response_model=ResendResponse)
def resend_verification(
    verification_id: uuid.UUID,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> ResendResponse:
    """Resend the caller's own pending/undeliverable verification email
    (issue #262, Ring 1-Profile Page.md §8.3). Ownership and status are
    checked first; anything else — missing, someone else's, terminal
    status, unbound ops_manual — is the same 404, never 403: the response
    must not reveal that an id exists but belongs to someone else.

    Rate limiting lives HERE, not in create_verification: the Redis
    per-user + per-address buckets guard this untrusted-facing session
    endpoint, while the shared service's own 60s data-driven cooldown
    keeps serving the ADMIN_API_TOKEN-gated Ops path unchanged — two
    call surfaces, two different abuse profiles, deliberately separate
    mechanisms (Profile Page.md §8.3)."""
    record = session.get(EmailVerification, verification_id)
    if (
        record is None
        or record.user_id != principal.user_id
        or record.status not in ("pending", "undeliverable")
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="verification not found")

    rate_limit_enforce_resend_verification(user_id=str(principal.user_id), email=record.email)

    try:
        new_record = create_verification(
            session, email=record.email, purpose=record.purpose, user_id=principal.user_id
        )
    except ResendTooSoon:
        # Scope-accurate wording (PR #263 review, mirroring the Ops router's
        # round-4 fix): this endpoint's calls are always bound, so the
        # cooldown scope is (user_id, purpose), not the address — a prior
        # send to a DIFFERENT address for the same user+purpose also trips.
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="a verification for this user and purpose was already sent less than 60s ago",
        ) from None
    except VerificationSendFailed:
        # Nothing local was touched (send-first ordering) — safe to retry.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="failed to send the verification email; no local data was touched, retry",
        ) from None
    return ResendResponse(
        id=new_record.id, status=new_record.status, expires_at=new_record.expires_at
    )
