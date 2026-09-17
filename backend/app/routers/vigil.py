from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.models.vigil import VigilVault
from app.schemas.vigil import VigilVaultStatus
from app.services.vigil.access import VigilOwner, require_vigil_owner

router = APIRouter()

# Issue #452 (Vigil R0 P1.2): owner authorization now exists
# (services/vigil/access.py) — this route reads the caller's own vault row
# under it. No configuration/object/cycle tables exist yet (#454+), so
# `active`/`pending`/`recipients`/`delivery_status` are always empty here;
# `last_scan_completed_at` stays None too, since no scan task writes it
# yet (#453/#456+).


@router.get("/vault", response_model=VigilVaultStatus)
def get_vault(
    owner: VigilOwner = Depends(require_vigil_owner),
    session: Session = Depends(get_session),
) -> VigilVaultStatus:
    vault = session.execute(
        select(VigilVault).where(VigilVault.owner_user_id == owner.user_id)
    ).scalar_one_or_none()

    if vault is None:
        return VigilVaultStatus(vault_id=None, phase="DISARMED", revision=0)

    return VigilVaultStatus(
        vault_id=vault.id,
        phase=vault.phase,
        revision=vault.revision,
        hold_reason=vault.hold_reason,
        next_check_at=vault.next_check_at,
    )
