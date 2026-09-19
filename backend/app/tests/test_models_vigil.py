"""VigilVault/VigilRuntime/VigilAuditEvent — base schema (issue #451, P1.1).

Only these three tables exist at this checkpoint; no business logic
(arm/release/etc.) reads or writes them yet. These tests prove the schema
contract from #450 Design section 4 / Vigil_R0_Dev.md Appendix A: column
defaults, CHECK constraints, and the FK boundaries later checkpoints (and
`purge_user`) depend on.

`vigil_audit_events` is deprecated/unused since #527 (no writer) — the
schema tests below still hold because the table itself is not dropped, and
`purge_user` still deletes pre-#527 rows (vault_id is ON DELETE RESTRICT).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.vigil import (
    VigilAuditEvent,
    VigilConfiguration,
    VigilObject,
    VigilRuntime,
    VigilVault,
)

_A = uuid.UUID("00000000-0000-0000-0000-0000000000e1")


def _user(user_id: uuid.UUID, email: str) -> User:
    return User(
        id=user_id,
        auth_provider="supabase",
        auth_subject=f"sub-{user_id}",
        email=email,
        status="active",
        locale="zh",
        base_currency="USD",
        report_cadence="mwf",
    )


def test_vault_defaults_disarmed_revision_zero(db_session: Session) -> None:
    db_session.add(_user(_A, "a@example.com"))
    db_session.flush()
    vault = VigilVault(owner_user_id=_A)
    db_session.add(vault)
    db_session.flush()
    db_session.refresh(vault)
    assert vault.phase == "DISARMED"
    assert vault.revision == 0
    assert vault.active_config_id is None
    assert vault.active_object_id is None
    assert vault.pending_config_id is None
    assert vault.pending_object_id is None
    assert vault.next_check_at is None
    assert vault.hold_reason is None


def test_vault_owner_user_id_is_unique(db_session: Session) -> None:
    db_session.add(_user(_A, "a@example.com"))
    db_session.flush()
    db_session.add(VigilVault(owner_user_id=_A))
    db_session.flush()
    db_session.add(VigilVault(owner_user_id=_A))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_vault_phase_check_constraint_rejects_unknown_value(db_session: Session) -> None:
    db_session.add(_user(_A, "a@example.com"))
    db_session.flush()
    db_session.add(VigilVault(owner_user_id=_A, phase="NOT_A_PHASE"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_vault_revision_check_constraint_rejects_negative(db_session: Session) -> None:
    db_session.add(_user(_A, "a@example.com"))
    db_session.flush()
    db_session.add(VigilVault(owner_user_id=_A, revision=-1))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_runtime_singleton_id_check_constraint(db_session: Session) -> None:
    db_session.add(VigilRuntime(id=2))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_runtime_health_defaults_ok(db_session: Session) -> None:
    row = VigilRuntime(id=1)
    db_session.add(row)
    db_session.flush()
    db_session.refresh(row)
    assert row.health == "ok"
    assert row.last_scan_completed_at is None
    assert row.last_dispatch_sweep_at is None
    assert row.reason is None


def test_runtime_health_check_constraint_rejects_unknown_value(db_session: Session) -> None:
    db_session.add(VigilRuntime(id=1, health="not-a-health-value"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_audit_event_requires_existing_vault(db_session: Session) -> None:
    """Real FK: vigil_audit_events.vault_id -> vigil_vaults.id (table kept
    as an abandoned-in-place table since #527)."""
    db_session.add(
        VigilAuditEvent(
            vault_id=uuid.uuid4(),
            sequence=1,
            action="test",
            actor_type="system",
            revision=0,
            detail={},
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_audit_event_unique_vault_sequence(db_session: Session) -> None:
    db_session.add(_user(_A, "a@example.com"))
    db_session.flush()
    vault = VigilVault(owner_user_id=_A)
    db_session.add(vault)
    db_session.flush()
    db_session.add(
        VigilAuditEvent(
            vault_id=vault.id, sequence=1, action="a", actor_type="system", revision=0, detail={}
        )
    )
    db_session.flush()
    db_session.add(
        VigilAuditEvent(
            vault_id=vault.id, sequence=1, action="b", actor_type="system", revision=0, detail={}
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_audit_event_actor_type_check_constraint(db_session: Session) -> None:
    db_session.add(_user(_A, "a@example.com"))
    db_session.flush()
    vault = VigilVault(owner_user_id=_A)
    db_session.add(vault)
    db_session.flush()
    db_session.add(
        VigilAuditEvent(
            vault_id=vault.id,
            sequence=1,
            action="a",
            actor_type="not-a-real-actor",
            revision=0,
            detail={},
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


# --- vigil_configurations / vigil_objects (issue #454, P2.1) ---


def _vault(session: Session, user_id: uuid.UUID, email: str) -> VigilVault:
    session.add(_user(user_id, email))
    session.flush()
    vault = VigilVault(owner_user_id=user_id)
    session.add(vault)
    session.flush()
    return vault


def _config(
    vault_id: uuid.UUID, *, config_revision: int = 1, status: str = "pending"
) -> VigilConfiguration:
    return VigilConfiguration(
        id=uuid.uuid4(),
        vault_id=vault_id,
        config_revision=config_revision,
        status=status,
        data_cipher="not-real-ciphertext",
    )


def test_configuration_status_check_constraint(db_session: Session) -> None:
    vault = _vault(db_session, _A, "a@example.com")
    db_session.add(_config(vault.id, status="not-a-status"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_configuration_unique_vault_id_config_revision(db_session: Session) -> None:
    vault = _vault(db_session, _A, "a@example.com")
    db_session.add(_config(vault.id, config_revision=1, status="retired"))
    db_session.flush()
    db_session.add(_config(vault.id, config_revision=1, status="pending"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_configuration_at_most_one_pending_per_vault(db_session: Session) -> None:
    vault = _vault(db_session, _A, "a@example.com")
    db_session.add(_config(vault.id, config_revision=1, status="pending"))
    db_session.flush()
    db_session.add(_config(vault.id, config_revision=2, status="pending"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_configuration_at_most_one_active_per_vault(db_session: Session) -> None:
    vault = _vault(db_session, _A, "a@example.com")
    db_session.add(_config(vault.id, config_revision=1, status="active"))
    db_session.flush()
    db_session.add(_config(vault.id, config_revision=2, status="active"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_configuration_requires_existing_vault(db_session: Session) -> None:
    db_session.add(_config(uuid.uuid4(), config_revision=1))
    with pytest.raises(IntegrityError):
        db_session.flush()


def _staging_object(
    vault_id: uuid.UUID, config_id: uuid.UUID, *, request_id: uuid.UUID | None = None
) -> VigilObject:
    return VigilObject(
        id=uuid.uuid4(),
        vault_id=vault_id,
        config_id=config_id,
        request_id=request_id or uuid.uuid4(),
        status="staging",
        filename_cipher="not-real-ciphertext",
        plaintext_size=100,
    )


def test_object_status_check_constraint(db_session: Session) -> None:
    vault = _vault(db_session, _A, "a@example.com")
    config = _config(vault.id)
    db_session.add(config)
    db_session.flush()
    obj = _staging_object(vault.id, config.id)
    obj.status = "not-a-status"
    db_session.add(obj)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_object_plaintext_size_bounds(db_session: Session) -> None:
    vault = _vault(db_session, _A, "a@example.com")
    config = _config(vault.id)
    db_session.add(config)
    db_session.flush()
    obj = _staging_object(vault.id, config.id)
    obj.plaintext_size = -1
    db_session.add(obj)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_object_plaintext_size_over_max_rejected(db_session: Session) -> None:
    vault = _vault(db_session, _A, "a@example.com")
    config = _config(vault.id)
    db_session.add(config)
    db_session.flush()
    obj = _staging_object(vault.id, config.id)
    obj.plaintext_size = 10_000_001
    db_session.add(obj)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_object_unique_vault_id_request_id(db_session: Session) -> None:
    vault = _vault(db_session, _A, "a@example.com")
    config = _config(vault.id)
    db_session.add(config)
    db_session.flush()
    rid = uuid.uuid4()
    db_session.add(_staging_object(vault.id, config.id, request_id=rid))
    db_session.flush()
    db_session.add(_staging_object(vault.id, config.id, request_id=rid))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_object_at_most_one_pending_per_vault_staging_or_ready(db_session: Session) -> None:
    vault = _vault(db_session, _A, "a@example.com")
    config = _config(vault.id)
    db_session.add(config)
    db_session.flush()
    db_session.add(_staging_object(vault.id, config.id))
    db_session.flush()
    db_session.add(_staging_object(vault.id, config.id))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_object_ready_requires_all_crypto_fields(db_session: Session) -> None:
    """crypto_fields_present_when_ready CHECK: a 'ready' row with any crypto
    field missing is rejected at the DB level (Appendix A)."""
    vault = _vault(db_session, _A, "a@example.com")
    config = _config(vault.id)
    db_session.add(config)
    db_session.flush()
    obj = _staging_object(vault.id, config.id)
    obj.status = "ready"
    db_session.add(obj)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_object_ready_requires_ciphertext_length_equals_plaintext_plus_tag(
    db_session: Session,
) -> None:
    vault = _vault(db_session, _A, "a@example.com")
    config = _config(vault.id)
    db_session.add(config)
    db_session.flush()
    obj = _staging_object(vault.id, config.id)
    obj.status = "ready"
    obj.ciphertext_size = 116
    obj.cipher_sha256 = "0" * 64
    obj.manifest = {"version": 1}
    obj.outer_cipher = "not-real-ciphertext"
    obj.ciphertext = b"0" * 115  # should be 100 + 16 = 116
    db_session.add(obj)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_object_ready_with_correct_ciphertext_length_succeeds(db_session: Session) -> None:
    vault = _vault(db_session, _A, "a@example.com")
    config = _config(vault.id)
    db_session.add(config)
    db_session.flush()
    obj = _staging_object(vault.id, config.id)
    obj.status = "ready"
    obj.ciphertext_size = 116
    obj.cipher_sha256 = "0" * 64
    obj.manifest = {"version": 1}
    obj.outer_cipher = "not-real-ciphertext"
    obj.ciphertext = b"0" * 116
    db_session.add(obj)
    db_session.flush()


def test_object_requires_existing_config(db_session: Session) -> None:
    vault = _vault(db_session, _A, "a@example.com")
    db_session.add(_staging_object(vault.id, uuid.uuid4()))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_vault_active_config_id_is_real_fk(db_session: Session) -> None:
    vault = _vault(db_session, _A, "a@example.com")
    vault.active_config_id = uuid.uuid4()
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_vault_pending_object_id_is_real_fk(db_session: Session) -> None:
    vault = _vault(db_session, _A, "a@example.com")
    vault.pending_object_id = uuid.uuid4()
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_vault_pending_config_id_accepts_real_configuration(db_session: Session) -> None:
    vault = _vault(db_session, _A, "a@example.com")
    config = _config(vault.id)
    db_session.add(config)
    db_session.flush()
    vault.pending_config_id = config.id
    db_session.flush()
