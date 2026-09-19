"""Vigil R0 P3.3 — three-round confirmation cycle (issue #459).

Real Postgres. Clock is injected; these tests never sleep() for gaps.
Acceptance P3.3-A01..A04.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.user import User
from app.models.vigil import (
    VigilActionToken,
    VigilAuditEvent,
    VigilCycle,
    VigilDeliveryEvent,
    VigilOutbox,
    VigilRound,
    VigilRuntime,
    VigilVault,
)
from app.services.vigil.configuration import (
    validate_configuration_input,
    write_pending_configuration,
)
from app.services.vigil.cycles import (
    SCAN_GAP,
    VigilCycleConflict,
    check_in,
    disarm,
    earliest_release_eligible_at,
    resume_held_vault,
    run_cycle_scan,
)
from app.services.vigil.dispatch import _decrypt_payload
from app.services.vigil.objects import init_object, upload_object
from app.services.vigil.tokens import hash_link_token
from app.tests.conftest import TEST_USER_ID

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SEP1 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
_GRACE = timedelta(hours=72)


@pytest.fixture(autouse=True)
def _vigil_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIGIL_MODE", "active")
    monkeypatch.setenv("VIGIL_OWNER_AUTH_SUBJECT", "owner-sub")
    monkeypatch.setenv("VIGIL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VIGIL_NOTIFICATION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _owner_user(db_session: Session) -> User:
    row = User(
        id=TEST_USER_ID,
        auth_provider="supabase",
        auth_subject="owner-sub",
        email="owner@example.com",
        status="active",
        locale="zh",
        base_currency="USD",
        report_cadence="mwf",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(row)
    db_session.flush()
    return row


def _b64url(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _manifest(vault_id: uuid.UUID, object_id: uuid.UUID) -> dict[str, Any]:
    return {
        "version": 1,
        "algorithm": "AES-256-GCM",
        "vault_id": str(vault_id),
        "object_id": str(object_id),
        "has_password": False,
        "file_nonce": _b64url(b"0" * 12),
        "salt": None,
        "kdf": None,
        "inner_nonce": None,
    }


def _heartbeat(db_session: Session, at: datetime, *, health: str = "ok") -> None:
    row = db_session.get(VigilRuntime, 1)
    if row is None:
        db_session.add(VigilRuntime(id=1, last_scan_completed_at=at, health=health))
    else:
        row.last_scan_completed_at = at
        row.health = health
        row.reason = None
    db_session.flush()


def _seed_armed(
    db_session: Session, *, next_check_at: datetime, grace_hours: int = 72
) -> VigilVault:
    cfg = write_pending_configuration(
        db_session,
        owner_user_id=TEST_USER_ID,
        owner_email="owner@example.com",
        expected_revision=0,
        normalized=validate_configuration_input(
            interval_days=30,
            grace_hours=grace_hours,
            recipients=[{"email": "a@example.com", "email_confirm": "a@example.com"}],
            message="",
        ),
    )
    db_session.flush()
    init = init_object(
        db_session,
        owner_user_id=TEST_USER_ID,
        expected_revision=cfg.revision,
        config_id=cfg.config_id,
        request_id=uuid.uuid4(),
        filename="will.pdf",
        plaintext_size=4,
    )
    db_session.flush()
    upload_object(
        db_session,
        owner_user_id=TEST_USER_ID,
        expected_revision=init.revision,
        object_id=init.object_id,
        config_id=cfg.config_id,
        manifest=_manifest(init.vault_id, init.object_id),
        inner_b64=_b64url(b"D" * 32),
        ciphertext=b"C" * 20,
    )
    db_session.flush()
    vault = db_session.execute(
        select(VigilVault).where(VigilVault.owner_user_id == TEST_USER_ID)
    ).scalar_one()
    from app.models.vigil import VigilConfiguration, VigilObject

    config = db_session.get(VigilConfiguration, cfg.config_id)
    obj = db_session.get(VigilObject, init.object_id)
    assert config is not None and obj is not None
    config.status = "active"
    obj.status = "active"
    obj.activated_at = next_check_at
    vault.active_config_id = config.id
    vault.active_object_id = obj.id
    vault.pending_config_id = None
    vault.pending_object_id = None
    vault.phase = "ARMED"
    vault.next_check_at = next_check_at
    vault.first_armed_at = next_check_at
    vault.retention_anchor_at = next_check_at
    vault.updated_at = next_check_at
    db_session.flush()
    return vault


def _current_cycle(db_session: Session, vault_id: uuid.UUID) -> VigilCycle:
    cycle = db_session.scalars(
        select(VigilCycle)
        .where(VigilCycle.vault_id == vault_id)
        .order_by(VigilCycle.created_at.desc())
    ).first()
    assert cycle is not None
    return cycle


def _rounds(db_session: Session, cycle_id: uuid.UUID) -> list[VigilRound]:
    return list(
        db_session.scalars(
            select(VigilRound)
            .where(VigilRound.cycle_id == cycle_id)
            .order_by(VigilRound.level, VigilRound.generation)
        ).all()
    )


def _open_round(db_session: Session, cycle_id: uuid.UUID, level: int) -> VigilRound:
    row = db_session.scalars(
        select(VigilRound).where(
            VigilRound.cycle_id == cycle_id,
            VigilRound.level == level,
            VigilRound.superseded_at.is_(None),
        )
    ).one()
    return row


def _credit_delivered(
    db_session: Session,
    outbox_id: uuid.UUID,
    *,
    anchor: datetime,
    first_attempt_at: datetime | None = None,
    event_type: str = "email.delivered",
) -> None:
    outbox = db_session.get(VigilOutbox, outbox_id)
    assert outbox is not None
    outbox.first_attempt_at = first_attempt_at or anchor
    outbox.status = "accepted"
    db_session.add(
        VigilDeliveryEvent(
            provider_event_id=f"evt-{uuid.uuid4()}",
            outbox_id=outbox_id,
            provider_message_id=f"msg-{uuid.uuid4()}",
            event_type=event_type,
            provider_at=anchor,
            received_at=anchor,
            evidence_source="webhook",
        )
    )
    db_session.flush()


def _scan(db_session: Session, now: datetime, *, recent: bool = True) -> None:
    if recent:
        _heartbeat(db_session, now - timedelta(seconds=30))
    run_cycle_scan(db_session, now=now)
    db_session.flush()


# --- P3.3-A01 / A08 --------------------------------------------------------


def test_p33_a01_three_windows_earliest_sep10_and_one_scan_never_creates_three_levels(
    db_session: Session,
) -> None:
    vault = _seed_armed(db_session, next_check_at=_SEP1)
    _heartbeat(db_session, _SEP1 - timedelta(seconds=30))

    _scan(db_session, _SEP1)
    cycle = _current_cycle(db_session, vault.id)
    assert cycle.current_level == 1
    assert vault.phase == "CHALLENGE_1"
    round1 = _open_round(db_session, cycle.id, 1)
    assert round1.deadline_at is None
    assert round1.generation == 1

    _heartbeat(db_session, _SEP1)
    _credit_delivered(db_session, round1.outbox_id, anchor=_SEP1)
    _scan(db_session, _SEP1 + timedelta(seconds=1))
    db_session.refresh(round1)
    assert round1.anchor_at == _SEP1
    assert round1.deadline_at == _SEP1 + _GRACE

    sep4 = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    _heartbeat(db_session, sep4 - timedelta(seconds=30))
    _scan(db_session, sep4)
    db_session.refresh(cycle)
    assert cycle.current_level == 2
    assert vault.phase == "CHALLENGE_2"
    round2 = _open_round(db_session, cycle.id, 2)
    assert round2.deadline_at is None

    sep4_1010 = datetime(2026, 9, 4, 12, 10, tzinfo=UTC)
    _heartbeat(db_session, sep4)
    _credit_delivered(db_session, round2.outbox_id, anchor=sep4_1010)
    _scan(db_session, sep4_1010)
    db_session.refresh(round2)
    assert round2.deadline_at == sep4_1010 + _GRACE

    sep7_1010 = datetime(2026, 9, 7, 12, 10, tzinfo=UTC)
    _heartbeat(db_session, sep7_1010 - timedelta(seconds=30))
    _scan(db_session, sep7_1010)
    db_session.refresh(cycle)
    assert cycle.current_level == 3
    assert vault.phase == "FINAL_WARNING"
    round3 = _open_round(db_session, cycle.id, 3)
    assert round3.deadline_at is None

    sep7_1215 = datetime(2026, 9, 7, 12, 15, tzinfo=UTC)
    _heartbeat(db_session, sep7_1010)
    _credit_delivered(db_session, round3.outbox_id, anchor=sep7_1215)
    _scan(db_session, sep7_1215)
    db_session.refresh(round3)
    assert round3.deadline_at == datetime(2026, 9, 10, 12, 15, tzinfo=UTC)
    assert earliest_release_eligible_at(db_session, cycle.id) == datetime(
        2026, 9, 10, 12, 15, tzinfo=UTC
    )
    assert len(_rounds(db_session, cycle.id)) == 3

    sep10_1215 = datetime(2026, 9, 10, 12, 15, tzinfo=UTC)
    _heartbeat(db_session, sep10_1215 - timedelta(seconds=30))
    _scan(db_session, sep10_1215)
    db_session.refresh(cycle)
    db_session.refresh(vault)
    assert cycle.current_level == 3
    assert len(_rounds(db_session, cycle.id)) == 3
    assert vault.phase == "FINAL_WARNING"
    assert vault.phase != "RELEASED"
    # #527: no scan-driven transition (cycle_opened/deadline_set/
    # level_advanced) writes vigil_audit_events any more.
    assert db_session.scalar(select(func.count()).select_from(VigilAuditEvent)) == 0


def test_p33_a01_single_scan_does_not_catch_up_three_levels(db_session: Session) -> None:
    vault = _seed_armed(db_session, next_check_at=_SEP1)
    late = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _scan(db_session, late)
    cycle = _current_cycle(db_session, vault.id)
    assert cycle.current_level == 1
    assert len(_rounds(db_session, cycle.id)) == 1
    assert vault.phase == "CHALLENGE_1"
    assert _open_round(db_session, cycle.id, 1).deadline_at is None
    _scan(db_session, late + timedelta(seconds=1))
    db_session.refresh(cycle)
    assert cycle.current_level == 1
    assert len(_rounds(db_session, cycle.id)) == 1


# --- P3.3-A02 / A07 --------------------------------------------------------


def test_p33_a02_no_delivery_means_no_deadline_negative_and_generation_block(
    db_session: Session,
) -> None:
    vault = _seed_armed(db_session, next_check_at=_SEP1)
    _scan(db_session, _SEP1)
    cycle = _current_cycle(db_session, vault.id)
    round1 = _open_round(db_session, cycle.id, 1)
    assert round1.deadline_at is None

    accepted_at = _SEP1 + timedelta(minutes=1)
    outbox = db_session.get(VigilOutbox, round1.outbox_id)
    assert outbox is not None
    outbox.status = "accepted"
    outbox.first_attempt_at = accepted_at
    outbox.accepted_at = accepted_at
    db_session.flush()
    _scan(db_session, accepted_at + timedelta(minutes=1))
    db_session.refresh(round1)
    assert round1.deadline_at is None
    assert round1.anchor_at is None

    _credit_delivered(db_session, round1.outbox_id, anchor=_SEP1, first_attempt_at=_SEP1)
    _scan(db_session, accepted_at + timedelta(minutes=2))
    db_session.refresh(round1)
    assert round1.deadline_at == _SEP1 + _GRACE

    sep4 = _SEP1 + _GRACE
    _scan(db_session, sep4)
    db_session.refresh(cycle)
    assert cycle.current_level == 2
    round2 = _open_round(db_session, cycle.id, 2)

    # Resume at level 2: new generation. Old outbox delivery must not credit it.
    vault.hold_reason = "scan_gap"
    vault.held_at = sep4
    db_session.flush()
    resume_held_vault(
        db_session, owner_user_id=TEST_USER_ID, expected_revision=vault.revision, now=sep4
    )
    db_session.flush()
    db_session.refresh(cycle)
    new_round2 = _open_round(db_session, cycle.id, 2)
    assert new_round2.id != round2.id
    assert new_round2.generation == 2
    assert new_round2.deadline_at is None
    _credit_delivered(db_session, round2.outbox_id, anchor=sep4 + timedelta(minutes=10))
    _heartbeat(db_session, sep4)
    _scan(db_session, sep4 + timedelta(minutes=1))
    db_session.refresh(new_round2)
    assert new_round2.deadline_at is None

    _credit_delivered(db_session, new_round2.outbox_id, anchor=sep4 + timedelta(minutes=10))
    _scan(db_session, sep4 + timedelta(minutes=11))
    db_session.refresh(new_round2)
    assert new_round2.deadline_at == sep4 + timedelta(minutes=10) + _GRACE

    due3 = new_round2.deadline_at
    assert due3 is not None
    _heartbeat(db_session, due3 - timedelta(seconds=30))
    _scan(db_session, due3)
    db_session.refresh(cycle)
    assert cycle.current_level == 3
    round3 = _open_round(db_session, cycle.id, 3)
    _credit_delivered(db_session, round3.outbox_id, anchor=due3 + timedelta(minutes=5))
    _heartbeat(db_session, due3)
    _scan(db_session, due3 + timedelta(minutes=5))
    db_session.refresh(round3)
    release_at = round3.deadline_at
    assert release_at is not None

    db_session.add(
        VigilDeliveryEvent(
            provider_event_id=f"bounce-{uuid.uuid4()}",
            outbox_id=round1.outbox_id,
            provider_message_id=f"bounce-{uuid.uuid4()}",
            event_type="email.bounced",
            provider_at=release_at,
            received_at=release_at,
            evidence_source="webhook",
        )
    )
    db_session.flush()
    _heartbeat(db_session, release_at - timedelta(seconds=30))
    _scan(db_session, release_at)
    db_session.refresh(vault)
    db_session.refresh(cycle)
    assert vault.phase == "FINAL_WARNING"
    assert vault.phase != "RELEASED"
    assert vault.hold_reason == "evidence_negative"
    assert cycle.current_level == 3


# --- P3.3-A03 / A12 --------------------------------------------------------


def test_p33_a03_ten_minute_gap_holds_before_overdue_and_level2_resume_does_not_skip(
    db_session: Session,
) -> None:
    vault = _seed_armed(db_session, next_check_at=_SEP1)
    _heartbeat(db_session, _SEP1 - timedelta(seconds=30))
    _scan(db_session, _SEP1)
    cycle = _current_cycle(db_session, vault.id)
    round1 = _open_round(db_session, cycle.id, 1)
    _credit_delivered(db_session, round1.outbox_id, anchor=_SEP1)
    _heartbeat(db_session, _SEP1)
    _scan(db_session, _SEP1 + timedelta(seconds=1))

    sep4 = _SEP1 + _GRACE
    _heartbeat(db_session, sep4 - timedelta(seconds=30))
    _scan(db_session, sep4)
    db_session.refresh(cycle)
    assert cycle.current_level == 2
    round2 = _open_round(db_session, cycle.id, 2)
    _credit_delivered(db_session, round2.outbox_id, anchor=sep4 + timedelta(minutes=10))
    _heartbeat(db_session, sep4)
    _scan(db_session, sep4 + timedelta(minutes=10))
    db_session.refresh(round2)
    due3 = round2.deadline_at
    assert due3 is not None

    gap_now = due3 + timedelta(minutes=10)
    last = gap_now - timedelta(minutes=10)
    assert gap_now - last == timedelta(minutes=10)
    assert gap_now - last >= SCAN_GAP
    _heartbeat(db_session, last)
    _scan(db_session, gap_now, recent=False)
    db_session.refresh(vault)
    db_session.refresh(cycle)
    assert vault.hold_reason == "scan_gap"
    assert cycle.current_level == 2
    assert vault.phase == "CHALLENGE_2"
    assert len(_rounds(db_session, cycle.id)) == 2

    resume_held_vault(
        db_session,
        owner_user_id=TEST_USER_ID,
        expected_revision=vault.revision,
        now=gap_now,
    )
    db_session.flush()
    db_session.refresh(cycle)
    db_session.refresh(vault)
    assert vault.hold_reason is None
    assert cycle.current_level == 2
    resumed = _open_round(db_session, cycle.id, 2)
    assert resumed.generation == 2
    assert resumed.deadline_at is None
    assert vault.phase == "CHALLENGE_2"

    _heartbeat(db_session, gap_now)
    _scan(db_session, gap_now + timedelta(seconds=30))
    db_session.refresh(cycle)
    assert cycle.current_level == 2
    assert vault.phase != "FINAL_WARNING"
    assert vault.phase != "RELEASED"


# --- P3.3-A04 --------------------------------------------------------------


def test_p33_a04_checkin_retry_does_not_reset_twice_hold_survives_released_cannot_rearm(
    app_client: TestClient, db_session: Session
) -> None:
    vault = _seed_armed(db_session, next_check_at=_SEP1)
    _heartbeat(db_session, _SEP1 - timedelta(seconds=30))
    _scan(db_session, _SEP1)
    _heartbeat(db_session, _SEP1)
    body = {"expected_revision": vault.revision}
    resp = app_client.post("/vigil/check-in", json=body)
    assert resp.status_code == 200, resp.text
    db_session.refresh(vault)
    clock = vault.last_owner_confirmed_at
    assert clock is not None
    assert vault.phase == "ARMED"
    assert vault.retention_anchor_at == clock
    revision_after = vault.revision
    next_check = vault.next_check_at
    cycle = _current_cycle(db_session, vault.id)
    assert cycle.status == "confirmed"

    retry = app_client.post(
        "/vigil/check-in", json={"expected_revision": body["expected_revision"]}
    )
    assert retry.status_code == 409
    db_session.refresh(vault)
    assert vault.last_owner_confirmed_at == clock
    assert vault.revision == revision_after
    assert vault.next_check_at == next_check

    vault.hold_reason = "scan_gap"
    vault.held_at = _SEP1
    db_session.flush()
    held = app_client.post("/vigil/check-in", json={"expected_revision": vault.revision})
    assert held.status_code == 200, held.text
    db_session.refresh(vault)
    assert vault.hold_reason == "scan_gap"
    assert vault.phase == "ARMED"

    vault.phase = "RELEASED"
    db_session.flush()
    with pytest.raises(VigilCycleConflict) as released:
        check_in(db_session, owner_user_id=TEST_USER_ID, expected_revision=vault.revision)
    assert released.value.detail == {"error": "released", "action": "revoke"}
    assert vault.phase == "RELEASED"


def test_p33_a04_disarm_before_release_cancels_cycle_after_release_409(
    app_client: TestClient, db_session: Session
) -> None:
    vault = _seed_armed(db_session, next_check_at=_SEP1)
    _heartbeat(db_session, _SEP1 - timedelta(seconds=30))
    _scan(db_session, _SEP1)
    cycle = _current_cycle(db_session, vault.id)
    resp = app_client.post("/vigil/disarm", json={"expected_revision": vault.revision})
    assert resp.status_code == 200, resp.text
    db_session.refresh(vault)
    db_session.refresh(cycle)
    assert vault.phase == "DISARMED"
    assert cycle.status == "cancelled"
    assert vault.next_check_at is None

    vault.phase = "RELEASED"
    db_session.flush()
    with pytest.raises(VigilCycleConflict):
        disarm(db_session, owner_user_id=TEST_USER_ID, expected_revision=vault.revision)
    assert vault.phase == "RELEASED"


def test_p33_a04_disarmed_cannot_check_in(db_session: Session) -> None:
    vault = _seed_armed(db_session, next_check_at=_SEP1)
    vault.phase = "DISARMED"
    vault.next_check_at = None
    db_session.flush()
    with pytest.raises(VigilCycleConflict):
        check_in(db_session, owner_user_id=TEST_USER_ID, expected_revision=vault.revision)
    assert vault.phase == "DISARMED"
    assert vault.last_owner_confirmed_at is None


def test_p33_public_cycle_confirm_replay_is_already_resolved(
    app_client: TestClient, db_session: Session
) -> None:
    vault = _seed_armed(db_session, next_check_at=_SEP1)
    _heartbeat(db_session, _SEP1 - timedelta(seconds=30))
    _scan(db_session, _SEP1)
    cycle = _current_cycle(db_session, vault.id)
    round1 = _open_round(db_session, cycle.id, 1)
    outbox = db_session.get(VigilOutbox, round1.outbox_id)
    assert outbox is not None
    token = _decrypt_payload(outbox).token
    origin = {"Origin": get_settings().FRONTEND_URL.rstrip("/")}
    status = app_client.post(
        "/vigil/public/status",
        headers=origin,
        json={"token": token, "action": "confirm"},
    )
    assert status.status_code == 200, status.text
    nonce = status.json()["nonce"]
    from typing import cast

    from altcha import v1 as altcha_v1
    from altcha.v1 import AlgoType

    challenge_resp = app_client.get("/vigil/public/altcha-challenge", headers=origin)
    challenge = challenge_resp.json()
    algorithm = cast(AlgoType, challenge["algorithm"])
    solution = altcha_v1.solve_challenge(
        challenge=challenge["challenge"],
        salt=challenge["salt"],
        algorithm=algorithm,
        max_number=challenge["maxNumber"],
    )
    assert solution is not None
    altcha = altcha_v1.Payload(
        algorithm=algorithm,
        challenge=challenge["challenge"],
        number=solution.number,
        salt=challenge["salt"],
        signature=challenge["signature"],
    ).to_base64()
    first = app_client.post(
        "/vigil/public/confirm",
        headers=origin,
        json={"token": token, "nonce": nonce, "altcha": altcha},
    )
    assert first.status_code == 200, first.text
    assert first.json()["result"] == "confirmed"
    db_session.refresh(vault)
    clock = vault.last_owner_confirmed_at
    assert clock is not None

    replay = app_client.post(
        "/vigil/public/confirm",
        headers=origin,
        json={"token": token, "nonce": nonce, "altcha": altcha},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["result"] == "already_resolved"
    db_session.refresh(vault)
    assert vault.last_owner_confirmed_at == clock


def test_cycle_unique_one_active_per_vault(db_session: Session) -> None:
    vault = _seed_armed(db_session, next_check_at=_SEP1)
    _heartbeat(db_session, _SEP1 - timedelta(seconds=30))
    _scan(db_session, _SEP1)
    cycle = _current_cycle(db_session, vault.id)
    db_session.add(
        VigilCycle(
            vault_id=vault.id,
            config_id=cycle.config_id,
            object_id=cycle.object_id,
            status="active",
            current_level=1,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_round_unique_level_generation(db_session: Session) -> None:
    vault = _seed_armed(db_session, next_check_at=_SEP1)
    _heartbeat(db_session, _SEP1 - timedelta(seconds=30))
    _scan(db_session, _SEP1)
    cycle = _current_cycle(db_session, vault.id)
    round1 = _open_round(db_session, cycle.id, 1)
    db_session.add(
        VigilRound(
            cycle_id=cycle.id,
            level=1,
            generation=1,
            outbox_id=round1.outbox_id,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_dispatch_unattempted_and_undelivered_holds(db_session: Session) -> None:
    vault = _seed_armed(db_session, next_check_at=_SEP1)
    _heartbeat(db_session, _SEP1 - timedelta(seconds=30))
    _scan(db_session, _SEP1)
    five_later = _SEP1 + timedelta(minutes=5)
    _heartbeat(db_session, five_later - timedelta(seconds=30))
    _scan(db_session, five_later)
    db_session.refresh(vault)
    assert vault.hold_reason == "dispatch_unattempted"
    assert vault.phase == "CHALLENGE_1"

    resume_held_vault(
        db_session, owner_user_id=TEST_USER_ID, expected_revision=vault.revision, now=five_later
    )
    db_session.flush()
    cycle = _current_cycle(db_session, vault.id)
    round1 = _open_round(db_session, cycle.id, 1)
    outbox = db_session.get(VigilOutbox, round1.outbox_id)
    assert outbox is not None
    outbox.first_attempt_at = five_later
    outbox.status = "accepted"
    db_session.flush()
    thirty = five_later + timedelta(minutes=30)
    _heartbeat(db_session, thirty - timedelta(seconds=30))
    _scan(db_session, thirty)
    db_session.refresh(vault)
    assert vault.hold_reason == "delivery_missing"
    db_session.refresh(round1)
    assert round1.deadline_at is None


def test_old_matching_cycle_token_still_confirms_after_resume(
    app_client: TestClient, db_session: Session
) -> None:
    vault = _seed_armed(db_session, next_check_at=_SEP1)
    _heartbeat(db_session, _SEP1 - timedelta(seconds=30))
    _scan(db_session, _SEP1)
    cycle = _current_cycle(db_session, vault.id)
    round1 = _open_round(db_session, cycle.id, 1)
    outbox = db_session.get(VigilOutbox, round1.outbox_id)
    assert outbox is not None
    old_token = _decrypt_payload(outbox).token
    vault.hold_reason = "scan_gap"
    vault.held_at = _SEP1
    db_session.flush()
    resume_held_vault(
        db_session, owner_user_id=TEST_USER_ID, expected_revision=vault.revision, now=_SEP1
    )
    db_session.flush()
    still = db_session.scalars(
        select(VigilActionToken).where(VigilActionToken.token_hash == hash_link_token(old_token))
    ).one()
    assert still.invalidated_at is None

    origin = {"Origin": get_settings().FRONTEND_URL.rstrip("/")}
    status = app_client.post(
        "/vigil/public/status",
        headers=origin,
        json={"token": old_token, "action": "confirm"},
    )
    assert status.status_code == 200, status.text


def test_scan_is_on_existing_beat_every_60s() -> None:
    from app.tasks import celery_app

    entry = celery_app.conf.beat_schedule["scan-vigil-cycles"]
    assert entry["task"] == "app.tasks.vigil_tasks.scan_vigil_cycles_task"
    assert entry["schedule"] == 60.0


def test_no_new_compose_service_or_worker() -> None:
    compose = yaml.safe_load((_REPO_ROOT / "docker-compose.yml").read_text())
    assert set(compose["services"]) == {
        "postgres",
        "redis",
        "migrate",
        "backend",
        "celery-worker",
        "celery-beat",
        "frontend",
        "caddy",
    }
    assert celery_worker_count() == 1


def celery_worker_count() -> int:
    compose = yaml.safe_load((_REPO_ROOT / "docker-compose.yml").read_text())
    return sum(1 for name in compose["services"] if "celery-worker" in name)


def test_purge_deletes_cycles_and_rounds(db_session: Session) -> None:
    from app.services.user_purge import purge_user

    vault = _seed_armed(db_session, next_check_at=_SEP1)
    _heartbeat(db_session, _SEP1 - timedelta(seconds=30))
    _scan(db_session, _SEP1)
    result = purge_user(db_session, TEST_USER_ID)
    db_session.rollback()
    assert result.vigil_cycles == 1
    assert result.vigil_rounds == 1
    assert db_session.scalar(select(func.count()).select_from(VigilCycle)) == 0
    assert db_session.scalar(select(func.count()).select_from(VigilRound)) == 0
    del vault


def test_owner_and_scan_transitions_write_no_audit_rows(db_session: Session) -> None:
    """#527: check_in / disarm / resume keep their evidence in the business
    rows only — nothing in the cycle path inserts a VigilAuditEvent."""
    vault = _seed_armed(db_session, next_check_at=_SEP1)
    _heartbeat(db_session, _SEP1 - timedelta(seconds=30))
    _scan(db_session, _SEP1)
    check_in(db_session, owner_user_id=TEST_USER_ID, expected_revision=vault.revision)
    db_session.refresh(vault)
    vault.hold_reason = "scan_gap"
    vault.held_at = _SEP1
    db_session.flush()
    resume_held_vault(db_session, owner_user_id=TEST_USER_ID, expected_revision=vault.revision)
    db_session.refresh(vault)
    disarm(db_session, owner_user_id=TEST_USER_ID, expected_revision=vault.revision)
    assert db_session.scalar(select(func.count()).select_from(VigilAuditEvent)) == 0
