"""Auth-provider adapter. Business code talks to this module, not to a vendor SDK.

Ring 1-B design.md §6.5 / Concept §10: wrap the hosted Auth API so the provider
is replaceable. Token verification uses the project's JWKS (asymmetric ES256 /
RS256) — not the legacy HS256 JWT secret, which new Supabase projects have
not used as the default since 2025-10-01.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.error import URLError

import httpx
import jwt
from jwt import PyJWKClient

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_jwks_client: PyJWKClient | None = None


@dataclass(frozen=True)
class AccessTokenClaims:
    sub: str
    email: str | None = None


class InvalidAccessToken(Exception):
    """Signature, issuer, audience, expiry, or role check failed."""


class AuthProviderError(Exception):
    """The Auth provider rejected an admin operation."""


def _issuer() -> str:
    return f"{get_settings().SUPABASE_URL.rstrip('/')}/auth/v1"


def _jwks() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(
            f"{_issuer()}/.well-known/jwks.json",
            cache_jwk_set=True,
            lifespan=600,
        )
    return _jwks_client


def reset_jwks_client() -> None:
    """Drop the cached JWKS client (tests / settings reload)."""
    global _jwks_client
    _jwks_client = None


def verify_access_token(token: str) -> AccessTokenClaims:
    """Verify a user access token locally against the project's JWKS.

    Rejects HS256 (legacy shared secret) by allowing only ES256/RS256.
    Rejects `anon` / `service_role` tokens — those are not a user identity.
    """
    try:
        signing_key = _jwks().get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256", "RS256"],
            audience="authenticated",
            issuer=_issuer(),
            leeway=30,
            options={"require": ["exp", "sub", "iss", "aud"]},
        )
    except (jwt.PyJWTError, URLError, TimeoutError, OSError) as exc:
        raise InvalidAccessToken("invalid token") from exc
    if payload.get("role") != "authenticated":
        raise InvalidAccessToken("invalid token")
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        raise InvalidAccessToken("invalid token")
    email = payload.get("email")
    return AccessTokenClaims(sub=sub, email=email if isinstance(email, str) else None)


def _admin_headers() -> dict[str, str]:
    """Headers for GoTrue admin endpoints.

    Legacy `service_role` keys are JWTs and go on both `apikey` and
    `Authorization: Bearer`. New-project secret keys (`sb_secret_...`) are
    opaque; sending them as Bearer is rejected as Invalid JWT. Those go on
    `apikey` only (Supabase API keys docs, 2026).
    """
    key = get_settings().SUPABASE_SERVICE_ROLE_KEY.get_secret_value()
    headers = {
        "apikey": key,
        "Content-Type": "application/json",
    }
    if not key.startswith("sb_"):
        headers["Authorization"] = f"Bearer {key}"
    return headers


def create_auth_user(email: str, password: str) -> str:
    """Create a confirmed Auth user via the service-role admin API. Returns sub."""
    url = f"{_issuer()}/admin/users"
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                url,
                headers=_admin_headers(),
                json={"email": email, "password": password, "email_confirm": True},
            )
    except httpx.HTTPError as exc:
        raise AuthProviderError("create_user failed") from exc
    if resp.status_code not in (200, 201):
        logger.error("auth provider create_user failed status=%s", resp.status_code)
        raise AuthProviderError("create_user failed")
    try:
        user_id = resp.json().get("id")
    except ValueError as exc:
        raise AuthProviderError("create_user missing id") from exc
    if not isinstance(user_id, str) or not user_id:
        raise AuthProviderError("create_user missing id")
    return user_id


def delete_auth_user(sub: str) -> None:
    """Compensation for a signup that created the Auth user then failed to commit."""
    url = f"{_issuer()}/admin/users/{sub}"
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.delete(url, headers=_admin_headers())
    except httpx.HTTPError as exc:
        raise AuthProviderError("delete_user failed") from exc
    if resp.status_code not in (200, 204, 404):
        logger.error("auth provider delete_user failed status=%s", resp.status_code)
        raise AuthProviderError("delete_user failed")
