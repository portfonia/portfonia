"""Explicit atomic arm of the current pending candidate (issue #458)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.user import User
from app.models.vigil import (
    VIGIL_OBJECT_GCM_TAG_LENGTH,
    VigilActionToken,
    VigilConfiguration,
    VigilObject,
    VigilVault,
)
from app.services.vigil.access import is_vigil_owner_eligible
from app.services.vigil.configuration import (
    VigilRevisionConflict,
    load_configuration_data,
    normalize_email,
)
from app.services.vigil.crypto import decrypt_field
from app.services.vigil.events import emit_vigil_event
from app.services.vigil.recovery import live_activation_allowed, stop_prior_arrangement
from app.services.vigil.tokens import db_now, rfc3339_z

_OUTER_PURPOSE = "vigil_object_outer"


class VigilArmInputError(ValueError):
    """Invalid arm request (-> 422)."""


class VigilArmUnavailable(RuntimeError):
    """Live activation or feature mode blocks arming (-> 503)."""


class VigilArmConflict(RuntimeError):
    """Hold / mismatched state (-> 409)."""


@dataclass(frozen=True)
class ArmResult:
    phase: str
    revision: int
    next_check_at: str


def _lock_user_and_vault(session: Session, owner_user_id: UUID) -> tuple[User, VigilVault]:
    user = session.execute(
        select(User).where(User.id == owner_user_id).with_for_update()
    ).scalar_one_or_none()
    if user is None:
        raise VigilArmUnavailable("owner is not available")
    vault = session.scalars(
        select(VigilVault).where(VigilVault.owner_user_id == owner_user_id).with_for_update()
    ).one_or_none()
    if vault is None:
        raise VigilArmConflict("no vault")
    return user, vault


def arm_pending(
    session: Session,
    *,
    owner_user_id: UUID,
    expected_revision: int,
    config_id: UUID,
    object_id: UUID,
    allow_incomplete_stop: bool = False,
) -> ArmResult:
    settings = get_settings()
    if settings.VIGIL_MODE != "active":
        raise VigilArmUnavailable("vigil arming is not available")
    if not allow_incomplete_stop and not live_activation_allowed():
        raise VigilArmUnavailable("stop/recovery integration is incomplete")

    user, vault = _lock_user_and_vault(session, owner_user_id)
    now = db_now(session)
    if expected_revision != vault.revision:
        raise VigilRevisionConflict(current_revision=vault.revision)
    if vault.hold_reason is not None:
        raise VigilArmConflict("vault is held")
    if not is_vigil_owner_eligible(session, owner_user_id):
        raise VigilArmUnavailable("owner is not eligible")
    if vault.pending_config_id != config_id or vault.pending_object_id != object_id:
        raise VigilArmInputError("config/object is not the current pending candidate")

    config = session.execute(
        select(VigilConfiguration).where(VigilConfiguration.id == config_id).with_for_update()
    ).scalar_one_or_none()
    obj = session.execute(
        select(VigilObject).where(VigilObject.id == object_id).with_for_update()
    ).scalar_one_or_none()
    if config is None or obj is None:
        raise VigilArmInputError("pending config/object is missing")
    if config.status != "pending" or obj.status != "ready":
        raise VigilArmInputError("pending candidate is not ready")
    if obj.config_id != config.id or obj.vault_id != vault.id or config.vault_id != vault.id:
        raise VigilArmInputError("config/object does not belong to this vault")
    if obj.ciphertext is None or obj.outer_cipher is None or obj.cipher_sha256 is None:
        raise VigilArmInputError("pending object is missing crypto fields")
    if len(obj.ciphertext) != obj.plaintext_size + VIGIL_OBJECT_GCM_TAG_LENGTH:
        raise VigilArmInputError("pending object ciphertext length is invalid")
    if hashlib.sha256(obj.ciphertext).hexdigest() != obj.cipher_sha256:
        raise VigilArmInputError("pending object hash mismatch")
    try:
        decrypt_field(
            obj.outer_cipher,
            purpose=_OUTER_PURPOSE,
            table="vigil_objects",
            row_id=obj.id,
            vault_id=vault.id,
        )
    except Exception as exc:
        raise VigilArmInputError("pending object outer envelope is unreadable") from exc

    data = load_configuration_data(config, vault.id)
    account_email = data.get("account_email")
    recipients = data.get("recipients")
    interval_days = data.get("interval_days")
    if not isinstance(account_email, str):
        raise VigilArmInputError("configuration account_email is missing")
    try:
        if normalize_email(user.email) != normalize_email(account_email):
            raise VigilArmInputError("account email does not match the configuration snapshot")
    except ValueError as exc:
        raise VigilArmInputError("account email is invalid") from exc
    if user.email_verified_at is None:
        raise VigilArmInputError("account email is not verified")
    if not isinstance(recipients, list) or not (1 <= len(recipients) <= 3):
        raise VigilArmInputError("configuration recipients are incomplete")
    if not isinstance(interval_days, int):
        raise VigilArmInputError("configuration interval is invalid")

    drill = session.scalars(
        select(VigilActionToken)
        .where(
            VigilActionToken.vault_id == vault.id,
            VigilActionToken.purpose == "drill",
            VigilActionToken.config_id == config_id,
            VigilActionToken.object_id == object_id,
            VigilActionToken.confirmed_at.isnot(None),
            VigilActionToken.invalidated_at.is_(None),
            VigilActionToken.used_at.is_(None),
        )
        .with_for_update()
    ).first()
    if drill is None:
        raise VigilArmInputError("no confirmed drill for this pending candidate")

    from_phase = vault.phase
    stop_prior_arrangement(session, vault, now=now)

    drill.used_at = now
    config.status = "active"
    obj.status = "active"
    obj.activated_at = now
    vault.active_config_id = config.id
    vault.active_object_id = obj.id
    vault.pending_config_id = None
    vault.pending_object_id = None
    vault.phase = "ARMED"
    vault.next_check_at = now + timedelta(days=interval_days)
    if vault.first_armed_at is None:
        vault.first_armed_at = now
    if vault.retention_anchor_at is None:
        vault.retention_anchor_at = vault.last_owner_confirmed_at or vault.first_armed_at
    vault.updated_at = now
    vault.revision += 1
    session.flush()
    emit_vigil_event(
        "vigil.armed",
        actor="owner",
        vault_id=vault.id,
        from_phase=from_phase,
        to_phase="ARMED",
    )
    assert vault.next_check_at is not None
    return ArmResult(
        phase="ARMED", revision=vault.revision, next_check_at=rfc3339_z(vault.next_check_at)
    )
