"""user_directory recipient resolution (issue #257, #276)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.user import User
from app.services.user_directory import recipient_email, recipient_email_with_purpose

_UID = uuid.UUID("00000000-0000-0000-0000-0000000000d7")

_VERIFIED = datetime(2026, 8, 31, 12, 0)


def _user(
    *,
    email: str = "acct@example.com",
    delivery_email: str | None = None,
    status: str = "active",
    email_verified_at: datetime | None = None,
    delivery_email_verified_at: datetime | None = None,
) -> User:
    return User(
        id=_UID,
        auth_provider="supabase",
        auth_subject=f"sub-{_UID}",
        email=email,
        delivery_email=delivery_email,
        status=status,
        locale="zh",
        base_currency="USD",
        report_cadence="mwf",
        email_verified_at=email_verified_at,
        delivery_email_verified_at=delivery_email_verified_at,
    )


def test_prefers_delivery_email_and_labels_purpose(db_session: Session) -> None:
    # Issue #276: a delivery address only counts once IT is verified —
    # unverified-by-default fixtures must pass their own timestamp.
    db_session.add(
        _user(delivery_email="reports@example.com", delivery_email_verified_at=_VERIFIED)
    )
    db_session.commit()

    assert recipient_email_with_purpose(db_session, _UID) == (
        "reports@example.com",
        "delivery_email",
    )
    assert recipient_email(db_session, _UID) == "reports@example.com"


def test_falls_back_to_account_email(db_session: Session) -> None:
    # Issue #276: the account address counts only when it is verified.
    db_session.add(_user(delivery_email=None, email_verified_at=_VERIFIED))
    db_session.commit()

    assert recipient_email_with_purpose(db_session, _UID) == (
        "acct@example.com",
        "account_email",
    )
    assert recipient_email(db_session, _UID) == "acct@example.com"


def test_missing_or_inactive_user_returns_none(db_session: Session) -> None:
    assert recipient_email_with_purpose(db_session, _UID) is None
    db_session.add(_user(status="suspended"))
    db_session.commit()
    assert recipient_email_with_purpose(db_session, _UID) is None
    assert recipient_email(db_session, _UID) is None


def test_no_cross_fallback_between_unverified_delivery_and_verified_account(
    db_session: Session,
) -> None:
    """Issue #276 Layer 2: an address only counts when ITS OWN verified
    timestamp is set. A filled-but-unverified `delivery_email` must not ride
    on the account email's verification (nor the reverse) — the resolver
    falls through to the verified account email instead of the fresher,
    human-entered, but unconfirmed delivery address."""
    db_session.add(
        _user(
            delivery_email="reports@example.com",
            email_verified_at=_VERIFIED,
        )
    )
    db_session.commit()

    assert recipient_email_with_purpose(db_session, _UID) == (
        "acct@example.com",
        "account_email",
    )
    assert recipient_email(db_session, _UID) == "acct@example.com"


def test_unverified_user_returns_none(db_session: Session) -> None:
    """Issue #276 Layer 2: active user, both addresses present, neither
    verified → nothing is sendable (fail closed)."""
    db_session.add(_user(delivery_email="reports@example.com"))
    db_session.commit()

    assert recipient_email_with_purpose(db_session, _UID) is None
    assert recipient_email(db_session, _UID) is None
