"""user_directory recipient resolution (issue #257)."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.models.user import User
from app.services.user_directory import recipient_email, recipient_email_with_purpose

_UID = uuid.UUID("00000000-0000-0000-0000-0000000000d7")


def _user(
    *,
    email: str = "acct@example.com",
    delivery_email: str | None = None,
    status: str = "active",
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
    )


def test_prefers_delivery_email_and_labels_purpose(db_session: Session) -> None:
    db_session.add(_user(delivery_email="reports@example.com"))
    db_session.commit()

    assert recipient_email_with_purpose(db_session, _UID) == (
        "reports@example.com",
        "delivery_email",
    )
    assert recipient_email(db_session, _UID) == "reports@example.com"


def test_falls_back_to_account_email(db_session: Session) -> None:
    db_session.add(_user(delivery_email=None))
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
