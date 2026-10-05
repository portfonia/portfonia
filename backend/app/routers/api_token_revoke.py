"""Public POST-only revoke-all redemption. GET never mutates tokens."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.schemas.agent import RevokedTokensOut
from app.services.api_token_revoke_link import verify_link
from app.services.api_tokens import revoke_all

router = APIRouter()


class RevokeLinkBody(BaseModel):
    token: str


@router.post("/revoke-by-link", response_model=RevokedTokensOut)
def revoke_by_link(
    body: RevokeLinkBody, session: Session = Depends(get_session)
) -> RevokedTokensOut:
    user_id = verify_link(body.token)
    if user_id is None:
        raise HTTPException(400, "invalid_link")
    count = revoke_all(session, user_id, "email_link")
    session.commit()
    return RevokedTokensOut(revoked_count=count)
