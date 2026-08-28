"""Coverage for the users.report_cadence CHECK constraint (issue #191,
migration e1f2a3b4c5d6).
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


@pytest.mark.parametrize("cadence", ["mwf", "weekly"])
def test_db_allows_known_report_cadence(db_session: Session, cadence: str) -> None:
    user = _user(report_cadence=cadence)
    db_session.add(user)
    db_session.commit()
    assert user.report_cadence == cadence


def test_db_rejects_unknown_report_cadence(db_session: Session) -> None:
    db_session.add(_user(report_cadence="bogus"))
    with pytest.raises(IntegrityError, match="ck_users_report_cadence"):
        db_session.commit()
