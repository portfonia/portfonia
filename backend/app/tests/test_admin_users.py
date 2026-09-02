"""GET /admin/users (issue #278).

Read-only ops user directory. The driver is not "listing users for
troubleshooting": after issue #274/PR #275, the delete-by-email pre-delete
confirmation policy needs an account's facts (created_at, whether it has
questionnaire/investment-context data, holdings count) before a human
re-confirms the target email — and until this endpoint existed, satisfying
that policy required SSH+psql, the exact step #274 was built to remove.
This endpoint exposes those facts as one read-only query; the confirmation
flow itself stays an operator-side behavior, out of this endpoint's scope.

Same test shape as test_admin_cadence.py: auth via the router-level
ADMIN_API_TOKEN dependency, real DB rows (no mocks) via db_session.
"""

from __future__ import annotations

import uuid
from typing import get_args

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.models.user import VALID_REPORT_CADENCES, VALID_USER_STATUSES, User
from app.models.user_investment_context import UserInvestmentContext
from app.routers.admin import ReportCadenceFilter, UserStatusFilter
from app.services.questionnaire_taxonomy import QUESTIONNAIRE_VERSION
from app.tests.test_admin_router import _headers

_U1 = uuid.UUID("00000000-0000-0000-0000-0000000000e1")
_U2 = uuid.UUID("00000000-0000-0000-0000-0000000000e2")
_U3 = uuid.UUID("00000000-0000-0000-0000-0000000000e3")
_U4 = uuid.UUID("00000000-0000-0000-0000-0000000000e4")


def _user(
    user_id: uuid.UUID,
    email: str,
    *,
    status: str = "active",
    cadence: str = "mwf",
    auth_subject: str | None = "bound",
) -> User:
    # "bound" (default) means "bind this row to a per-user sub"; pass None
    # explicitly to build an unbound row (auth_subject_bound=False). Real
    # subs are UUIDs, so the sentinel never collides.
    resolved: str | None = f"sub-{user_id}" if auth_subject == "bound" else auth_subject
    return User(
        id=user_id,
        auth_provider="supabase",
        auth_subject=resolved,
        email=email,
        status=status,
        locale="zh",
        base_currency="USD",
        report_cadence=cadence,
    )


def _h(user_id: uuid.UUID) -> Holding:
    return Holding(
        user_id=user_id,
        name="NVIDIA",
        ticker="NVDA",
        pricing_mode="auto",
        currency="USD",
        asset_class="EQUITY_US_TECH",
    )


def _context(user_id: uuid.UUID) -> UserInvestmentContext:
    return UserInvestmentContext(
        user_id=user_id,
        questionnaire={
            "asset_scale": "100K_500K",
            "markets": ["US"],
            "style": "GROWTH",
            "horizon": "LONG",
            "risk_appetite": "BALANCED",
            "sectors_of_interest": [],
            "objective": "GROWTH",
            "intel_focus": "MACRO",
        },
        questionnaire_version=QUESTIONNAIRE_VERSION,
    )


def test_filter_literals_match_model_valid_values() -> None:
    """Same hand-kept-sync discipline as UpdateCadenceBody's Literal (PR
    #248): a Pydantic Literal can't be derived from a tuple at type-check
    time, so drift between the two copies fails this test instead of
    silently accepting/rejecting the wrong values at runtime."""
    assert set(get_args(UserStatusFilter)) == set(VALID_USER_STATUSES)
    assert set(get_args(ReportCadenceFilter)) == set(VALID_REPORT_CADENCES)


def test_list_users_requires_ops_token(app_client: TestClient) -> None:
    resp = app_client.get("/admin/users")
    assert resp.status_code == 401


def test_list_users_rejects_wrong_token(app_client: TestClient) -> None:
    resp = app_client.get("/admin/users", headers=_headers("wrong-token"))
    assert resp.status_code == 401


