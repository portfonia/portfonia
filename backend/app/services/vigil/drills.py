"""Account drill enqueue, public status, and confirmation (issue #458)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.user import User
from app.models.vigil import (
    VigilActionToken,
    VigilConfiguration,
    VigilObject,
    VigilOutbox,
    VigilVault,
)
from app.services.vigil.access import is_vigil_owner_eligible
from app.services.vigil.configuration import (
    VigilRevisionConflict,
    load_configuration_data,
    normalize_email,
)
from app.services.vigil.crypto import decrypt_notification_field
from app.services.vigil.dispatch import cancel_outbox_intents, write_outbox_entry
from app.services.vigil.tokens import (
    DRILL_TTL,
    VigilNonceError,
    consume_nonce,
    db_now,
    hash_link_token,
    invalidate_open_drill_tokens,
    mint_link_token,
    mint_signed_nonce,
    rfc3339_z,
    verify_signed_nonce,
)

DRILL_COOLDOWN = timedelta(seconds=60)
_DRILL_PURPOSE = "drill"
_CONFIRM_ACTION = "confirm"
_PAYLOAD_PURPOSE = "vigil_outbox_payload"


class VigilDrillInputError(ValueError):
    """Malformed/out-of-bounds drill request (-> 422)."""


class VigilDrillConflict(RuntimeError):
    """Stale revision or conflicting pending state (-> 409)."""


class VigilDrillCooldown(RuntimeError):
    """60s drill cooldown (-> 429)."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("drill cooldown")
        self.retry_after = retry_after


class VigilDrillUnavailable(RuntimeError):
    """Feature/account/mode cannot enqueue or arm (-> 503)."""


class VigilPublicTokenError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class DrillEnqueueResult:
    drill_id: UUID
    status: str
    revision: int


@dataclass(frozen=True)
class PublicStatus:
    available: bool
    nonce: str | None = None
    expires_at: str | None = None


def _lock_user_and_vault(session: Session, owner_user_id: UUID) -> tuple[User, VigilVault]:
    user = session.execute(
        select(User).where(User.id == owner_user_id).with_for_update()
    ).scalar_one_or_none()
    if user is None:
        raise VigilDrillUnavailable("owner is not available")
    vault = session.scalars(
        select(VigilVault).where(VigilVault.owner_user_id == owner_user_id).with_for_update()
    ).one_or_none()
    if vault is None:
        raise VigilDrillConflict("no vault")
    return user, vault


def _lock_token_context(
    session: Session, token_hash: str
) -> tuple[User, VigilVault, VigilActionToken]:
    peek = session.scalars(
        select(VigilActionToken).where(VigilActionToken.token_hash == token_hash)
    ).one_or_none()
    if peek is None:
        raise VigilPublicTokenError(404, "not found")
    vault_id = peek.vault_id
    vault = session.get(VigilVault, vault_id)
    if vault is None:
        raise VigilPublicTokenError(404, "not found")
    user = session.execute(
        select(User).where(User.id == vault.owner_user_id).with_for_update()
    ).scalar_one_or_none()
    if user is None:
        raise VigilPublicTokenError(404, "not found")
    locked_vault = session.execute(
        select(VigilVault).where(VigilVault.id == vault_id).with_for_update()
    ).scalar_one()
    token = session.execute(
        select(VigilActionToken).where(VigilActionToken.id == peek.id).with_for_update()
    ).scalar_one()
    return user, locked_vault, token


def _account_matches_config(user: User, config: VigilConfiguration, vault_id: UUID) -> bool:
    if user.email_verified_at is None:
        return False
    data = load_configuration_data(config, vault_id)
    account_email = data.get("account_email")
    if not isinstance(account_email, str):
        return False
    try:
        return normalize_email(user.email) == normalize_email(account_email)
    except ValueError:
        return False


