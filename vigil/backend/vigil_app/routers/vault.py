"""Minimal owner vault surface for P1.2. Configuration/objects belong to later PRs."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_serializer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from vigil_app.core.auth import AccessTokenClaims
from vigil_app.core.database import get_session
from vigil_app.models.vault import Vault
from vigil_app.services.management_auth import require_management_owner

router = APIRouter()


class VaultView(BaseModel):
    phase: str
    revision: int
    hold_reason: str | None
    held_at: datetime | None
    active_object_id: UUID | None
    next_check_at: datetime | None

    @field_serializer("held_at", "next_check_at")
    def _rfc3339_z(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        as_utc = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return as_utc.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _disarmed_view() -> VaultView:
    return VaultView(
        phase="DISARMED",
        revision=0,
        hold_reason=None,
        held_at=None,
        active_object_id=None,
        next_check_at=None,
    )


def _from_row(row: Vault) -> VaultView:
    return VaultView(
        phase=row.phase,
        revision=row.revision,
        hold_reason=row.hold_reason,
        held_at=row.held_at,
        active_object_id=row.active_object_id,
        next_check_at=row.next_check_at,
    )


@router.get("/vault")
def get_vault(
    _owner: AccessTokenClaims = Depends(require_management_owner),
    session: Session = Depends(get_session),
) -> VaultView:
    row = session.execute(
        select(Vault).where(Vault.owner_auth_subject == _owner.sub)
    ).scalar_one_or_none()
    if row is None:
        return _disarmed_view()
    return _from_row(row)


@router.post("/vault", status_code=status.HTTP_201_CREATED)
def create_vault(
    _owner: AccessTokenClaims = Depends(require_management_owner),
    session: Session = Depends(get_session),
) -> VaultView:
    existing = session.execute(
        select(Vault).where(Vault.owner_auth_subject == _owner.sub)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="vault exists")
    row = Vault(owner_auth_subject=_owner.sub, phase="DISARMED", revision=0)
    session.add(row)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="vault exists") from exc
    session.refresh(row)
    return _from_row(row)
