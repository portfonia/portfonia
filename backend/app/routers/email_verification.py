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

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.services.altcha_challenge import create_email_verification_challenge
from app.services.email_verification import (
    VerificationRejected,
    confirm_verification,
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
