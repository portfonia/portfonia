from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.user import User
from app.models.vigil import (
    VigilActionToken,
    VigilConfiguration,
    VigilConfirmationEmail,
    VigilObject,
    VigilOutbox,
    VigilVault,
)
from app.services.vigil.arm import VigilArmInputError, arm_pending
from app.services.vigil.configuration import (
    validate_configuration_input,
    write_pending_configuration,
)
from app.services.vigil.confirmation_emails import (
    VigilConfirmationEmailPublicError,
    add_confirmation_email,
    cancel_confirmation_email_verification,
    confirm_confirmation_email,
    delete_confirmation_email,
    send_confirmation_email_verification,
)
from app.services.vigil.cycles import disarm
from app.services.vigil.dispatch import _decrypt_payload
from app.services.vigil.objects import init_object, upload_object

_OWNER_ID = uuid.UUID("00000000-0000-4000-8000-000000000539")


@pytest.fixture(autouse=True)
def _vigil_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIGIL_MODE", "active")
    monkeypatch.setenv("VIGIL_OWNER_AUTH_SUBJECT", "owner-sub-539")
    monkeypatch.setenv("VIGIL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VIGIL_NOTIFICATION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _owner(db_session: Session, _vigil_settings: None) -> None:
    db_session.add(
        User(
            id=_OWNER_ID,
            auth_provider="supabase",
            auth_subject="owner-sub-539",
            email="account@example.com",
            status="active",
            locale="en",
            base_currency="USD",
            report_cadence="mwf",
            email_verified_at=datetime.now(UTC),
        )
    )
    db_session.flush()


def _confirmation_token(session: Session, email_id: uuid.UUID) -> str:
    token = session.scalars(
        select(VigilActionToken)
        .where(
            VigilActionToken.confirmation_email_id == email_id,
            VigilActionToken.purpose == "email_verify",
        )
        .order_by(VigilActionToken.created_at.desc())
    ).one()
    outbox = session.scalars(
        select(VigilOutbox).where(
            VigilOutbox.scope_id == token.id, VigilOutbox.purpose == "email_verify"
        )
    ).one()
    return _decrypt_payload(outbox).token


def _add_verified(session: Session, address: str = "Owner@Example.COM") -> tuple[uuid.UUID, int]:
    added = add_confirmation_email(
        session, owner_user_id=_OWNER_ID, expected_revision=0, address=address
    )
    assert added.email is not None
    send_confirmation_email_verification(
        session,
        owner_user_id=_OWNER_ID,
        email_id=added.email.id,
        expected_revision=added.revision,
    )
    token = _confirmation_token(session, added.email.id)
    assert confirm_confirmation_email(session, token=token) == "confirmed"
    vault = session.scalars(select(VigilVault).where(VigilVault.owner_user_id == _OWNER_ID)).one()
    return added.email.id, vault.revision


def _ready_candidate(
    session: Session, email_id: uuid.UUID, revision: int
) -> tuple[VigilVault, VigilConfiguration, VigilObject]:
    config_result = write_pending_configuration(
        session,
        owner_user_id=_OWNER_ID,
        confirmation_email_id=email_id,
        expected_revision=revision,
        normalized=validate_configuration_input(
            interval_days=30,
            grace_hours=72,
            recipients=[{"email": "release@example.com", "email_confirm": "release@example.com"}],
            message="message",
        ),
    )
    init = init_object(
        session,
        owner_user_id=_OWNER_ID,
        expected_revision=config_result.revision,
        config_id=config_result.config_id,
        request_id=uuid.uuid4(),
        filename="will.pdf",
        plaintext_size=4,
    )
    vault = session.get(VigilVault, config_result.vault_id)
    assert vault is not None
    manifest = {
        "version": 1,
        "algorithm": "AES-256-GCM",
        "vault_id": str(vault.id),
        "object_id": str(init.object_id),
        "has_password": False,
        "file_nonce": "MDAwMDAwMDAwMDAw",
        "salt": None,
        "kdf": None,
        "inner_nonce": None,
    }
    uploaded = upload_object(
        session,
        owner_user_id=_OWNER_ID,
        expected_revision=init.revision,
        object_id=init.object_id,
        config_id=config_result.config_id,
        manifest=manifest,
        inner_b64=base64.urlsafe_b64encode(b"0" * 32).rstrip(b"=").decode(),
        ciphertext=b"data" + b"0" * 16,
    )
    vault = session.get(VigilVault, config_result.vault_id)
    config = session.get(VigilConfiguration, config_result.config_id)
    obj = session.get(VigilObject, init.object_id)
    assert vault is not None and config is not None and obj is not None
    assert uploaded.revision == vault.revision
    return vault, config, obj


def test_add_deduplicates_case_normalized_address_and_explicit_post_verifies(
    db_session: Session,
) -> None:
    first = add_confirmation_email(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=0,
        address=" Owner@Example.COM ",
    )
    assert first.email is not None and first.email.address == "owner@example.com"
    duplicate = add_confirmation_email(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=first.revision,
        address="owner@example.com",
    )
    assert duplicate.email is not None and duplicate.email.id == first.email.id
    assert duplicate.revision == first.revision

    sent = send_confirmation_email_verification(
        db_session,
        owner_user_id=_OWNER_ID,
        email_id=first.email.id,
        expected_revision=first.revision,
    )
    token = _confirmation_token(db_session, first.email.id)
    assert sent.email is not None and sent.email.verified_at is None
    assert confirm_confirmation_email(db_session, token=token) == "confirmed"
    assert confirm_confirmation_email(db_session, token=token) == "already_resolved"
    verified = db_session.get(VigilConfirmationEmail, first.email.id)
    assert verified is not None and verified.verified_at is not None


def test_expired_and_wrong_purpose_tokens_do_not_verify(db_session: Session) -> None:
    added = add_confirmation_email(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=0,
        address="owner@example.com",
    )
    assert added.email is not None
    send_confirmation_email_verification(
        db_session,
        owner_user_id=_OWNER_ID,
        email_id=added.email.id,
        expected_revision=added.revision,
    )
    token = _confirmation_token(db_session, added.email.id)
    row = db_session.scalars(
        select(VigilActionToken).where(VigilActionToken.confirmation_email_id == added.email.id)
    ).one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(VigilConfirmationEmailPublicError) as excinfo:
        confirm_confirmation_email(db_session, token=token)
    assert excinfo.value.status_code == 410
    expired = db_session.get(VigilConfirmationEmail, added.email.id)
    assert expired is not None and expired.verified_at is None


def test_public_confirmation_only_verifies_email(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    added = add_confirmation_email(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=0,
        address="owner@example.com",
    )
    assert added.email is not None
    send_confirmation_email_verification(
        db_session,
        owner_user_id=_OWNER_ID,
        email_id=added.email.id,
        expected_revision=added.revision,
    )
    token = _confirmation_token(db_session, added.email.id)
    db_session.commit()
    monkeypatch.setattr("app.routers.vigil_public.verify_vigil_solution", lambda _value: True)

    response = app_client.post("/vigil/public/confirm", json={"token": token, "altcha": "solved"})

    assert response.status_code == 200, response.text
    assert response.json() == {"result": "email_verified", "next_check_at": None}
    vault = db_session.scalars(
        select(VigilVault).where(VigilVault.owner_user_id == _OWNER_ID)
    ).one()
    db_session.refresh(vault)
    assert vault.phase == "DISARMED"
    assert vault.active_config_id is None and vault.active_object_id is None


def test_arm_rejects_unverified_email_without_mutating_candidate(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.vigil.arm.live_activation_allowed", lambda: True)
    email_id, revision = _add_verified(db_session)
    vault, config, obj = _ready_candidate(db_session, email_id, revision)
    email = db_session.get(VigilConfirmationEmail, email_id)
    assert email is not None
    email.verified_at = None
    with pytest.raises(VigilArmInputError):
        arm_pending(
            db_session,
            owner_user_id=_OWNER_ID,
            expected_revision=vault.revision,
            config_id=config.id,
            object_id=obj.id,
        )
    assert vault.pending_config_id == config.id
    assert vault.pending_object_id == obj.id
    assert config.status == "pending" and obj.status == "ready"


def test_arm_depends_on_selected_confirmation_not_account_email(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.vigil.arm.live_activation_allowed", lambda: True)
    email_id, revision = _add_verified(db_session)
    owner = db_session.get(User, _OWNER_ID)
    assert owner is not None
    owner.email_verified_at = None
    vault, config, obj = _ready_candidate(db_session, email_id, revision)

    result = arm_pending(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=vault.revision,
        config_id=config.id,
        object_id=obj.id,
    )

    assert result.phase == "ARMED"


def test_pause_preserves_ciphertext_and_allows_direct_reactivate(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.vigil.arm.live_activation_allowed", lambda: True)
    email_id, revision = _add_verified(db_session)
    vault, config, obj = _ready_candidate(db_session, email_id, revision)
    armed = arm_pending(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=vault.revision,
        config_id=config.id,
        object_id=obj.id,
    )
    ciphertext = obj.ciphertext
    outer = obj.outer_cipher
    paused = disarm(db_session, owner_user_id=_OWNER_ID, expected_revision=armed.revision)
    assert paused.phase == "DISARMED"
    assert vault.active_config_id is None and vault.active_object_id is None
    assert vault.pending_config_id == config.id and vault.pending_object_id == obj.id
    assert config.status == "pending" and obj.status == "ready"
    assert obj.ciphertext == ciphertext and obj.outer_cipher == outer

    rearmed = arm_pending(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=paused.revision,
        config_id=config.id,
        object_id=obj.id,
    )
    assert rearmed.phase == "ARMED"


@pytest.mark.parametrize("delete", [False, True])
def test_cancel_or_delete_selected_email_retires_pending_setup(
    db_session: Session, delete: bool
) -> None:
    email_id, revision = _add_verified(db_session)
    vault, config, obj = _ready_candidate(db_session, email_id, revision)
    if delete:
        result = delete_confirmation_email(
            db_session,
            owner_user_id=_OWNER_ID,
            email_id=email_id,
            expected_revision=vault.revision,
        )
        assert db_session.get(VigilConfirmationEmail, email_id) is None
    else:
        result = cancel_confirmation_email_verification(
            db_session,
            owner_user_id=_OWNER_ID,
            email_id=email_id,
            expected_revision=vault.revision,
        )
        email = db_session.get(VigilConfirmationEmail, email_id)
        assert email is not None and email.verified_at is None
    assert result.revision == vault.revision
    assert vault.phase == "DISARMED"
    assert vault.pending_config_id is None and vault.pending_object_id is None
    assert config.status == "retired" and obj.status == "retired"
    assert obj.ciphertext is None and obj.outer_cipher is None
    assert not db_session.scalars(
        select(VigilOutbox).where(VigilOutbox.vault_id == vault.id, VigilOutbox.status == "pending")
    ).all()


def test_cancel_selected_email_preserves_other_email_verification(db_session: Session) -> None:
    selected_id, revision = _add_verified(db_session)
    other = add_confirmation_email(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=revision,
        address="other@example.com",
    )
    assert other.email is not None
    sent = send_confirmation_email_verification(
        db_session,
        owner_user_id=_OWNER_ID,
        email_id=other.email.id,
        expected_revision=other.revision,
    )
    vault, _config, _obj = _ready_candidate(db_session, selected_id, sent.revision)

    cancel_confirmation_email_verification(
        db_session,
        owner_user_id=_OWNER_ID,
        email_id=selected_id,
        expected_revision=vault.revision,
    )

    token = db_session.scalars(
        select(VigilActionToken).where(
            VigilActionToken.confirmation_email_id == other.email.id,
            VigilActionToken.purpose == "email_verify",
        )
    ).one()
    outbox = db_session.scalars(
        select(VigilOutbox).where(
            VigilOutbox.scope_id == token.id,
            VigilOutbox.purpose == "email_verify",
        )
    ).one()
    assert token.invalidated_at is None
    assert outbox.status == "pending"