def test_list_users_empty(app_client: TestClient) -> None:
    resp = app_client.get("/admin/users", headers=_headers())
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_users_returns_all_users_with_full_shape(
    app_client: TestClient, db_session: Session
) -> None:
    db_session.add_all(
        [
            _user(_U1, "u1@example.com", auth_subject="sub-u1"),
            _user(_U2, "u2@example.com", auth_subject=None),
        ]
    )
    db_session.flush()

    resp = app_client.get("/admin/users", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert {u["email"] for u in body} == {"u1@example.com", "u2@example.com"}

    u1 = next(u for u in body if u["email"] == "u1@example.com")
    assert uuid.UUID(u1["id"]) == _U1
    assert u1["status"] == "active"
    assert u1["report_cadence"] == "mwf"
    assert u1["auth_subject_bound"] is True
    assert u1["has_investment_context"] is False
    assert u1["holdings_count"] == 0
    # created_at is a serialized timestamp, not null
    assert u1.get("created_at")

    u2 = next(u for u in body if u["email"] == "u2@example.com")
    assert uuid.UUID(u2["id"]) == _U2
    assert u2["auth_subject_bound"] is False
    assert u2["has_investment_context"] is False
    assert u2["holdings_count"] == 0


def test_email_filter_normalizes_and_exact_matches(
    app_client: TestClient, db_session: Session
) -> None:
    db_session.add_all(
        [
            _user(_U1, "u1@example.com"),
            _user(_U2, "other@example.com"),
        ]
    )
    db_session.flush()

    resp = app_client.get("/admin/users", headers=_headers(), params={"email": "  U1@Example.COM "})
    assert resp.status_code == 200
    assert [u["email"] for u in resp.json()] == ["u1@example.com"]


def test_email_filter_no_match_is_empty_array(app_client: TestClient, db_session: Session) -> None:
    db_session.add(_user(_U1, "u1@example.com"))
    db_session.flush()

    resp = app_client.get(
        "/admin/users", headers=_headers(), params={"email": "nobody@example.com"}
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_email_filter_is_exact_not_substring(app_client: TestClient, db_session: Session) -> None:
    db_session.add(_user(_U1, "u1@example.com"))
    db_session.flush()

    resp = app_client.get("/admin/users", headers=_headers(), params={"email": "u1@example"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_blank_email_param_is_no_filter(app_client: TestClient, db_session: Session) -> None:
    """_normalize_email returns None for whitespace-only input; the endpoint
    treats that the same as the param being absent (the filter is optional)."""
    db_session.add_all([_user(_U1, "u1@example.com"), _user(_U2, "u2@example.com")])
    db_session.flush()

    resp = app_client.get("/admin/users", headers=_headers(), params={"email": "   "})
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_status_filter(app_client: TestClient, db_session: Session) -> None:
    db_session.add_all(
        [
            _user(_U1, "active@example.com", status="active"),
            _user(_U2, "suspended@example.com", status="suspended"),
            _user(_U3, "deleted@example.com", status="deleted"),
        ]
    )
    db_session.flush()

    resp = app_client.get("/admin/users", headers=_headers(), params={"status": "suspended"})
    assert resp.status_code == 200
    assert [u["email"] for u in resp.json()] == ["suspended@example.com"]


def test_cadence_filter(app_client: TestClient, db_session: Session) -> None:
    db_session.add_all(
        [
            _user(_U1, "mwf@example.com", cadence="mwf"),
            _user(_U2, "weekly@example.com", cadence="weekly"),
        ]
    )
    db_session.flush()

    resp = app_client.get("/admin/users", headers=_headers(), params={"report_cadence": "weekly"})
    assert resp.status_code == 200
    assert [u["email"] for u in resp.json()] == ["weekly@example.com"]


def test_combined_filters(app_client: TestClient, db_session: Session) -> None:
    db_session.add_all(
        [
            _user(_U1, "a@example.com", status="active", cadence="mwf"),
            _user(_U2, "b@example.com", status="suspended", cadence="mwf"),
            _user(_U3, "c@example.com", status="active", cadence="weekly"),
        ]
    )
    db_session.flush()

    resp = app_client.get(
        "/admin/users",
        headers=_headers(),
        params={"status": "active", "report_cadence": "mwf"},
    )
    assert resp.status_code == 200
    assert [u["email"] for u in resp.json()] == ["a@example.com"]


def test_holdings_count_and_context_use_real_related_rows(
    app_client: TestClient, db_session: Session
) -> None:
    """No mocks: holdings_count / has_investment_context must reflect actual
    rows in the holdings / user_investment_context tables (the exact facts
    the pre-delete confirmation policy reports back to a human)."""
    db_session.add_all(
        [
            _user(_U1, "u1@example.com"),  # 2 holdings + context
            _user(_U2, "u2@example.com"),  # 0 holdings + context
            _user(_U3, "u3@example.com"),  # 3 holdings, no context
            _user(_U4, "u4@example.com"),  # 0 holdings, no context
        ]
    )
    db_session.add_all([_h(_U1), _h(_U1), _h(_U3), _h(_U3), _h(_U3)])
    db_session.add_all([_context(_U1), _context(_U2)])
    db_session.flush()

    resp = app_client.get("/admin/users", headers=_headers())
    assert resp.status_code == 200
    by_email = {u["email"]: u for u in resp.json()}

    assert by_email["u1@example.com"]["holdings_count"] == 2
    assert by_email["u1@example.com"]["has_investment_context"] is True
    assert by_email["u2@example.com"]["holdings_count"] == 0
    assert by_email["u2@example.com"]["has_investment_context"] is True
    assert by_email["u3@example.com"]["holdings_count"] == 3
    assert by_email["u3@example.com"]["has_investment_context"] is False
    assert by_email["u4@example.com"]["holdings_count"] == 0
    assert by_email["u4@example.com"]["has_investment_context"] is False


def _seed_n_users(db_session: Session, n: int) -> None:
    db_session.add_all(
        [
            _user(
                uuid.UUID(f"00000000-0000-0000-0000-0000000000f{i}"),
                f"user{i}@example.com",
            )
            for i in range(1, n + 1)
        ]
    )
    db_session.flush()


def test_limit_offset_paginates_without_overlap_or_gap(
    app_client: TestClient, db_session: Session
) -> None:
    _seed_n_users(db_session, 5)

    page1 = app_client.get("/admin/users", headers=_headers(), params={"limit": 2}).json()
    page2 = app_client.get(
        "/admin/users", headers=_headers(), params={"limit": 2, "offset": 2}
    ).json()
    page3 = app_client.get(
        "/admin/users", headers=_headers(), params={"limit": 2, "offset": 4}
    ).json()
    page4 = app_client.get(
        "/admin/users", headers=_headers(), params={"limit": 2, "offset": 5}
    ).json()

    assert len(page1) == 2
    assert len(page2) == 2
    assert len(page3) == 1
    assert page4 == []

    e1 = {u["email"] for u in page1}
    e2 = {u["email"] for u in page2}
    e3 = {u["email"] for u in page3}
    assert e1.isdisjoint(e2) and e2.isdisjoint(e3) and e1.isdisjoint(e3)
    assert e1 | e2 | e3 == {f"user{i}@example.com" for i in range(1, 6)}


def test_default_limit_returns_all_and_bounds_enforced(
    app_client: TestClient, db_session: Session
) -> None:
    _seed_n_users(db_session, 3)

    resp = app_client.get("/admin/users", headers=_headers())
    assert resp.status_code == 200
    assert len(resp.json()) == 3

    assert (
        app_client.get("/admin/users", headers=_headers(), params={"limit": 201}).status_code == 422
    )
    assert (
        app_client.get("/admin/users", headers=_headers(), params={"limit": 0}).status_code == 422
    )
    assert (
        app_client.get("/admin/users", headers=_headers(), params={"offset": -1}).status_code == 422
    )


def test_invalid_status_and_cadence_values_422(app_client: TestClient, db_session: Session) -> None:
    db_session.add(_user(_U1, "u1@example.com"))
    db_session.flush()

    # Same validation shape as POST /admin/users/{user_id}/cadence's Literal:
    # a value outside the legal set is a request-shape 422, never a 500.
    assert (
        app_client.get("/admin/users", headers=_headers(), params={"status": "banned"}).status_code
        == 422
    )
    assert (
        app_client.get(
            "/admin/users", headers=_headers(), params={"report_cadence": "daily"}
        ).status_code
        == 422
    )
