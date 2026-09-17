"""services/vigil/configuration.write_pending_configuration — DB-backed
persistence under the User-then-vault lock order (issue #454, P2.1).

Real Postgres per this project's test convention (races/constraints need a
real engine, not a mock).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.vigil import VigilConfiguration, VigilVault
from app.services.vigil.configuration import (
    NormalizedConfiguration,
    VigilRecipientsLocked,
    VigilRevisionConflict,
    validate_configuration_input,
    write_pending_configuration,
)
from app.services.vigil.crypto import decrypt_field

_OWNER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f1")


@pytest.fixture(autouse=True)
def _vigil_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("VIGIL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()


def _owner(db_session: Session) -> User:
    user = User(
        id=_OWNER_ID,
        auth_provider="supabase",
        auth_subject="owner-sub",
        email="owner@example.com",
        status="active",
        locale="zh",
        base_currency="USD",
        report_cadence="mwf",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(user)
    db_session.flush()
    return user


def _normalized(*emails: str) -> NormalizedConfiguration:
    return validate_configuration_input(
        interval_days=30,
        grace_hours=72,
        recipients=[{"email": e, "email_confirm": e} for e in emails],
        message="hello",
    )


def test_creates_vault_on_first_write(db_session: Session) -> None:
    _owner(db_session)
    result = write_pending_configuration(
        db_session,
        owner_user_id=_OWNER_ID,
        owner_email="owner@example.com",
        expected_revision=0,
        normalized=_normalized("a@example.com"),
    )
    db_session.flush()
    vault = db_session.get(VigilVault, result.vault_id)
    assert vault is not None
    assert vault.owner_user_id == _OWNER_ID
    assert vault.revision == 1
    assert vault.pending_config_id == result.config_id


def test_first_write_with_nonzero_expected_revision_conflicts(db_session: Session) -> None:
    _owner(db_session)
    with pytest.raises(VigilRevisionConflict) as excinfo:
        write_pending_configuration(
            db_session,
            owner_user_id=_OWNER_ID,
            owner_email="owner@example.com",
            expected_revision=5,
            normalized=_normalized("a@example.com"),
        )
    assert excinfo.value.current_revision == 0
    assert db_session.query(VigilVault).count() == 0


def test_expected_revision_mismatch_on_existing_vault_conflicts_without_mutation(
    db_session: Session,
) -> None:
    _owner(db_session)
    write_pending_configuration(
        db_session,
        owner_user_id=_OWNER_ID,
        owner_email="owner@example.com",
        expected_revision=0,
        normalized=_normalized("a@example.com"),
    )
    db_session.flush()

    with pytest.raises(VigilRevisionConflict) as excinfo:
        write_pending_configuration(
            db_session,
            owner_user_id=_OWNER_ID,
            owner_email="owner@example.com",
            expected_revision=0,  # stale — real current revision is 1
            normalized=_normalized("b@example.com"),
        )
    assert excinfo.value.current_revision == 1
    assert db_session.query(VigilConfiguration).count() == 1


def test_second_write_retires_prior_pending_and_bumps_config_revision(
    db_session: Session,
) -> None:
    _owner(db_session)
    first = write_pending_configuration(
        db_session,
        owner_user_id=_OWNER_ID,
        owner_email="owner@example.com",
        expected_revision=0,
        normalized=_normalized("a@example.com"),
    )
    db_session.flush()

    second = write_pending_configuration(
        db_session,
        owner_user_id=_OWNER_ID,
        owner_email="owner@example.com",
        expected_revision=first.revision,
        normalized=_normalized("b@example.com"),
    )
    db_session.flush()

    old = db_session.get(VigilConfiguration, first.config_id)
    new = db_session.get(VigilConfiguration, second.config_id)
    assert old is not None and old.status == "retired"
    assert new is not None and new.status == "pending"
    assert new.config_revision == old.config_revision + 1
    vault = db_session.get(VigilVault, second.vault_id)
    assert vault is not None
    assert vault.pending_config_id == second.config_id
    assert vault.revision == 2


def test_data_cipher_decrypts_to_expected_business_json(db_session: Session) -> None:
    _owner(db_session)
    result = write_pending_configuration(
        db_session,
        owner_user_id=_OWNER_ID,
        owner_email="owner@example.com",
        expected_revision=0,
        normalized=_normalized("a@example.com", "b@example.com"),
    )
    db_session.flush()
    config = db_session.get(VigilConfiguration, result.config_id)
    assert config is not None
    plaintext = decrypt_field(
        config.data_cipher,
        purpose="vigil_configuration",
        table="vigil_configurations",
        row_id=config.id,
        vault_id=result.vault_id,
    )
    data = json.loads(plaintext)
    assert data == {
        "interval_days": 30,
        "grace_hours": 72,
        "account_email": "owner@example.com",
        "recipients": [
            {"position": 1, "email": "a@example.com"},
            {"position": 2, "email": "b@example.com"},
        ],
        "message": "hello",
    }


def test_recipients_locked_after_first_arming_rejects_changed_list(db_session: Session) -> None:
    _owner(db_session)
    first = write_pending_configuration(
        db_session,
        owner_user_id=_OWNER_ID,
        owner_email="owner@example.com",
        expected_revision=0,
        normalized=_normalized("a@example.com"),
    )
    db_session.flush()
    vault = db_session.get(VigilVault, first.vault_id)
    assert vault is not None
    # Simulate #458's arm: promote pending -> active, mark first_armed_at.
    config = db_session.get(VigilConfiguration, first.config_id)
    assert config is not None
    config.status = "active"
    vault.active_config_id = first.config_id
    vault.pending_config_id = None
    vault.first_armed_at = datetime.now(UTC)
    db_session.flush()

    with pytest.raises(VigilRecipientsLocked):
        write_pending_configuration(
            db_session,
            owner_user_id=_OWNER_ID,
            owner_email="owner@example.com",
            expected_revision=first.revision,
            normalized=_normalized("different@example.com"),
        )


def test_recipients_locked_after_first_arming_allows_unchanged_list(db_session: Session) -> None:
    _owner(db_session)
    first = write_pending_configuration(
        db_session,
        owner_user_id=_OWNER_ID,
        owner_email="owner@example.com",
        expected_revision=0,
        normalized=_normalized("a@example.com"),
    )
    db_session.flush()
    vault = db_session.get(VigilVault, first.vault_id)
    assert vault is not None
    config = db_session.get(VigilConfiguration, first.config_id)
    assert config is not None
    config.status = "active"
    vault.active_config_id = first.config_id
    vault.pending_config_id = None
    vault.first_armed_at = datetime.now(UTC)
    db_session.flush()

    second = write_pending_configuration(
        db_session,
        owner_user_id=_OWNER_ID,
        owner_email="owner@example.com",
        expected_revision=first.revision,
        normalized=_normalized("a@example.com"),
    )
    assert second.config_id != first.config_id
