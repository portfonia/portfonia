"""Public Vigil token endpoints (issue #458). Token in JSON, not cookies."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_session
from app.models.vigil import VigilActionToken
from app.schemas.vigil import VigilPublicConfirmIn, VigilPublicConfirmOut
from app.services.altcha_challenge import create_vigil_challenge, verify_vigil_solution
from app.services.vigil.confirmation_emails import (
    VigilConfirmationEmailPublicError,
    confirm_confirmation_email,
)
from app.services.vigil.crypto import VigilCryptoError
from app.services.vigil.cycles import confirm_cycle_token
from app.services.vigil.drills import VigilPublicTokenError, confirm_drill
from app.services.vigil.tokens import hash_link_token

router = APIRouter()


def _require_public_feature() -> None:
    if get_settings().VIGIL_MODE not in {"active", "recovery"}:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="vigil is not available"
        )


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.get("/public/altcha-challenge")
def get_altcha_challenge(response: Response) -> dict[str, object]:
    _require_public_feature()
    _no_store(response)
    try:
        return create_vigil_challenge()
    except VigilCryptoError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="vigil is not available"
        ) from exc


@router.post("/public/confirm", response_model=VigilPublicConfirmOut)
def post_public_confirm(
    payload: VigilPublicConfirmIn,
    response: Response,
    session: Session = Depends(get_session),
) -> VigilPublicConfirmOut:
    _require_public_feature()
    _no_store(response)
    if not verify_vigil_solution(payload.altcha):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid altcha"
        )
    try:
        peeked = session.scalars(
            select(VigilActionToken).where(
                VigilActionToken.token_hash == hash_link_token(payload.token)
            )
        ).one_or_none()
        if peeked is None:
            raise VigilPublicTokenError(404, "not found")
        if peeked.purpose == "cycle_confirm":
            cycle_result = confirm_cycle_token(session, token=payload.token)
            session.commit()
            return VigilPublicConfirmOut(
                result=cycle_result.result, next_check_at=cycle_result.next_check_at
            )
        if peeked.purpose == "email_verify":
            result = confirm_confirmation_email(session, token=payload.token)
            session.commit()
            public_result = "email_verified" if result == "confirmed" else "email_already_verified"
            return VigilPublicConfirmOut(result=public_result, next_check_at=None)
        result = confirm_drill(session, token=payload.token)
        session.commit()
    except (VigilPublicTokenError, VigilConfirmationEmailPublicError) as exc:
        session.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return VigilPublicConfirmOut(result=result, next_check_at=None)
