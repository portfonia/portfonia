import secrets
from uuid import UUID

from fastapi import Header, HTTPException, status

from app.core.config import get_settings


def get_current_user_id() -> UUID:
    """Ring 0: fixed dev user. Swap this for JWT extraction in MVP."""
    return UUID(get_settings().DEV_USER_ID)


def _bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def require_ops_token(authorization: str | None = Header(default=None)) -> None:
    """Bearer <ADMIN_API_TOKEN>. Completely independent of current_principal —
    queries no table, doesn't care whether the user system exists or is
    healthy (Ring 1-B design doc §4.3: the ops channel must still work when
    the login system is what's broken).

    A missing/malformed header raises 401 here rather than letting FastAPI's
    own required-Header validation produce a 422 — the B2 acceptance
    criteria treat "no token" as an auth failure, not a request-shape error.
    """
    settings = get_settings()
    provided = _bearer_token(authorization)
    if provided is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing ops token")

    candidates = [settings.ADMIN_API_TOKEN.get_secret_value()]
    if settings.ADMIN_API_TOKEN_PREV is not None:
        candidates.append(settings.ADMIN_API_TOKEN_PREV.get_secret_value())

    if not any(secrets.compare_digest(provided, candidate) for candidate in candidates):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid ops token")
