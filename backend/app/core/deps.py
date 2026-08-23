import hashlib
import secrets
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_session
from app.models.user import User
from app.services.auth_provider import InvalidAccessToken, verify_access_token


def get_current_user_id() -> UUID:
    """Ring 0: fixed dev user. Swap this for JWT extraction in MVP."""
    return UUID(get_settings().DEV_USER_ID)


@dataclass(frozen=True)
class Principal:
    """The authenticated caller of the current request."""

    user_id: UUID
    email: str | None = None
    locale: str | None = None
    base_currency: str | None = None


_COOKIE_NAME = "portfonia_access_token"


def _request_access_token(request: Request) -> str | None:
    bearer = _bearer_token(request.headers.get("authorization"))
    if bearer is not None:
        return bearer
    cookie = request.cookies.get(_COOKIE_NAME)
    return cookie or None


def current_principal(request: Request, session: Session = Depends(get_session)) -> Principal:
    """The one request-scoped entry point for "who is calling".

    Verifies the access token (Bearer or session cookie) against the Auth
    provider's JWKS, then looks up `users.auth_subject`. A valid token whose
    `sub` has no `users` row is 401 — never auto-inserted (Ring 1-B §6.5/§6.9).
    """
    token = _request_access_token(request)
    if token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    try:
        claims = verify_access_token(token)
    except InvalidAccessToken:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
        ) from None
    user = session.execute(
        select(User).where(User.auth_subject == claims.sub, User.status == "active")
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    return Principal(
        user_id=user.id,
        email=user.email,
        locale=user.locale,
        base_currency=user.base_currency,
    )


def _bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _digest(value: str) -> bytes:
    """Fixed-length digest for comparison, not a security-hardening hash.

    `secrets.compare_digest` requires ASCII-only when given two `str`s and
    raises `TypeError` otherwise — a crafted `Authorization` header with a
    non-ASCII byte would turn into an unhandled 500 (PR #177 review round 2).
    `str.encode("utf-8")` never raises, and `bytes` vs `bytes` has no such
    restriction, so hashing first closes that off entirely. It also fixes
    every digest at 32 bytes regardless of input length, closing the
    `any()`-short-circuit / length timing side-channel below.
    """
    return hashlib.sha256(value.encode("utf-8")).digest()


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

    # Every candidate is compared unconditionally (no `any()` short-circuit) —
    # matching the primary token must take exactly as long as matching _PREV
    # or matching nothing, or the timing itself would reveal which candidate
    # matched during a rotation window (PR #177 review round 2).
    provided_digest = _digest(provided)
    matched = False
    for candidate in candidates:
        matched |= secrets.compare_digest(provided_digest, _digest(candidate))

    if not matched:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid ops token")
