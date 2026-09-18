"""Public Vigil token endpoints (issue #458). Token in JSON, not cookies."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_session
from app.models.vigil import VigilActionToken
from app.schemas.vigil import (
    VigilPublicConfirmIn,
    VigilPublicConfirmOut,
    VigilPublicStatusIn,
    VigilPublicStatusOut,
)
from app.services.altcha_challenge import create_vigil_challenge, verify_vigil_solution
from app.services.vigil.crypto import VigilCryptoError
from app.services.vigil.cycles import confirm_cycle_token
from app.services.vigil.drills import VigilPublicTokenError, confirm_drill, public_status
from app.services.vigil.tokens import hash_link_token

router = APIRouter()


def _require_public_origin(request: Request) -> None:
    expected = get_settings().FRONTEND_URL.rstrip("/")
    origin = request.headers.get("origin")
    if origin != expected:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")


def _require_public_feature() -> None:
    if get_settings().VIGIL_MODE not in {"active", "recovery"}:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="vigil is not available"
        )


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.get("/public/altcha-challenge")
def get_altcha_challenge(request: Request, response: Response) -> dict[str, object]:
    _require_public_origin(request)
    _require_public_feature()
    _no_store(response)
    try:
        return create_vigil_challenge()
    except VigilCryptoError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="vigil is not available"
        ) from exc


@router.get("/public/status", response_model=VigilPublicStatusOut)
def get_public_status(
    request: Request,
    response: Response,
    token: str,
    session: Session = Depends(get_session),
) -> VigilPublicStatusOut:
    _require_public_origin(request)
    _require_public_feature()
    _no_store(response)
    try:
        result = public_status(session, token=token, action=None, mint_nonce=False)
    except VigilPublicTokenError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return VigilPublicStatusOut(available=result.available)


@router.post("/public/status", response_model=VigilPublicStatusOut)
def post_public_status(
    payload: VigilPublicStatusIn,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> VigilPublicStatusOut:
    _require_public_origin(request)
    _require_public_feature()
    _no_store(response)
    try:
        result = public_status(session, token=payload.token, action=payload.action, mint_nonce=True)
    except VigilPublicTokenError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except VigilCryptoError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="vigil is not available"
        ) from exc
    return VigilPublicStatusOut(
        available=result.available, nonce=result.nonce, expires_at=result.expires_at
    )


@router.post("/public/confirm", response_model=VigilPublicConfirmOut)
def post_public_confirm(
    payload: VigilPublicConfirmIn,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> VigilPublicConfirmOut:
    _require_public_origin(request)
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
            cycle_result = confirm_cycle_token(session, token=payload.token, nonce=payload.nonce)
            session.commit()
            return VigilPublicConfirmOut(
                result=cycle_result.result, next_check_at=cycle_result.next_check_at
            )
        result = confirm_drill(session, token=payload.token, nonce=payload.nonce)
        session.commit()
    except VigilPublicTokenError as exc:
        session.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return VigilPublicConfirmOut(result=result, next_check_at=None)
