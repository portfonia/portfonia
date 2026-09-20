from __future__ import annotations

import uuid
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.vigil import VigilConfirmationEmail
from app.services.vigil.confirmation_emails import (
    confirmation_email_fingerprint,
    normalize_confirmation_email,
)
from app.services.vigil.crypto import encrypt_field


def ensure_confirmation_email(
    session: Session,
    *,
    owner_user_id: UUID,
    address: str = "owner@example.com",
    verified: bool = True,
    email_id: UUID | None = None,
) -> VigilConfirmationEmail:
    normalized = normalize_confirmation_email(address)
    fingerprint = confirmation_email_fingerprint(normalized)
    existing = session.scalars(
        select(VigilConfirmationEmail).where(
            VigilConfirmationEmail.owner_user_id == owner_user_id,
            VigilConfirmationEmail.address_fingerprint == fingerprint,
        )
    ).one_or_none()
    if existing is not None:
        return existing
    resolved_id = email_id or uuid.uuid4()
    row = VigilConfirmationEmail(
        id=resolved_id,
        owner_user_id=owner_user_id,
        address_cipher=encrypt_field(
            normalized,
            purpose="vigil_confirmation_email_address",
            table="vigil_confirmation_emails",
            row_id=resolved_id,
            vault_id=owner_user_id,
        ),
        address_fingerprint=fingerprint,
        verified_at=datetime.now(UTC) if verified else None,
    )
    session.add(row)
    session.flush()
    return row
