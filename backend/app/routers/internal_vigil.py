"""Internal Vigil account-facts adapter (issue #452). Not a public API."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_serializer
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import require_vigil_identity_token
from app.models.user import User

router = APIRouter(
    prefix="/internal/vigil",
    tags=["internal-vigil"],
    dependencies=[Depends(require_vigil_identity_token)],
)


class VigilPrincipalOut(BaseModel):
    auth_subject: str
    eligible: bool
    account_email: str | None
    email_verified_at: datetime | None
    observed_at: datetime

    @field_serializer("email_verified_at", "observed_at")
    def _rfc3339_z(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        as_utc = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return as_utc.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _ineligible(auth_subject: str, observed_at: datetime) -> VigilPrincipalOut:
    return VigilPrincipalOut(
        auth_subject=auth_subject,
        eligible=False,
        account_email=None,
        email_verified_at=None,
        observed_at=observed_at,
    )


@router.get("/principals/{auth_subject}")
def get_principal(auth_subject: str, session: Session = Depends(get_session)) -> JSONResponse:
    observed_at = datetime.now(UTC)
    try:
        user = session.execute(
            select(User).where(User.auth_subject == auth_subject)
        ).scalar_one_or_none()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="identity lookup unavailable",
        ) from exc

    if user is None or user.status != "active":
        body = _ineligible(auth_subject, observed_at)
    else:
        account_email = user.email or None
        verified_at = user.email_verified_at
        body = VigilPrincipalOut(
            auth_subject=auth_subject,
            eligible=bool(account_email) and verified_at is not None,
            account_email=account_email,
            email_verified_at=verified_at,
            observed_at=observed_at,
        )

    return JSONResponse(
        content=body.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )
