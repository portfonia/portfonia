import hashlib
import secrets
from dataclasses import dataclass
from uuid import UUID

from fastapi import Header, HTTPException, Request, status

from app.core.config import get_settings


def get_current_user_id() -> UUID:
    """Ring 0: fixed dev user. Swap this for JWT extraction in MVP."""
    return UUID(get_settings().DEV_USER_ID)


@dataclass(frozen=True)
class Principal:
    """The authenticated caller of the current request.

    B4 appends email/locale/base_currency once `users` exists — deliberately
    absent now rather than reserved as placeholders for fields that don't
    exist yet (Ring 1-B design doc §5.3).
    """

    user_id: UUID


def current_principal(request: Request) -> Principal:
    """The one request-scoped entry point for "who is calling".

    B3: still resolves to `DEV_USER_ID` unconditionally — no real auth
    exists yet. What changes is the shape: every identity-bearing route
    depends on this via `Depends(current_principal)` instead of calling
    `get_current_user_id()` directly, so `dependency_overrides` (which only
    intercepts `Depends`, not a bare function call) works in tests, and B4
    can swap this function's body for JWT extraction off `request` without
    touching a single call site.
    """
    return Principal(user_id=get_current_user_id())


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
