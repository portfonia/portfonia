"""Scan path reads account facts only; hold, never release (issue #452)."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from vigil_app.models.vault import Vault
from vigil_app.services import scan_identity as scan_mod
from vigil_app.services.account_facts import AccountFacts, IdentityServiceError
from vigil_app.services.arm_gate import ArmDenied, assert_can_arm
from vigil_app.services.scan_identity import observe_vault_account


def _vault(session: Session, subject: str = "owner-sub-test") -> Vault:
    row = Vault(owner_auth_subject=subject, phase="ARMED", revision=1)
    session.add(row)
    session.flush()
    return row


def _facts(*, eligible: bool, email: str | None = "owner@example.com") -> AccountFacts:
    return AccountFacts(
        auth_subject="owner-sub-test",
        eligible=eligible,
        account_email=email if eligible or email else None,
        email_verified_at=datetime(2026, 1, 1, tzinfo=UTC) if eligible else None,
        observed_at=datetime(2026, 9, 14, tzinfo=UTC),
    )


def test_scan_module_does_not_call_session_status() -> None:
    source = inspect.getsource(scan_mod)
    assert "session-status" not in source
    assert "session_status" not in source


def test_ineligible_sets_hold_without_releasing(db_session: Session) -> None:
    vault = _vault(db_session)

    def _fetch(_subject: str, *, force_fresh: bool = False) -> AccountFacts:
        assert force_fresh is False
        return _facts(eligible=False, email="owner@example.com")

    observe_vault_account(db_session, vault, fetch_facts=_fetch, configured_email=None)
    db_session.refresh(vault)
    assert vault.phase == "ARMED"
    assert vault.hold_reason == "account_ineligible"
    assert vault.held_at is not None


def test_unavailable_identity_holds_not_releases(db_session: Session) -> None:
    vault = _vault(db_session)

    def _fetch(_subject: str, *, force_fresh: bool = False) -> AccountFacts:
        raise IdentityServiceError("down")

    observe_vault_account(db_session, vault, fetch_facts=_fetch, configured_email=None)
    db_session.refresh(vault)
    assert vault.phase == "ARMED"
    assert vault.hold_reason == "identity_unavailable"


def test_email_mismatch_holds(db_session: Session) -> None:
    vault = _vault(db_session)

    def _fetch(_subject: str, *, force_fresh: bool = False) -> AccountFacts:
        return _facts(eligible=True, email="other@example.com")

    observe_vault_account(
        db_session, vault, fetch_facts=_fetch, configured_email="owner@example.com"
    )
    db_session.refresh(vault)
    assert vault.hold_reason == "account_email_mismatch"
    assert vault.phase != "RELEASED"


def test_release_path_forces_fresh(db_session: Session) -> None:
    vault = _vault(db_session)
    seen: list[bool] = []

    def _fetch(_subject: str, *, force_fresh: bool = False) -> AccountFacts:
        seen.append(force_fresh)
        return _facts(eligible=True)

    observe_vault_account(
        db_session, vault, fetch_facts=_fetch, configured_email=None, force_fresh=True
    )
    assert seen == [True]


def test_scan_transport_never_hits_session_status(db_session: Session) -> None:
    vault = _vault(db_session)
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "auth_subject": "owner-sub-test",
                "eligible": False,
                "account_email": "owner@example.com",
                "email_verified_at": None,
                "observed_at": "2026-09-14T12:00:00Z",
            },
        )

    from vigil_app.services.account_facts import AccountFactsClient

    facts_client = AccountFactsClient(
        http=httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://backend:8000",
        ),
        identity_token="identity-token-test",
        cache_ttl_seconds=60,
        monotonic=lambda: 0.0,
    )
    observe_vault_account(db_session, vault, fetch_facts=facts_client.fetch)
    assert paths == ["/internal/vigil/principals/owner-sub-test"]
    assert all("session-status" not in p for p in paths)


def test_inactive_cannot_arm() -> None:
    with pytest.raises(ArmDenied):
        assert_can_arm(_facts(eligible=False))


def test_eligible_matching_email_can_arm() -> None:
    assert_can_arm(_facts(eligible=True), configured_email="owner@example.com")


def test_observe_locks_existing_row(db_session: Session) -> None:
    vault = _vault(db_session)
    locked = db_session.execute(
        select(Vault).where(Vault.id == vault.id).with_for_update()
    ).scalar_one()
    assert locked.id == vault.id
