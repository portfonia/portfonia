"""Three-round confirmation cycle and shared-worker scan (issue #459, P3.3).

Scheduling/state-machine only: at most one level per scan, no catch-up
release, no P4.1 grant materialization. Clock is injectable for tests.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.user import User
from app.models.vigil import (
    VigilActionToken,
    VigilConfiguration,
    VigilCycle,
    VigilObject,
    VigilOutbox,
    VigilRound,
    VigilRuntime,
    VigilVault,
)
from app.services.vigil.access import is_vigil_owner_eligible
from app.services.vigil.configuration import (
    VigilRevisionConflict,
    load_configuration_data,
    normalize_email,
)
from app.services.vigil.delivery import evaluate_delivery_evidence
from app.services.vigil.dispatch import cancel_outbox_intents, write_outbox_entry
from app.services.vigil.events import emit_vigil_event
from app.services.vigil.tokens import (
    VigilNonceError,
    consume_nonce,
    db_now,
    hash_link_token,
    mint_link_token,
    rfc3339_z,
    verify_signed_nonce,
)

SCAN_GAP = timedelta(minutes=5)
MISSING_DELIVERY_HOLD = timedelta(minutes=30)
UNATTEMPTED_DISPATCH_HOLD = timedelta(minutes=5)
_CHALLENGE_PURPOSE = "challenge"
_CYCLE_CONFIRM = "cycle_confirm"
_LEVEL_PHASE = {1: "CHALLENGE_1", 2: "CHALLENGE_2", 3: "FINAL_WARNING"}
_SCAN_PHASES = frozenset({"ARMED", "CHALLENGE_1", "CHALLENGE_2", "FINAL_WARNING"})
HOLD_SCAN_GAP = "scan_gap"
HOLD_DELIVERY_MISSING = "delivery_missing"
HOLD_DISPATCH_UNATTEMPTED = "dispatch_unattempted"
HOLD_RELEASE_UNAVAILABLE = "release_unavailable"
HOLD_EVIDENCE_NEGATIVE = "evidence_negative"
HOLD_ACCOUNT_INELIGIBLE = "account_ineligible"


class VigilCycleConflict(RuntimeError):
    """Stale revision or illegal phase for check-in/disarm (-> 409)."""

    def __init__(self, detail: str | dict[str, object]) -> None:
        super().__init__(detail if isinstance(detail, str) else str(detail))
        self.detail = detail


class VigilCycleUnavailable(RuntimeError):
    """Feature/account cannot run the cycle action (-> 503)."""


@dataclass(frozen=True)
class CycleActionResult:
    phase: str
    revision: int
    next_check_at: str | None
    result: str = "ok"


@dataclass(frozen=True)
class PublicCycleConfirmResult:
    result: str
    next_check_at: str | None


def _lock_user_and_vault(session: Session, owner_user_id: UUID) -> tuple[User, VigilVault]:
    user = session.execute(
        select(User).where(User.id == owner_user_id).with_for_update()
    ).scalar_one_or_none()
    if user is None:
        raise VigilCycleUnavailable("owner is not available")
    vault = session.scalars(
        select(VigilVault).where(VigilVault.owner_user_id == owner_user_id).with_for_update()
    ).one_or_none()
    if vault is None:
        raise VigilCycleConflict("no vault")
    return user, vault


def _lock_cycle_graph(session: Session, vault: VigilVault, cycle: VigilCycle) -> None:
    session.execute(
        select(VigilConfiguration).where(VigilConfiguration.id == cycle.config_id).with_for_update()
    ).scalar_one()
    session.execute(
        select(VigilObject).where(VigilObject.id == cycle.object_id).with_for_update()
    ).scalar_one()
    session.execute(
        select(VigilCycle).where(VigilCycle.id == cycle.id).with_for_update()
    ).scalar_one()
    round_ids = list(
        session.scalars(
            select(VigilRound.id).where(VigilRound.cycle_id == cycle.id).order_by(VigilRound.id)
        ).all()
    )
    if round_ids:
        session.scalars(
            select(VigilRound)
            .where(VigilRound.id.in_(round_ids))
            .order_by(VigilRound.id)
            .with_for_update()
        ).all()
        outbox_ids = list(
            session.scalars(
                select(VigilRound.outbox_id)
                .where(VigilRound.cycle_id == cycle.id)
                .order_by(VigilRound.outbox_id)
            ).all()
        )
        if outbox_ids:
            session.scalars(
                select(VigilOutbox)
                .where(VigilOutbox.id.in_(outbox_ids))
                .order_by(VigilOutbox.id)
                .with_for_update()
            ).all()
    session.scalars(
        select(VigilActionToken)
        .where(VigilActionToken.cycle_id == cycle.id)
        .order_by(VigilActionToken.id)
        .with_for_update()
    ).all()


def _active_cycle(session: Session, vault_id: UUID) -> VigilCycle | None:
    return session.scalars(
        select(VigilCycle)
        .where(VigilCycle.vault_id == vault_id, VigilCycle.status == "active")
        .with_for_update()
    ).one_or_none()


def _open_round_row(session: Session, cycle_id: UUID, level: int) -> VigilRound | None:
    return session.scalars(
        select(VigilRound).where(
            VigilRound.cycle_id == cycle_id,
            VigilRound.level == level,
            VigilRound.superseded_at.is_(None),
        )
    ).one_or_none()


def _grace_hours(session: Session, vault: VigilVault, config_id: UUID) -> int:
    config = session.get(VigilConfiguration, config_id)
    if config is None:
        raise VigilCycleUnavailable("configuration is missing")
    data = load_configuration_data(config, vault.id)
    grace = data.get("grace_hours")
    if not isinstance(grace, int):
        raise VigilCycleUnavailable("configuration grace is invalid")
    return grace


def _interval_days(session: Session, vault: VigilVault, config_id: UUID) -> int:
    config = session.get(VigilConfiguration, config_id)
    if config is None:
        raise VigilCycleUnavailable("configuration is missing")
    data = load_configuration_data(config, vault.id)
    interval = data.get("interval_days")
    if not isinstance(interval, int):
        raise VigilCycleUnavailable("configuration interval is invalid")
    return interval


def _account_ok(session: Session, user: User, vault: VigilVault, config_id: UUID) -> bool:
    if not is_vigil_owner_eligible(session, user.id):
        return False
    config = session.get(VigilConfiguration, config_id)
    if config is None:
        return False
    data = load_configuration_data(config, vault.id)
    account_email = data.get("account_email")
    if not isinstance(account_email, str):
        return False
    try:
        return normalize_email(user.email) == normalize_email(account_email)
    except ValueError:
        return False


def _apply_hold(vault: VigilVault, reason: str, now: datetime) -> None:
    if vault.hold_reason is None:
        vault.hold_reason = reason
        vault.held_at = now
        vault.updated_at = now
        vault.revision += 1


def _runtime_row(session: Session) -> VigilRuntime:
    row = session.get(VigilRuntime, 1)
    if row is None:
        row = VigilRuntime(id=1)
        session.add(row)
        session.flush()
    return row


def cancel_active_cycles(
    session: Session,
    vault: VigilVault,
    *,
    now: datetime,
    status: str = "cancelled",
) -> list[UUID]:
    """Cancel active cycles and their pending challenge intents. Caller holds locks."""
    cycles = list(
        session.scalars(
            select(VigilCycle)
            .where(VigilCycle.vault_id == vault.id, VigilCycle.status == "active")
            .order_by(VigilCycle.id)
            .with_for_update()
        ).all()
    )
    ids = [c.id for c in cycles]
    for cycle in cycles:
        _lock_cycle_graph(session, vault, cycle)
        cycle.status = status
        cycle.resolved_at = now
        for token in session.scalars(
            select(VigilActionToken).where(
                VigilActionToken.cycle_id == cycle.id,
                VigilActionToken.purpose == _CYCLE_CONFIRM,
                VigilActionToken.invalidated_at.is_(None),
                VigilActionToken.confirmed_at.is_(None),
            )
        ).all():
            token.invalidated_at = now
        cancel_outbox_intents(session, vault_id=vault.id, purpose=_CHALLENGE_PURPOSE)
    if cycles:
        session.flush()
    return ids


def _mint_round(
    session: Session,
    *,
    vault: VigilVault,
    cycle: VigilCycle,
    user: User,
    level: int,
    generation: int,
    now: datetime,
) -> VigilRound:
    round_id = uuid.uuid4()
    presented, token_hash = mint_link_token()
    token_row = VigilActionToken(
        vault_id=vault.id,
        config_id=cycle.config_id,
        object_id=cycle.object_id,
        cycle_id=cycle.id,
        purpose=_CYCLE_CONFIRM,
        token_hash=token_hash,
        created_at=now,
    )
    session.add(token_row)
    session.flush()
    settings = get_settings()
    confirm_url = f"{settings.FRONTEND_URL.rstrip('/')}/vigil/confirm#{presented}"
    subject = "Portfonia Vigil confirmation required"
    text_body = (
        "A Vigil confirmation round is waiting on your account address.\n"
        f"Open: {confirm_url}\n"
        "This does not release any file."
    )
    html_body = (
        "<p>A Vigil confirmation round is waiting on your account address.</p>"
        f'<p><a href="{confirm_url}">Confirm</a></p>'
        "<p>This does not release any file.</p>"
    )
    outbox = write_outbox_entry(
        session,
        vault=vault,
        config_id=cycle.config_id,
        object_id=cycle.object_id,
        scope_id=round_id,
        purpose=_CHALLENGE_PURPOSE,
        dedup_key=f"challenge:{round_id}",
        recipient_email=user.email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        token=presented,
    )
    outbox.created_at = now
    row = VigilRound(
        id=round_id,
        cycle_id=cycle.id,
        level=level,
        generation=generation,
        outbox_id=outbox.id,
        created_at=now,
    )
    session.add(row)
    session.flush()
    return row


def _open_level_one(session: Session, *, user: User, vault: VigilVault, now: datetime) -> None:
    if vault.active_config_id is None or vault.active_object_id is None:
        return
    session.execute(
        select(VigilConfiguration)
        .where(VigilConfiguration.id == vault.active_config_id)
        .with_for_update()
    ).scalar_one()
    session.execute(
        select(VigilObject).where(VigilObject.id == vault.active_object_id).with_for_update()
    ).scalar_one()
    if not _account_ok(session, user, vault, vault.active_config_id):
        _apply_hold(vault, HOLD_ACCOUNT_INELIGIBLE, now)
        return
    cycle = VigilCycle(
        vault_id=vault.id,
        config_id=vault.active_config_id,
        object_id=vault.active_object_id,
        status="active",
        current_level=1,
        created_at=now,
    )
    session.add(cycle)
    session.flush()
    _mint_round(session, vault=vault, cycle=cycle, user=user, level=1, generation=1, now=now)
    from_phase = vault.phase
    vault.phase = "CHALLENGE_1"
    vault.updated_at = now
    vault.revision += 1
    emit_vigil_event(
        "vigil.cycle_opened",
        actor="system",
        vault_id=vault.id,
        from_phase=from_phase,
        to_phase=vault.phase,
        cycle_id=cycle.id,
        level=1,
    )


def _set_deadline_or_hold(
    session: Session,
    *,
    vault: VigilVault,
    cycle: VigilCycle,
    now: datetime,
) -> bool:
    current = _open_round_row(session, cycle.id, cycle.current_level)
    if current is None:
        return False
    if current.deadline_at is not None:
        return False
    evidence = evaluate_delivery_evidence(session, current.outbox_id, now=now)
    if evidence.usable and evidence.anchor_at is not None:
        grace = _grace_hours(session, vault, cycle.config_id)
        current.anchor_at = evidence.anchor_at
        current.deadline_at = evidence.anchor_at + timedelta(hours=grace)
        vault.updated_at = now
        vault.revision += 1
        emit_vigil_event(
            "vigil.deadline_set",
            actor="system",
            vault_id=vault.id,
            from_phase=vault.phase,
            to_phase=vault.phase,
            cycle_id=cycle.id,
            round_id=current.id,
            level=current.level,
        )
        return True
    if evidence.reason == "negative":
        _apply_hold(vault, HOLD_EVIDENCE_NEGATIVE, now)
        return True
    outbox = session.get(VigilOutbox, current.outbox_id)
    if outbox is None:
        return False
    if outbox.first_attempt_at is None:
        created = outbox.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if now - created >= UNATTEMPTED_DISPATCH_HOLD:
            _apply_hold(vault, HOLD_DISPATCH_UNATTEMPTED, now)
            return True
        return False
    if now - outbox.first_attempt_at >= MISSING_DELIVERY_HOLD:
        _apply_hold(vault, HOLD_DELIVERY_MISSING, now)
        return True
    return False


def _relied_on_rounds(session: Session, cycle: VigilCycle) -> list[VigilRound]:
    rows = []
    for level in range(1, cycle.current_level + 1):
        row = _open_round_row(session, cycle.id, level)
        if row is not None:
            rows.append(row)
    return rows


def _windows_complete(session: Session, cycle: VigilCycle, *, now: datetime) -> str | None:
    """None if all current unsuperseded rounds are valid and fully waited."""
    if cycle.current_level != 3:
        return "incomplete"
    rows = _relied_on_rounds(session, cycle)
    if len(rows) != 3:
        return "incomplete"
    for row in rows:
        if row.anchor_at is None or row.deadline_at is None:
            return "incomplete"
        if now < row.deadline_at:
            return "incomplete"
        evidence = evaluate_delivery_evidence(session, row.outbox_id, now=now)
        if evidence.reason == "negative":
            return HOLD_EVIDENCE_NEGATIVE
        if not evidence.usable:
            return "incomplete"
        if evidence.anchor_at != row.anchor_at:
            return HOLD_EVIDENCE_NEGATIVE
    return None


def _advance_or_gate_release(
    session: Session,
    *,
    user: User,
    vault: VigilVault,
    cycle: VigilCycle,
    now: datetime,
) -> bool:
    current = _open_round_row(session, cycle.id, cycle.current_level)
    if current is None or current.deadline_at is None or now < current.deadline_at:
        return False
    evidence = evaluate_delivery_evidence(session, current.outbox_id, now=now)
    if evidence.reason == "negative":
        _apply_hold(vault, HOLD_EVIDENCE_NEGATIVE, now)
        return True
    if not evidence.usable:
        _apply_hold(vault, HOLD_DELIVERY_MISSING, now)
        return True
    if cycle.current_level < 3:
        if not _account_ok(session, user, vault, cycle.config_id):
            _apply_hold(vault, HOLD_ACCOUNT_INELIGIBLE, now)
            return True
        next_level = cycle.current_level + 1
        cycle.current_level = next_level
        _mint_round(
            session,
            vault=vault,
            cycle=cycle,
            user=user,
            level=next_level,
            generation=1,
            now=now,
        )
        from_phase = vault.phase
        vault.phase = _LEVEL_PHASE[next_level]
        vault.updated_at = now
        vault.revision += 1
        emit_vigil_event(
            "vigil.level_advanced",
            actor="system",
            vault_id=vault.id,
            from_phase=from_phase,
            to_phase=vault.phase,
            cycle_id=cycle.id,
            level=next_level,
        )
        return True
    blocked = _windows_complete(session, cycle, now=now)
    if blocked == HOLD_EVIDENCE_NEGATIVE:
        _apply_hold(vault, HOLD_EVIDENCE_NEGATIVE, now)
        return True
    if blocked is not None:
        return False
    from app.services.vigil.recovery import final_release_allowed

    if not final_release_allowed():
        _apply_hold(vault, HOLD_RELEASE_UNAVAILABLE, now)
        return True
    return False


def _step_vault(session: Session, *, user: User, vault: VigilVault, now: datetime) -> None:
    if vault.phase == "ARMED":
        if vault.next_check_at is not None and now >= vault.next_check_at:
            _open_level_one(session, user=user, vault=vault, now=now)
        return
    cycle = _active_cycle(session, vault.id)
    if cycle is None:
        return
    _lock_cycle_graph(session, vault, cycle)
    if _set_deadline_or_hold(session, vault=vault, cycle=cycle, now=now):
        return
    _advance_or_gate_release(session, user=user, vault=vault, cycle=cycle, now=now)


def run_cycle_scan(session: Session, *, now: datetime | None = None) -> None:
    """Bounded scan: inspect last completed heartbeat, maybe hold, at most
    one state step per vault. No HTTP. Caller commits; heartbeat is part
    of that same transaction.
    """
    settings = get_settings()
    runtime = _runtime_row(session)
    previous = runtime.last_scan_completed_at
    vaults = list(
        session.scalars(
            select(VigilVault).where(VigilVault.phase.in_(_SCAN_PHASES)).order_by(VigilVault.id)
        ).all()
    )
    scan_now = now
    gap = False
    if previous is not None and scan_now is not None:
        gap = scan_now - previous >= SCAN_GAP
    for vault_id in [v.id for v in vaults]:
        owner_id = session.execute(
            select(VigilVault.owner_user_id).where(VigilVault.id == vault_id)
        ).scalar_one()
        user = session.execute(
            select(User).where(User.id == owner_id).with_for_update()
        ).scalar_one_or_none()
        locked = session.execute(
            select(VigilVault).where(VigilVault.id == vault_id).with_for_update()
        ).scalar_one()
        current = scan_now or db_now(session)
        if previous is not None and scan_now is None:
            gap = current - previous >= SCAN_GAP
        if settings.VIGIL_MODE != "active" or user is None:
            continue
        if gap:
            _apply_hold(locked, HOLD_SCAN_GAP, current)
            continue
        if locked.hold_reason is not None:
            continue
        _step_vault(session, user=user, vault=locked, now=current)
    finish = scan_now or db_now(session)
    runtime.last_scan_completed_at = finish
    if gap:
        runtime.health = "held"
        runtime.reason = HOLD_SCAN_GAP
    session.flush()


def earliest_release_eligible_at(session: Session, cycle_id: UUID) -> datetime | None:
    rows = list(
        session.scalars(
            select(VigilRound)
            .where(VigilRound.cycle_id == cycle_id, VigilRound.superseded_at.is_(None))
            .order_by(VigilRound.level)
        ).all()
    )
    if len(rows) != 3:
        return None
    deadlines = [r.deadline_at for r in rows]
    if any(d is None for d in deadlines):
        return None
    present = [d for d in deadlines if d is not None]
    return max(present)


def _resolve_to_armed(
    session: Session,
    *,
    user: User,
    vault: VigilVault,
    now: datetime,
    actor_type: str,
    confirming_token: VigilActionToken | None = None,
    cycle_status: str = "confirmed",
) -> None:
    cycle = _active_cycle(session, vault.id)
    if cycle is not None:
        _lock_cycle_graph(session, vault, cycle)
        cycle.status = cycle_status
        cycle.resolved_at = now
        for token in session.scalars(
            select(VigilActionToken).where(
                VigilActionToken.cycle_id == cycle.id,
                VigilActionToken.purpose == _CYCLE_CONFIRM,
            )
        ).all():
            if confirming_token is not None and token.id == confirming_token.id:
                token.confirmed_at = now
                token.used_at = now
            elif token.invalidated_at is None and token.confirmed_at is None:
                token.invalidated_at = now
        cancel_outbox_intents(session, vault_id=vault.id, purpose=_CHALLENGE_PURPOSE)
    if vault.active_config_id is None:
        raise VigilCycleConflict("no active configuration")
    interval = _interval_days(session, vault, vault.active_config_id)
    from_phase = vault.phase
    vault.phase = "ARMED"
    vault.next_check_at = now + timedelta(days=interval)
    vault.last_owner_confirmed_at = now
    vault.retention_anchor_at = now
    vault.updated_at = now
    vault.revision += 1
    emit_vigil_event(
        "vigil.check_in",
        actor=actor_type,
        vault_id=vault.id,
        from_phase=from_phase,
        to_phase="ARMED",
        cycle_id=cycle.id if cycle is not None else None,
    )
    session.flush()


def check_in(
    session: Session,
    *,
    owner_user_id: UUID,
    expected_revision: int,
    now: datetime | None = None,
) -> CycleActionResult:
    settings = get_settings()
    if settings.VIGIL_MODE not in {"active", "recovery"}:
        raise VigilCycleUnavailable("vigil is not available")
    user, vault = _lock_user_and_vault(session, owner_user_id)
    current = now or db_now(session)
    if expected_revision != vault.revision:
        raise VigilRevisionConflict(current_revision=vault.revision)
    if vault.phase == "DISARMED":
        raise VigilCycleConflict("disarmed cannot check-in")
    if vault.phase == "RELEASED":
        raise VigilCycleConflict({"error": "released", "action": "revoke"})
    _resolve_to_armed(session, user=user, vault=vault, now=current, actor_type="owner")
    assert vault.next_check_at is not None
    return CycleActionResult(
        phase=vault.phase,
        revision=vault.revision,
        next_check_at=rfc3339_z(vault.next_check_at),
    )


def disarm(
    session: Session,
    *,
    owner_user_id: UUID,
    expected_revision: int,
    now: datetime | None = None,
) -> CycleActionResult:
    settings = get_settings()
    if settings.VIGIL_MODE not in {"active", "recovery"}:
        raise VigilCycleUnavailable("vigil is not available")
    _user, vault = _lock_user_and_vault(session, owner_user_id)
    current = now or db_now(session)
    if expected_revision != vault.revision:
        raise VigilRevisionConflict(current_revision=vault.revision)
    if vault.phase == "RELEASED":
        raise VigilCycleConflict({"error": "released", "action": "revoke"})
    from_phase = vault.phase
    cancel_active_cycles(session, vault, now=current, status="cancelled")
    vault.phase = "DISARMED"
    vault.next_check_at = None
    vault.updated_at = current
    vault.revision += 1
    emit_vigil_event(
        "vigil.disarm",
        actor="owner",
        vault_id=vault.id,
        from_phase=from_phase,
        to_phase="DISARMED",
    )
    session.flush()
    return CycleActionResult(phase=vault.phase, revision=vault.revision, next_check_at=None)


def resume_held_vault(
    session: Session,
    *,
    owner_user_id: UUID,
    expected_revision: int,
    now: datetime | None = None,
) -> CycleActionResult:
    user, vault = _lock_user_and_vault(session, owner_user_id)
    current = now or db_now(session)
    if expected_revision != vault.revision:
        raise VigilRevisionConflict(current_revision=vault.revision)
    if vault.hold_reason is None:
        raise VigilCycleConflict("vault is not held")
    cycle = _active_cycle(session, vault.id)
    vault.hold_reason = None
    vault.held_at = None
    if cycle is not None and vault.phase in {"CHALLENGE_1", "CHALLENGE_2", "FINAL_WARNING"}:
        _lock_cycle_graph(session, vault, cycle)
        current_round = _open_round_row(session, cycle.id, cycle.current_level)
        next_generation = 1
        if current_round is not None:
            current_round.superseded_at = current
            next_generation = current_round.generation + 1
        _mint_round(
            session,
            vault=vault,
            cycle=cycle,
            user=user,
            level=cycle.current_level,
            generation=next_generation,
            now=current,
        )
    elif vault.phase == "ARMED" and vault.active_config_id is not None:
        interval = _interval_days(session, vault, vault.active_config_id)
        vault.next_check_at = current + timedelta(days=interval)
    vault.updated_at = current
    vault.revision += 1
    runtime = _runtime_row(session)
    runtime.health = "ok"
    runtime.reason = None
    emit_vigil_event(
        "vigil.resume",
        actor="ops",
        vault_id=vault.id,
        from_phase=vault.phase,
        to_phase=vault.phase,
        level=cycle.current_level if cycle is not None else None,
    )
    session.flush()
    next_check = rfc3339_z(vault.next_check_at) if vault.next_check_at is not None else None
    return CycleActionResult(phase=vault.phase, revision=vault.revision, next_check_at=next_check)


def current_deadline_at(session: Session, vault: VigilVault) -> datetime | None:
    cycle = session.scalars(
        select(VigilCycle).where(VigilCycle.vault_id == vault.id, VigilCycle.status == "active")
    ).one_or_none()
    if cycle is None:
        return None
    row = _open_round_row(session, cycle.id, cycle.current_level)
    if row is None:
        return None
    return row.deadline_at


def confirm_cycle_token(
    session: Session,
    *,
    token: str,
    nonce: str,
    now: datetime | None = None,
) -> PublicCycleConfirmResult:
    from app.services.vigil.drills import VigilPublicTokenError, _lock_token_context

    token_hash = hash_link_token(token)
    user, vault, row = _lock_token_context(session, token_hash)
    current = now or db_now(session)
    if row.purpose != _CYCLE_CONFIRM or row.cycle_id is None:
        raise VigilPublicTokenError(410, "gone")
    cycle = session.execute(
        select(VigilCycle).where(VigilCycle.id == row.cycle_id).with_for_update()
    ).scalar_one_or_none()
    if cycle is None:
        raise VigilPublicTokenError(404, "not found")
    try:
        signed = verify_signed_nonce(
            nonce,
            token_hash=token_hash,
            action="confirm",
            object_id=row.object_id,
            now=current,
        )
    except VigilNonceError as exc:
        raise VigilPublicTokenError(422, str(exc)) from exc

    if cycle.status == "confirmed" or row.confirmed_at is not None:
        next_check = rfc3339_z(vault.next_check_at) if vault.next_check_at is not None else None
        return PublicCycleConfirmResult(result="already_resolved", next_check_at=next_check)
    if cycle.status != "active" or row.invalidated_at is not None:
        raise VigilPublicTokenError(410, "gone")
    if vault.phase == "RELEASED":
        raise VigilPublicTokenError(409, "gone")
    if vault.phase == "DISARMED":
        raise VigilPublicTokenError(410, "gone")
    try:
        consume_nonce(session, signed, now=current)
    except VigilNonceError as exc:
        raise VigilPublicTokenError(422, str(exc)) from exc
    _resolve_to_armed(
        session,
        user=user,
        vault=vault,
        now=current,
        actor_type="token",
        confirming_token=row,
    )
    assert vault.next_check_at is not None
    return PublicCycleConfirmResult(
        result="confirmed", next_check_at=rfc3339_z(vault.next_check_at)
    )


def cycle_token_public_state(
    session: Session, token_row: VigilActionToken, *, now: datetime, vault: VigilVault
) -> str:
    if token_row.cycle_id is None:
        return "wrong_purpose"
    cycle = session.get(VigilCycle, token_row.cycle_id)
    if cycle is None:
        return "stale"
    if token_row.confirmed_at is not None or cycle.status == "confirmed":
        return "confirmed"
    if token_row.invalidated_at is not None:
        return "invalidated"
    if cycle.status != "active":
        return "stale"
    if cycle.vault_id != vault.id:
        return "stale"
    del now
    return "available"