def enqueue_drill(
    session: Session,
    *,
    owner_user_id: UUID,
    expected_revision: int,
    config_id: UUID,
    object_id: UUID,
) -> DrillEnqueueResult:
    settings = get_settings()
    if settings.VIGIL_MODE != "active":
        raise VigilDrillUnavailable("vigil outbound mail is not available")

    user, vault = _lock_user_and_vault(session, owner_user_id)
    now = db_now(session)
    if expected_revision != vault.revision:
        raise VigilRevisionConflict(current_revision=vault.revision)
    if not is_vigil_owner_eligible(session, owner_user_id):
        raise VigilDrillUnavailable("owner is not eligible")
    if vault.pending_config_id != config_id or vault.pending_object_id != object_id:
        raise VigilDrillInputError("config/object is not the current pending candidate")

    config = session.execute(
        select(VigilConfiguration).where(VigilConfiguration.id == config_id).with_for_update()
    ).scalar_one_or_none()
    obj = session.execute(
        select(VigilObject).where(VigilObject.id == object_id).with_for_update()
    ).scalar_one_or_none()
    if config is None or obj is None:
        raise VigilDrillInputError("pending config/object is missing")
    if config.vault_id != vault.id or obj.vault_id != vault.id or obj.config_id != config.id:
        raise VigilDrillInputError("config/object does not belong to this vault")
    if config.status != "pending" or obj.status != "ready":
        raise VigilDrillInputError("pending candidate is not ready")
    if obj.cipher_sha256 is None or obj.outer_cipher is None or obj.ciphertext is None:
        raise VigilDrillInputError("pending object is missing crypto fields")
    if not _account_matches_config(user, config, vault.id):
        raise VigilDrillInputError("drill recipient must be the verified current account")

    last = session.scalars(
        select(VigilActionToken)
        .where(VigilActionToken.vault_id == vault.id, VigilActionToken.purpose == _DRILL_PURPOSE)
        .order_by(VigilActionToken.created_at.desc())
    ).first()
    if last is not None and now - last.created_at < DRILL_COOLDOWN:
        remaining = int((DRILL_COOLDOWN - (now - last.created_at)).total_seconds()) + 1
        raise VigilDrillCooldown(retry_after=max(1, remaining))

    superseded = invalidate_open_drill_tokens(
        session, vault_id=vault.id, now=now, config_id=config_id, object_id=object_id
    )
    if superseded:
        cancel_outbox_intents(
            session, vault_id=vault.id, purpose=_DRILL_PURPOSE, scope_ids=superseded
        )

    presented, token_hash = mint_link_token()
    token_row = VigilActionToken(
        vault_id=vault.id,
        config_id=config_id,
        object_id=object_id,
        purpose=_DRILL_PURPOSE,
        token_hash=token_hash,
        expires_at=now + DRILL_TTL,
    )
    session.add(token_row)
    session.flush()

    confirm_url = f"{settings.FRONTEND_URL.rstrip('/')}/vigil/confirm#{presented}"
    subject = "Portfonia Vigil account confirmation"
    text_body = (
        "Confirm this Vigil setup from your current account address.\n"
        f"Open: {confirm_url}\n"
        "This link expires in 48 hours and does not arm Vigil."
    )
    html_body = (
        "<p>Confirm this Vigil setup from your current account address.</p>"
        f'<p><a href="{confirm_url}">Confirm</a></p>'
        "<p>This link expires in 48 hours and does not arm Vigil.</p>"
    )
    write_outbox_entry(
        session,
        vault=vault,
        config_id=config_id,
        object_id=object_id,
        scope_id=token_row.id,
        purpose=_DRILL_PURPOSE,
        dedup_key=f"drill:{token_row.id}",
        recipient_email=user.email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        token=presented,
    )
    vault.revision += 1
    session.flush()
    return DrillEnqueueResult(drill_id=token_row.id, status="pending", revision=vault.revision)


def _token_available(token: VigilActionToken, *, now: datetime, vault: VigilVault) -> str:
    if token.invalidated_at is not None:
        return "invalidated"
    if token.purpose != _DRILL_PURPOSE:
        return "wrong_purpose"
    if token.confirmed_at is not None:
        return "confirmed"
    if token.used_at is not None:
        return "used"
    if token.expires_at is None or now >= token.expires_at:
        return "expired"
    if vault.pending_config_id != token.config_id or vault.pending_object_id != token.object_id:
        return "stale"
    return "available"


