"""POST /admin/users/{user_id}/cadence (issue #191).

Ops endpoint to change a user's report_cadence. Same shape as
POST /admin/users/{id}/bind-subject (test_admin_invites.py): auth via the
router-level ADMIN_API_TOKEN dependency, 404 for an unknown user, validated
against the same two-value set as the DB CheckConstraint.
"""

from __future__ import annotations

import uuid
from typing import get_args

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.user import VALID_REPORT_CADENCES, User
from app.routers.admin import UpdateCadenceBody
from app.tests.test_admin_router import _headers
from app.tests.test_user_scope import _user

_UID = uuid.UUID("00000000-0000-0000-0000-0000000000c9")


def test_update_cadence_literal_matches_valid_report_cadences() -> None:
    """UpdateCadenceBody.report_cadence's Literal is a hand-kept second copy
    of VALID_REPORT_CADENCES (documented in both places, PR #248 review) —
    a Pydantic Literal can't be derived from a tuple at type-check time. This
    doesn't remove the duplication, it makes drift between the two fail a
    test instead of silently accepting/rejecting the wrong values at
    runtime."""
    literal_values = get_args(UpdateCadenceBody.model_fields["report_cadence"].annotation)
    assert set(literal_values) == set(VALID_REPORT_CADENCES)


def test_update_cadence_requires_ops_token(app_client: TestClient) -> None:
    resp = app_client.post(f"/admin/users/{_UID}/cadence", json={"report_cadence": "weekly"})
    assert resp.status_code == 401


def test_update_cadence_mwf_to_weekly(app_client: TestClient, db_session: Session) -> None:
    db_session.add(_user(_UID, "seed@example.com", cadence="mwf"))
    db_session.flush()

    resp = app_client.post(
        f"/admin/users/{_UID}/cadence",
        headers=_headers(),
        json={"report_cadence": "weekly"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"id": str(_UID), "email": "seed@example.com", "report_cadence": "weekly"}
    db_session.expire_all()
    row = db_session.get(User, _UID)
    assert row is not None
    assert row.report_cadence == "weekly"


def test_update_cadence_weekly_to_mwf(app_client: TestClient, db_session: Session) -> None:
    db_session.add(_user(_UID, "seed@example.com", cadence="weekly"))
    db_session.flush()

    resp = app_client.post(
        f"/admin/users/{_UID}/cadence",
        headers=_headers(),
        json={"report_cadence": "mwf"},
    )

    assert resp.status_code == 200
    db_session.expire_all()
    row = db_session.get(User, _UID)
    assert row is not None
    assert row.report_cadence == "mwf"


def test_update_cadence_rejects_unknown_value(app_client: TestClient, db_session: Session) -> None:
    db_session.add(_user(_UID, "seed@example.com", cadence="mwf"))
    db_session.flush()

    resp = app_client.post(
        f"/admin/users/{_UID}/cadence",
        headers=_headers(),
        json={"report_cadence": "daily"},
    )

    assert resp.status_code == 422
    db_session.expire_all()
    row = db_session.get(User, _UID)
    assert row is not None
    assert row.report_cadence == "mwf"


def test_update_cadence_404_unknown_user(app_client: TestClient) -> None:
    resp = app_client.post(
        f"/admin/users/{uuid.uuid4()}/cadence",
        headers=_headers(),
        json={"report_cadence": "weekly"},
    )
    assert resp.status_code == 404
