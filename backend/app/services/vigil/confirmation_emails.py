"""Reusable owner-scoped Vigil confirmation-email lifecycle (issue #539)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.user import User
from app.models.vigil import (
    VigilActionToken,
    VigilConfiguration,
    VigilConfirmationEmail,
    VigilObject,
    VigilVault,
)
from app.services.vigil.configuration import (
    VigilRevisionConflict,
    load_configuration_data,
    normalize_email,
)
from app.services.vigil.crypto import decrypt_field, encrypt_field
from app.services.vigil.dispatch import cancel_outbox_intents, write_outbox_entry
from app.services.vigil.tokens import DRILL_TTL, db_now, hash_link_token, mint_link_token

_ADDRESS_PURPOSE = "vigil_confirmation_email_address"
_VERIFY_PURPOSE = "email_verify"
_FINGERPRINT_CONTEXT = b"portfonia:vigil-confirmation-email-fingerprint:v1"
VERIFY_COOLDOWN = timedelta(seconds=60)


class VigilConfirmationEmailInputError(ValueError):
    pass


class VigilConfirmationEmailNotFound(RuntimeError):
    pass


class VigilConfirmationEmailCooldown(RuntimeError):
    def __init__(self, retry_after: int) -> None:
        super().__init__("confirmation email cooldown")
        self.retry_after = retry_after


class VigilConfirmationEmailPublicError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class ConfirmationEmailView:
    id: UUID
    address: str
    verified_at: datetime | None


@dataclass(frozen=True)
class ConfirmationEmailMutation:
    email: ConfirmationEmailView | None
    revision: int


def normalize_confirmation_email(raw: str) -> str:
    try:
        return normalize_email(raw).lower()
    except (ValueError, binascii.Error) as exc:
        raise VigilConfirmationEmailInputError("invalid confirmation email address") from exc


def _fingerprint_key() -> bytes:
    secret = get_settings().VIGIL_NOTIFICATION_KEY
    if secret is None or not secret.get_secret_value():
        raise VigilConfirmationEmailInputError("vigil notification key is unavailable")
    try:
        material = base64.urlsafe_b64decode(secret.get_secret_value().encode())
    except ValueError as exc:
        raise VigilConfirmationEmailInputError("vigil notification key is unavailable") from exc
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_FINGERPRINT_CONTEXT,
    ).derive(material)


def confirmation_email_fingerprint(address: str) -> str:
    return hmac.new(_fingerprint_key(), address.encode(), hashlib.sha256).hexdigest()


def _decrypt_address(row: VigilConfirmationEmail) -> str:
    return decrypt_field(
        row.address_cipher,
        purpose=_ADDRESS_PURPOSE,
        table="vigil_confirmation_emails",
        row_id=row.id,
        vault_id=row.owner_user_id,
    )


def _view(row: VigilConfirmationEmail) -> ConfirmationEmailView:
    return ConfirmationEmailView(
        id=row.id, address=_decrypt_address(row), verified_at=row.verified_at
    )


def list_confirmation_emails(
    session: Session, *, owner_user_id: UUID
) -> list[ConfirmationEmailView]:
    rows = session.scalars(
        select(VigilConfirmationEmail)
        .where(VigilConfirmationEmail.owner_user_id == owner_user_id)
        .order_by(VigilConfirmationEmail.created_at, VigilConfirmationEmail.id)
    ).all()
    return [_view(row) for row in rows]


def _lock_user_vault(
    session: Session, *, owner_user_id: UUID, expected_revision: int, create: bool
) -> tuple[User, VigilVault]:
    user = session.execute(
        select(User).where(User.id == owner_user_id).with_for_update()
    ).scalar_one_or_none()
    if user is None:
        raise VigilConfirmationEmailNotFound("owner is not available")
    vault = session.scalars(
        select(VigilVault).where(VigilVault.owner_user_id == owner_user_id).with_for_update()
    ).one_or_none()
    if vault is None:
        if not create:
            raise VigilConfirmationEmailNotFound("no vault")
        if expected_revision != 0:
            raise VigilRevisionConflict(current_revision=0)
        vault = VigilVault(owner_user_id=owner_user_id)
        session.add(vault)
        session.flush()
    elif expected_revision != vault.revision:
        raise VigilRevisionConflict(current_revision=vault.revision)
    return user, vault


def add_confirmation_email(
    session: Session, *, owner_user_id: UUID, expected_revision: int, address: str
) -> ConfirmationEmailMutation:
    normalized = normalize_confirmation_email(address)
    _, vault = _lock_user_vault(
        session, owner_user_id=owner_user_id, expected_revision=expected_revision, create=True
    )
    fingerprint = confirmation_email_fingerprint(normalized)
    existing = session.scalars(
        select(VigilConfirmationEmail)
        .where(
            VigilConfirmationEmail.owner_user_id == owner_user_id,
            VigilConfirmationEmail.address_fingerprint == fingerprint,
        )
        .with_for_update()
    ).one_or_none()
    if existing is not None:
        return ConfirmationEmailMutation(email=_view(existing), revision=vault.revision)

    email_id = uuid.uuid4()
    row = VigilConfirmationEmail(
        id=email_id,
        owner_user_id=owner_user_id,
        address_cipher=encrypt_field(
            normalized,
            purpose=_ADDRESS_PURPOSE,
            table="vigil_confirmation_emails",
            row_id=email_id,
            vault_id=owner_user_id,
        ),
        address_fingerprint=fingerprint,
    )
    session.add(row)
    vault.revision += 1
    session.flush()
    return ConfirmationEmailMutation(email=_view(row), revision=vault.revision)


def _lock_owned_email(
    session: Session, *, owner_user_id: UUID, email_id: UUID
) -> VigilConfirmationEmail:
    row = session.scalars(
        select(VigilConfirmationEmail)
        .where(
            VigilConfirmationEmail.id == email_id,
            VigilConfirmationEmail.owner_user_id == owner_user_id,
        )
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise VigilConfirmationEmailNotFound("confirmation email not found")
    return row


def send_confirmation_email_verification(
    session: Session, *, owner_user_id: UUID, email_id: UUID, expected_revision: int
) -> ConfirmationEmailMutation:
    _, vault = _lock_user_vault(
        session, owner_user_id=owner_user_id, expected_revision=expected_revision, create=False
    )
    row = _lock_owned_email(session, owner_user_id=owner_user_id, email_id=email_id)
    if row.verified_at is not None:
        return ConfirmationEmailMutation(email=_view(row), revision=vault.revision)
    now = db_now(session)
    latest = session.scalars(
        select(VigilActionToken)
        .where(
            VigilActionToken.confirmation_email_id == row.id,
            VigilActionToken.purpose == _VERIFY_PURPOSE,
        )
        .order_by(VigilActionToken.created_at.desc())
    ).first()
    if latest is not None and now - latest.created_at < VERIFY_COOLDOWN:
        remaining = int((VERIFY_COOLDOWN - (now - latest.created_at)).total_seconds()) + 1
        raise VigilConfirmationEmailCooldown(max(1, remaining))

    open_rows = list(
        session.scalars(
            select(VigilActionToken)
            .where(
                VigilActionToken.confirmation_email_id == row.id,
                VigilActionToken.purpose == _VERIFY_PURPOSE,
                VigilActionToken.confirmed_at.is_(None),
                VigilActionToken.invalidated_at.is_(None),
            )
            .order_by(VigilActionToken.id)
            .with_for_update()
        ).all()
    )
    for token in open_rows:
        token.invalidated_at = now
    if open_rows:
        cancel_outbox_intents(
            session,
            vault_id=vault.id,
            purpose=_VERIFY_PURPOSE,
            scope_ids=[token.id for token in open_rows],
        )

    presented, token_hash = mint_link_token()
    token_row = VigilActionToken(
        vault_id=vault.id,
        config_id=None,
        object_id=None,
        confirmation_email_id=row.id,
        purpose=_VERIFY_PURPOSE,
        token_hash=token_hash,
        expires_at=now + DRILL_TTL,
    )
    session.add(token_row)
    session.flush()
    confirm_url = f"{get_settings().FRONTEND_URL.rstrip('/')}/vigil/confirm#{presented}"
    write_outbox_entry(
        session,
        vault=vault,
        config_id=None,
        object_id=None,
        scope_id=token_row.id,
        purpose=_VERIFY_PURPOSE,
        dedup_key=f"email-verify:{token_row.id}",
        recipient_email=_decrypt_address(row),
        subject="Verify your Portfonia Vigil confirmation email",
        text_body=(
            "Verify this address for Portfonia Vigil.\n"
            f"Open: {confirm_url}\n"
            "This link expires in 48 hours and does not activate Vigil."
        ),
        html_body=(
            "<p>Verify this address for Portfonia Vigil.</p>"
            f'<p><a href="{confirm_url}">Verify email</a></p>'
            "<p>This link expires in 48 hours and does not activate Vigil.</p>"
        ),
        token=presented,
    )
    vault.revision += 1
    session.flush()
    return ConfirmationEmailMutation(email=_view(row), revision=vault.revision)


def _configuration_selects_email(
    config: VigilConfiguration, *, vault_id: UUID, email_id: UUID
) -> bool:
    data = load_configuration_data(config, vault_id)
    return data.get("confirmation_email_id") == str(email_id)


def invalidate_selected_setup(
    session: Session, *, vault: VigilVault, email_id: UUID, now: datetime
) -> bool:
    selected = False
    configs: list[VigilConfiguration] = []
    for config_id in (vault.active_config_id, vault.pending_config_id):
        if config_id is None:
            continue
        config = session.execute(
            select(VigilConfiguration).where(VigilConfiguration.id == config_id).with_for_update()
        ).scalar_one_or_none()
        if config is not None:
            configs.append(config)
            selected = selected or _configuration_selects_email(
                config, vault_id=vault.id, email_id=email_id
            )
    if not selected:
        return False

    from app.services.vigil.cycles import cancel_active_cycles

    cancel_active_cycles(session, vault, now=now, status="cancelled")
    for purpose in ("drill", "challenge", "release", "owner_notice"):
        cancel_outbox_intents(session, vault_id=vault.id, purpose=purpose)
    for token in session.scalars(
        select(VigilActionToken)
        .where(
            VigilActionToken.vault_id == vault.id,
            VigilActionToken.purpose != _VERIFY_PURPOSE,
            VigilActionToken.invalidated_at.is_(None),
            VigilActionToken.confirmed_at.is_(None),
        )
        .order_by(VigilActionToken.id)
        .with_for_update()
    ).all():
        token.invalidated_at = now
    for object_id in (vault.active_object_id, vault.pending_object_id):
        if object_id is None:
            continue
        obj = session.execute(
            select(VigilObject).where(VigilObject.id == object_id).with_for_update()
        ).scalar_one_or_none()
        if obj is not None:
            obj.status = "retired"
            obj.ciphertext = None
            obj.outer_cipher = None
    for config in configs:
        config.status = "retired"
    vault.active_config_id = None
    vault.active_object_id = None
    vault.pending_config_id = None
    vault.pending_object_id = None
    vault.phase = "DISARMED"
    vault.next_check_at = None
    vault.updated_at = now
    vault.revision += 1
    session.flush()
    return True


def invalidate_setup_for_configuration(
    session: Session, *, vault: VigilVault, config: VigilConfiguration, now: datetime
) -> bool:
    data = load_configuration_data(config, vault.id)
    raw_id = data.get("confirmation_email_id")
    if not isinstance(raw_id, str):
        return False
    try:
        email_id = UUID(raw_id)
    except ValueError:
        return False
    return invalidate_selected_setup(session, vault=vault, email_id=email_id, now=now)


def cancel_confirmation_email_verification(
    session: Session, *, owner_user_id: UUID, email_id: UUID, expected_revision: int
) -> ConfirmationEmailMutation:
    _, vault = _lock_user_vault(
        session, owner_user_id=owner_user_id, expected_revision=expected_revision, create=False
    )
    row = _lock_owned_email(session, owner_user_id=owner_user_id, email_id=email_id)
    now = db_now(session)
    tokens = list(
        session.scalars(
            select(VigilActionToken)
            .where(
                VigilActionToken.confirmation_email_id == row.id,
                VigilActionToken.purpose == _VERIFY_PURPOSE,
            )
            .order_by(VigilActionToken.id)
            .with_for_update()
        ).all()
    )
    for token in tokens:
        if token.invalidated_at is None and token.confirmed_at is None:
            token.invalidated_at = now
    cancel_outbox_intents(
        session,
        vault_id=vault.id,
        purpose=_VERIFY_PURPOSE,
        scope_ids=[token.id for token in tokens],
    )
    row.verified_at = None
    row.updated_at = now
    if not invalidate_selected_setup(session, vault=vault, email_id=row.id, now=now):
        vault.revision += 1
    session.flush()
    return ConfirmationEmailMutation(email=_view(row), revision=vault.revision)


def delete_confirmation_email(
    session: Session, *, owner_user_id: UUID, email_id: UUID, expected_revision: int
) -> ConfirmationEmailMutation:
    _, vault = _lock_user_vault(
        session, owner_user_id=owner_user_id, expected_revision=expected_revision, create=False
    )
    row = _lock_owned_email(session, owner_user_id=owner_user_id, email_id=email_id)
    now = db_now(session)
    token_ids = list(
        session.scalars(
            select(VigilActionToken.id).where(
                VigilActionToken.confirmation_email_id == row.id,
                VigilActionToken.purpose == _VERIFY_PURPOSE,
            )
        ).all()
    )
    cancel_outbox_intents(session, vault_id=vault.id, purpose=_VERIFY_PURPOSE, scope_ids=token_ids)
    selected = invalidate_selected_setup(session, vault=vault, email_id=row.id, now=now)
    session.execute(
        delete(VigilActionToken).where(VigilActionToken.confirmation_email_id == row.id)
    )
    session.delete(row)
    if not selected:
        vault.revision += 1
    session.flush()
    return ConfirmationEmailMutation(email=None, revision=vault.revision)


def confirm_confirmation_email(session: Session, *, token: str, now: datetime | None = None) -> str:
    token_hash = hash_link_token(token)
    peek = session.scalars(
        select(VigilActionToken).where(VigilActionToken.token_hash == token_hash)
    ).one_or_none()
    if peek is None or peek.purpose != _VERIFY_PURPOSE or peek.confirmation_email_id is None:
        raise VigilConfirmationEmailPublicError(404, "not found")
    vault = session.get(VigilVault, peek.vault_id)
    if vault is None:
        raise VigilConfirmationEmailPublicError(404, "not found")
    session.execute(
        select(User).where(User.id == vault.owner_user_id).with_for_update()
    ).scalar_one()
    locked_vault = session.execute(
        select(VigilVault).where(VigilVault.id == vault.id).with_for_update()
    ).scalar_one()
    row = session.execute(
        select(VigilConfirmationEmail)
        .where(VigilConfirmationEmail.id == peek.confirmation_email_id)
        .with_for_update()
    ).scalar_one_or_none()
    locked_token = session.execute(
        select(VigilActionToken).where(VigilActionToken.id == peek.id).with_for_update()
    ).scalar_one()
    if row is None or row.owner_user_id != locked_vault.owner_user_id:
        raise VigilConfirmationEmailPublicError(410, "gone")
    current = now or db_now(session)
    if locked_token.confirmed_at is not None:
        return "already_resolved"
    if (
        locked_token.invalidated_at is not None
        or locked_token.expires_at is None
        or current >= locked_token.expires_at
    ):
        raise VigilConfirmationEmailPublicError(410, "gone")
    locked_token.confirmed_at = current
    locked_token.used_at = current
    row.verified_at = current
    row.updated_at = current
    locked_vault.revision += 1
    session.flush()
    return "confirmed"


def resolve_verified_configuration_email(
    session: Session, *, vault: VigilVault, config: VigilConfiguration, lock: bool = False
) -> str | None:
    data = load_configuration_data(config, vault.id)
    raw_id = data.get("confirmation_email_id")
    snapshot = data.get("confirmation_email")
    if not isinstance(raw_id, str) or not isinstance(snapshot, str):
        return None
    try:
        email_id = UUID(raw_id)
    except ValueError:
        return None
    stmt = select(VigilConfirmationEmail).where(
        VigilConfirmationEmail.id == email_id,
        VigilConfirmationEmail.owner_user_id == vault.owner_user_id,
    )
    if lock:
        stmt = stmt.with_for_update()
    row = session.scalars(stmt).one_or_none()
    if row is None or row.verified_at is None:
        return None
    address = _decrypt_address(row)
    if address != snapshot:
        return None
    return address
