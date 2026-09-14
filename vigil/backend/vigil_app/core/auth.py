"""Vigil JWT verifier. Independent copy of Portfonia's access-token contract."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.error import URLError

import jwt
from fastapi import HTTPException, Request, status
from jwt import PyJWKClient

from vigil_app.core.config import get_settings

_jwks_client: PyJWKClient | None = None


@dataclass(frozen=True)
class AccessTokenClaims:
    sub: str
    session_id: str
    token: str
    email: str | None = None


class InvalidAccessToken(Exception):
    """Signature, issuer, audience, expiry, or role check failed."""


def _jwks() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        issuer = get_settings().AUTH_ISSUER.rstrip("/")
        _jwks_client = PyJWKClient(
            f"{issuer}/.well-known/jwks.json",
            cache_jwk_set=True,
            lifespan=600,
        )
    return _jwks_client


def reset_jwks_client() -> None:
    global _jwks_client
    _jwks_client = None


def verify_access_token(token: str) -> AccessTokenClaims:
    try:
        signing_key = _jwks().get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256", "RS256"],
            audience="authenticated",
            issuer=get_settings().AUTH_ISSUER.rstrip("/"),
            leeway=30,
            options={"require": ["exp", "sub", "iss", "aud", "session_id"]},
        )
    except (jwt.PyJWTError, URLError, TimeoutError, OSError) as exc:
        raise InvalidAccessToken("invalid token") from exc
    if payload.get("role") != "authenticated":
        raise InvalidAccessToken("invalid token")
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        raise InvalidAccessToken("invalid token")
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise InvalidAccessToken("invalid token")
    email = payload.get("email")
    return AccessTokenClaims(
        sub=sub,
        email=email if isinstance(email, str) else None,
        session_id=session_id,
        token=token,
    )


def _bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def require_owner(request: Request) -> AccessTokenClaims:
    token = _bearer_token(request.headers.get("authorization"))
    if token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    try:
        claims = verify_access_token(token)
    except InvalidAccessToken:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
        ) from None
    if claims.sub != get_settings().OWNER_AUTH_SUBJECT:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    return claims
