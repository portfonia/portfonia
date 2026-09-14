"""Interactive management auth: JWT owner allowlist then Portfonia session-status."""

from __future__ import annotations

from fastapi import Depends, HTTPException, status

from vigil_app.core.auth import AccessTokenClaims, require_owner
from vigil_app.services import session_status as session_status_service
from vigil_app.services.session_status import SessionExpired, SessionStatusUnavailable


def require_management_owner(
    claims: AccessTokenClaims = Depends(require_owner),
) -> AccessTokenClaims:
    try:
        session_status_service.check_session_status(claims.token)
    except SessionExpired:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
        ) from None
    except SessionStatusUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="session status unavailable",
        ) from None
    return claims
