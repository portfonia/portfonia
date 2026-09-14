"""Scheduled-scan identity observation. Account facts only — no browser session."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from vigil_app.models.vault import Vault
from vigil_app.services.account_facts import AccountFacts, IdentityServiceError, fetch_account_facts

FactsFetcher = Callable[..., AccountFacts]


def observe_vault_account(
    session: Session,
    vault: Vault,
    *,
    fetch_facts: FactsFetcher | None = None,
    configured_email: str | None = None,
    force_fresh: bool = False,
) -> None:
    locked = session.execute(
        select(Vault).where(Vault.id == vault.id).with_for_update()
    ).scalar_one()
    getter: FactsFetcher = fetch_facts or fetch_account_facts
    try:
        facts = getter(locked.owner_auth_subject, force_fresh=force_fresh)
    except IdentityServiceError:
        _hold(session, locked, "identity_unavailable")
        return
    if not facts.eligible:
        _hold(session, locked, "account_ineligible")
        return
    if (
        configured_email is not None
        and facts.account_email is not None
        and configured_email.casefold() != facts.account_email.casefold()
    ):
        _hold(session, locked, "account_email_mismatch")


def _hold(session: Session, vault: Vault, reason: str) -> None:
    vault.hold_reason = reason
    vault.held_at = datetime.now(UTC)
    session.flush()
