"""Arming eligibility. Release/arm routes land later; this gate is the contract."""

from __future__ import annotations

from vigil_app.services.account_facts import AccountFacts


class ArmDenied(Exception):
    """Inactive, unverified, or mismatched account must not arm."""


def assert_can_arm(facts: AccountFacts, *, configured_email: str | None = None) -> None:
    if not facts.eligible:
        raise ArmDenied
    if configured_email is None or facts.account_email is None:
        return
    if configured_email.casefold() != facts.account_email.casefold():
        raise ArmDenied
