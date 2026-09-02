"""Coverage for the users.locale CHECK constraint (issue #308).

Mirrors test_user_report_cadence_constraint.py's shape (issue #191,
migration e1f2a3b4c5d6): `locale` was NOT NULL with no CHECK constraint
before this issue, so a `report_cadence`-style whitelist is added here
too, backing the report-language feature.
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


@pytest.mark.parametrize("locale", ["en", "zh"])
def test_db_allows_known_locale(db_session: Session, locale: str) -> None:
    user = _user(locale=locale)
    db_session.add(user)
    db_session.commit()
    assert user.locale == locale


def test_db_rejects_unknown_locale(db_session: Session) -> None:
    db_session.add(_user(locale="fr"))
    with pytest.raises(IntegrityError, match="ck_users_locale"):
        db_session.commit()
