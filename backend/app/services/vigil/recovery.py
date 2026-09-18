"""Stop/recovery hooks for Vigil activation (issue #458, P2.3).

Live HTTP arming stays disabled until later checkpoints install complete
stop/recovery (cycles, grants, restore). Fixture tests may monkeypatch
`live_activation_allowed` to exercise the atomic swap that this
checkpoint does own: cancel intents, invalidate unused drills, retire the
previous active file and NULL its C/outer columns in the current logical
row.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.vigil import VigilConfiguration, VigilObject, VigilVault
from app.services.vigil.dispatch import cancel_outbox_intents
from app.services.vigil.tokens import invalidate_open_drill_tokens


def live_activation_allowed() -> bool:
    """False until #460/#464 complete grant-revocation and restore.

    #459 installs cycles so stop can cancel an active confirmation cycle,
    but live HTTP arming still refuses while grant revocation is missing.
    Tests that need the supported atomic-swap cases patch this to True.
    """
    return False


def final_release_allowed() -> bool:
    """False until P4.1 (#460) owns transactional grants/release.

    The cycle scanner must not materialize a RELEASED vault or grants.
    """
    return False


def stop_prior_arrangement(session: Session, vault: VigilVault, *, now: datetime) -> None:
    """Application-layer stop of the current arrangement.

    Cancels outbox intents and pending drills, retires the previous active
    config/object, and NULLs ciphertext/outer_cipher on that logical row.
    Cancels an active confirmation cycle when #459 tables exist. Grants
    still do not exist; callers must refuse live arming while
    `live_activation_allowed()` is False.
    """
    from app.services.vigil.cycles import cancel_active_cycles

    cancel_active_cycles(session, vault, now=now, status="cancelled")
    cancel_outbox_intents(session, vault_id=vault.id)
    invalidate_open_drill_tokens(session, vault_id=vault.id, now=now)

    if vault.active_object_id is not None:
        old_obj = session.execute(
            select(VigilObject).where(VigilObject.id == vault.active_object_id).with_for_update()
        ).scalar_one_or_none()
        if old_obj is not None and old_obj.status == "active":
            old_obj.status = "retired"
            old_obj.ciphertext = None
            old_obj.outer_cipher = None

    if vault.active_config_id is not None:
        old_cfg = session.execute(
            select(VigilConfiguration)
            .where(VigilConfiguration.id == vault.active_config_id)
            .with_for_update()
        ).scalar_one_or_none()
        if old_cfg is not None and old_cfg.status == "active":
            old_cfg.status = "retired"

    session.flush()
