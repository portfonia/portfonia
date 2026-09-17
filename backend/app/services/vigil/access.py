from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_session
from app.core.deps import Principal, current_principal
from app.models.user import User

# Vigil R0 P1.2 (issue #452) — in-process owner authorization only. No
# /internal/vigil route, no identity-service token, no new JWKS client, no
# cross-service session-status HTTP hop: everything here is a FastAPI
# dependency built directly on the existing `current_principal` and
# `get_session`. See #452's Design comment / #450 Design section 3
# (incorporated by reference) for the full contract this implements.

_FEATURE_ACTIVE_MODES = frozenset({"active", "recovery"})


@dataclass(frozen=True)
class VigilOwner:
    """The Vigil single-owner allowlisted caller. Only the facts the
    feature actually needs ride along — no broader profile dump."""

    user_id: UUID
    email: str
    email_verified_at: datetime | None


def _configured_owner_subject() -> str | None:
    """None (not just falsy) means "not configured" — matches
    VIGIL_OWNER_AUTH_SUBJECT's own Settings default and the
    VIGIL_MODE off-by-default posture (issue #451)."""
    settings = get_settings()
    subject = settings.VIGIL_OWNER_AUTH_SUBJECT
    if subject is None or not subject.strip():
        return None
    return subject


def _feature_available() -> bool:
    return get_settings().VIGIL_MODE in _FEATURE_ACTIVE_MODES


def require_vigil_owner(
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> VigilOwner:
    """Owner-only Vigil authorization boundary (#452).

    Reuses `current_principal` unchanged for authentication (a missing or
    invalid token already raises 401 there, including the idle/absolute
    session-lifetime checks — this dependency never reimplements any of
    that). Reloads `User` fresh by `Principal.user_id` — never by JWT
    `sub` directly, and never via the reserved admin-role column — and
    compares the freshly loaded `auth_subject` against the single
    configured `VIGIL_OWNER_AUTH_SUBJECT` allowlist entry.

    503 (feature not configured) is checked before the owner comparison:
    a caller who isn't the owner must never learn "you're just not the
    owner" (403) when the feature is simply unavailable to everyone.

    Deliberately does NOT gate on `User.email_verified_at` — an
    unverified-email owner can still read vault status; only the (future)
    arm/escalation guard uses `is_vigil_owner_eligible` for that stricter
    check.
    """
    owner_subject = _configured_owner_subject()
    if owner_subject is None or not _feature_available():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="vigil is not available",
        )

    user = session.execute(select(User).where(User.id == principal.user_id)).scalar_one_or_none()
    if user is None:
        # Defensive only: current_principal already confirmed this user_id
        # exists and is active in the same request/transaction.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    if user.auth_subject != owner_subject:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    return VigilOwner(user_id=user.id, email=user.email, email_verified_at=user.email_verified_at)


def is_vigil_owner_eligible(session: Session, user_id: UUID) -> bool:
    """Fresh account-eligibility check for arm/scan/release (#452), callable
    from background/scheduled code with no request-scoped Principal —
    reloads everything by primary key rather than trusting any decision
    made earlier in a browser session.

    Account facts only: active status, allowlist match against the
    currently configured `VIGIL_OWNER_AUTH_SUBJECT`, and a verified
    account email (`User.email_verified_at`) — never the report
    pipeline's separate delivery-address field. Does not gate on
    `VIGIL_MODE`; callers that also need the feature-availability check
    compose that separately (see `require_vigil_owner`).

    Future arm/scan/release callers hold the User-then-vault lock order
    from #450 Design section 3 — that locking is the caller's
    responsibility (e.g. `SELECT ... FOR UPDATE` on `User` before calling
    this), not built into this read.

    Known residual (blacktomb42 PR #504 review, non-blocking): A02's
    "unchanged account address" isn't checked here yet — there is no
    encrypted config `account_email` snapshot to compare against until
    #454 lands. Add that comparison here once #454's configuration table
    exists, rather than opening a parallel eligibility path.
    """
    owner_subject = _configured_owner_subject()
    if owner_subject is None:
        return False

    user = session.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
    if user is None:
        return False
    if user.status != "active":
        return False
    if user.auth_subject != owner_subject:
        return False
    return user.email_verified_at is not None
