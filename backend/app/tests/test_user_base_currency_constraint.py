"""Coverage for the users.base_currency CHECK constraint (issue #350 item 1).

Mirrors test_user_locale_constraint.py's shape (issue #308, migration
a2b3c4d5e6f7): `base_currency` was NOT NULL with no CHECK constraint
before this issue.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.user import User


def _user(**overrides: object) -> User:
    defaults: dict[str, object] = dict(
        id=uuid.uuid4(),
        auth_provider="supabase",
        auth_subject=f"sub-{uuid.uuid4()}",
        email=f"{uuid.uuid4()}@example.com",
        status="active",
        locale="zh",
        base_currency="USD",
        report_cadence="mwf",
    )
    defaults.update(overrides)
    return User(**defaults)


@pytest.mark.parametrize("base_currency", ["USD", "CNY", "HKD", "TWD"])
def test_db_allows_known_currency(db_session: Session, base_currency: str) -> None:
    user = _user(base_currency=base_currency)
    db_session.add(user)
    db_session.commit()
    assert user.base_currency == base_currency


def test_db_rejects_unknown_currency(db_session: Session) -> None:
    db_session.add(_user(base_currency="XXX"))
    with pytest.raises(IntegrityError, match="ck_users_base_currency"):
        db_session.commit()