def public_status(
    session: Session,
    *,
    token: str,
    action: str | None,
    mint_nonce: bool,
) -> PublicStatus:
    """Read-only unless mint_nonce, which still performs no DB write."""
    token_hash = hash_link_token(token)
    row = session.scalars(
        select(VigilActionToken).where(VigilActionToken.token_hash == token_hash)
    ).one_or_none()
    if row is None:
        raise VigilPublicTokenError(404, "not found")
    vault = session.get(VigilVault, row.vault_id)
    if vault is None:
        raise VigilPublicTokenError(404, "not found")
    now = db_now(session)
    if row.purpose == "cycle_confirm":
        from app.services.vigil.cycles import cycle_token_public_state

        state = cycle_token_public_state(session, row, now=now, vault=vault)
    else:
        state = _token_available(row, now=now, vault=vault)
    if state in {"invalidated", "expired", "stale", "used", "wrong_purpose"}:
        raise VigilPublicTokenError(410, "gone")
    if state == "confirmed":
        return PublicStatus(available=False)
    if action is not None and action != _CONFIRM_ACTION:
        return PublicStatus(available=False)
    if not mint_nonce:
        return PublicStatus(available=True)
    nonce = mint_signed_nonce(
        token_hash=token_hash, action=_CONFIRM_ACTION, object_id=row.object_id, now=now
    )
    return PublicStatus(
        available=True,
        nonce=nonce.compact,
        expires_at=rfc3339_z(nonce.expires_at),
    )


def confirm_drill(
    session: Session,
    *,
    token: str,
    nonce: str,
    now: datetime | None = None,
) -> str:
    token_hash = hash_link_token(token)
    _, vault, row = _lock_token_context(session, token_hash)
    current = now or db_now(session)
    try:
        signed = verify_signed_nonce(
            nonce,
            token_hash=token_hash,
            action=_CONFIRM_ACTION,
            object_id=row.object_id,
            now=current,
        )
        consume_nonce(session, signed, now=current)
    except VigilNonceError as exc:
        raise VigilPublicTokenError(422, str(exc)) from exc

    state = _token_available(row, now=current, vault=vault)
    if state == "confirmed":
        return "already_resolved"
    if state != "available":
        raise VigilPublicTokenError(410, "gone")

    row.confirmed_at = current
    session.flush()
    return "confirmed"


def drill_delivery_state(session: Session, vault: VigilVault) -> list[dict[str, str]]:
    """Owner-visible drill state for GET /vigil/vault. No token material."""
    if vault.pending_config_id is None or vault.pending_object_id is None:
        return []
    token = session.scalars(
        select(VigilActionToken)
        .where(
            VigilActionToken.vault_id == vault.id,
            VigilActionToken.purpose == _DRILL_PURPOSE,
            VigilActionToken.config_id == vault.pending_config_id,
            VigilActionToken.object_id == vault.pending_object_id,
        )
        .order_by(VigilActionToken.created_at.desc())
    ).first()
    if token is None:
        return []
    now = db_now(session)
    if token.confirmed_at is not None:
        state = "confirmed"
    elif token.invalidated_at is not None:
        return []
    elif token.expires_at is not None and now >= token.expires_at:
        state = "expired"
    else:
        outbox = session.scalars(
            select(VigilOutbox).where(
                VigilOutbox.vault_id == vault.id,
                VigilOutbox.purpose == _DRILL_PURPOSE,
                VigilOutbox.scope_id == token.id,
            )
        ).first()
        # #525 flattened the outbox to pending/accepted/failed. A `failed`
        # intent that actually got an attempt (provider refusal, retry
        # window) reads as `unknown` here; one that never got an attempt —
        # window expiry on an unattempted row, or a superseded/cancelled
        # intent — leaves no drill row at all, matching the pre-#525
        # behaviour where those rows were `cancelled` and hidden rather than
        # shown to the owner as an unresolved attempt.
        if outbox is None or outbox.status == "pending":
            state = "pending"
        elif outbox.status == "accepted":
            state = "sent"
        elif outbox.first_attempt_at is not None:
            state = "unknown"
        else:
            return []
    return [{"purpose": "drill", "state": state}]


def peek_outbox_token_for_tests(session: Session, drill_id: UUID) -> str:
    """Test helper: recover the presented token from a still-encrypted payload."""
    row = session.scalars(
        select(VigilOutbox).where(
            VigilOutbox.scope_id == drill_id, VigilOutbox.purpose == _DRILL_PURPOSE
        )
    ).one()
    if row.payload_cipher is None:
        raise RuntimeError("outbox payload already cleared")
    raw = decrypt_notification_field(
        row.payload_cipher,
        purpose=_PAYLOAD_PURPOSE,
        table="vigil_outbox",
        row_id=row.id,
        vault_id=row.vault_id,
    )
    payload = json.loads(raw)
    token = payload["token"]
    if not isinstance(token, str):
        raise RuntimeError("outbox payload missing token")
    return token
