"""Session-only personal API token settings."""

from datetime import date, datetime, time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import Principal, current_principal
from app.core.timezones import ET, today_et
from app.models.api_token import ApiToken
from app.models.user import User
from app.services.api_tokens import new_token, now_et, token_status

router = APIRouter(dependencies=[Depends(current_principal)])


class TokenBody(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
    expires_on: date | None = None


class TokenOut(BaseModel):
    id: UUID
    name: str
    prefix: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    status: str


class CreatedTokenOut(BaseModel):
    token: str
    id: UUID
    name: str
    prefix: str
    created_at: datetime
    expires_at: datetime | None


@router.get("/api-tokens", response_model=list[TokenOut])
def list_tokens(
    principal: Principal = Depends(current_principal), session: Session = Depends(get_session)
) -> list[TokenOut]:
    now = now_et()
    rows = session.scalars(
        select(ApiToken)
        .where(ApiToken.user_id == principal.user_id, ApiToken.revoked_at.is_(None))
        .order_by(ApiToken.created_at.desc(), ApiToken.id.desc())
    )
    return [
        TokenOut(
            id=r.id,
            name=r.name,
            prefix=r.token_prefix,
            created_at=r.created_at,
            expires_at=r.expires_at,
            last_used_at=r.last_used_at,
            status=token_status(r, now),
        )
        for r in rows
    ]


@router.post("/api-tokens", response_model=CreatedTokenOut, status_code=201)
def create_token(
    body: TokenBody,
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> CreatedTokenOut:
    if body.expires_on is not None and body.expires_on <= today_et():
        raise HTTPException(422, "expiry must be after today ET")
    # Serialize creation for this user so concurrent requests cannot exceed five.
    session.scalar(select(User).where(User.id == principal.user_id).with_for_update())
    now = now_et()
    rows = session.scalars(
        select(ApiToken).where(ApiToken.user_id == principal.user_id, ApiToken.revoked_at.is_(None))
    )
    if sum(token_status(r, now) == "active" for r in rows) >= 5:
        raise HTTPException(409, "token_limit")
    expires = (
        datetime.combine(body.expires_on, time(23, 59, 59), tzinfo=ET) if body.expires_on else None
    )
    row, plaintext = new_token(session, principal.user_id, body.name, expires)
    session.commit()
    return CreatedTokenOut(
        token=plaintext,
        id=row.id,
        name=row.name,
        prefix=row.token_prefix,
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


@router.delete("/api-tokens/{token_id}", status_code=204)
def revoke_token(
    token_id: UUID,
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> Response:
    row = session.scalar(
        select(ApiToken)
        .where(
            ApiToken.id == token_id,
            ApiToken.user_id == principal.user_id,
            ApiToken.revoked_at.is_(None),
        )
        .with_for_update()
    )
    if row is None:
        raise HTTPException(404, "token not found")
    row.revoked_at = now_et()
    row.revoked_by = "user"
    session.commit()
    return Response(status_code=204)
