"""Token-only agent authentication, independent of browser session timers."""

from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import _bearer_token
from app.models.user import User
from app.services import api_tokens


@dataclass(frozen=True)
class AgentPrincipal:
    user_id: UUID
    token_id: UUID
    user: User


def agent_principal(request: Request, session: Session = Depends(get_session)) -> AgentPrincipal:
    request.state.agent_identity_checked = True
    plaintext = _bearer_token(request.headers.get("authorization"))
    if plaintext is None or not plaintext.startswith("pfa_"):
        raise HTTPException(401, "unauthorized")
    token = api_tokens.lookup_token(session, plaintext)
    if token is None:
        raise HTTPException(401, "unauthorized")
    request.state.agent_user_id = token.user_id
    request.state.agent_token_id = token.id
    now = api_tokens.now_et()
    user = session.get(User, token.user_id)
    if api_tokens.token_status(token, now) != "active" or user is None or user.status != "active":
        raise HTTPException(401, "unauthorized")
    token.last_used_at = now
    session.commit()
    return AgentPrincipal(user_id=token.user_id, token_id=token.id, user=user)
