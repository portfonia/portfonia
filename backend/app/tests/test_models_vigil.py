"""VigilVault/VigilRuntime/VigilAuditEvent — base schema (issue #451, P1.1).

Only these three tables exist at this checkpoint; no business logic
(arm/release/etc.) reads or writes them yet. These tests prove the schema
contract from #450 Design section 4 / Vigil_R0_Dev.md Appendix A: column
defaults, CHECK constraints, and the FK boundaries later checkpoints (and
`purge_user`) depend on.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.vigil import VigilAuditEvent, VigilRuntime, VigilVault

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
    """Real FK: vigil_audit_events.vault_id -> vigil_vaults.id."""
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
