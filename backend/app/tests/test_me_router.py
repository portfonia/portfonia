"""Integration tests for GET /me — real Postgres (issue #220/#221).

Shape is the full #221 form ({email, delivery_email, tos_accepted_at,
has_questionnaire, has_holdings, missing}), built once per the Obsidian
Ring 1-Onboarding.md §6 coupling decision rather than a narrow #220-only
{email, delivery_email} landed first and widened later. #220's Profile page
does not render `missing` as a gap card yet — that's #221 — but the
endpoint's contract is final now.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import current_principal
from app.main import app
from app.models.holding import Holding
from app.models.user import User
from app.models.user_investment_context import UserInvestmentContext
from app.services.questionnaire_taxonomy import QUESTIONNAIRE_VERSION
from app.tests.conftest import TEST_USER_ID


def _seed_user(
    db_session: Session,
    *,
    user_id: uuid.UUID = TEST_USER_ID,
    email: str = "me-test@example.com",
    delivery_email: str | None = None,
    tos_accepted_at: object = None,
) -> User:
    row = User(
        id=user_id,
        auth_provider="supabase",
        auth_subject=f"sub-{user_id}",
        email=email,
        status="active",
        locale="zh",
        base_currency="USD",
        report_cadence="mwf",
        delivery_email=delivery_email,
        tos_accepted_at=tos_accepted_at,
    )
    db_session.add(row)
    db_session.flush()
    return row


@pytest.fixture
def raw_client(db_session: Session) -> Iterator[TestClient]:
    def _override_session() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides.pop(current_principal, None)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_me_without_token_is_401(raw_client: TestClient) -> None:
    resp = raw_client.get("/me")
    assert resp.status_code == 401


def test_me_returns_email_and_delivery_email(app_client: TestClient, db_session: Session) -> None:
    _seed_user(db_session, delivery_email="delivery@example.com")

    resp = app_client.get("/me")

    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "me-test@example.com"
    assert body["delivery_email"] == "delivery@example.com"


def test_me_delivery_email_null_when_unset(app_client: TestClient, db_session: Session) -> None:
    _seed_user(db_session, delivery_email=None)

    resp = app_client.get("/me")

    assert resp.json()["delivery_email"] is None


def test_me_tos_accepted_at_null_for_legacy_user(
    app_client: TestClient, db_session: Session
) -> None:
    _seed_user(db_session, tos_accepted_at=None)

    resp = app_client.get("/me")

    assert resp.json()["tos_accepted_at"] is None


def test_me_missing_never_contains_tos(app_client: TestClient, db_session: Session) -> None:
    """A NULL tos_accepted_at is audit-only — never surfaced as a fixable
    gap (Ring 1-Onboarding.md §2.6: no re-accept flow for existing users)."""
    _seed_user(db_session, tos_accepted_at=None)

    resp = app_client.get("/me")

    assert "tos" not in resp.json()["missing"]


def test_me_missing_both_when_no_questionnaire_and_no_holdings(
    app_client: TestClient, db_session: Session
) -> None:
    _seed_user(db_session)

    resp = app_client.get("/me")

    body = resp.json()
    assert body["has_questionnaire"] is False
    assert body["has_holdings"] is False
    assert set(body["missing"]) == {"questionnaire", "holdings"}


def test_me_has_questionnaire_true_when_row_exists(
    app_client: TestClient, db_session: Session
) -> None:
    _seed_user(db_session)
    db_session.add(
        UserInvestmentContext(
            user_id=TEST_USER_ID,
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
            free_text=None,
        )
    )
    db_session.flush()

    resp = app_client.get("/me")

    body = resp.json()
    assert body["has_questionnaire"] is True
    assert "questionnaire" not in body["missing"]
    assert "holdings" in body["missing"]


def test_me_has_holdings_true_when_row_exists(app_client: TestClient, db_session: Session) -> None:
    _seed_user(db_session)
    db_session.add(
        Holding(
            user_id=TEST_USER_ID,
            name="Apple",
            ticker="AAPL",
            currency="USD",
            pricing_mode="auto",
            asset_type="stock",
            asset_class="STOCK",
            position=0,
        )
    )
    db_session.flush()

    resp = app_client.get("/me")

    body = resp.json()
    assert body["has_holdings"] is True
    assert "holdings" not in body["missing"]
    assert "questionnaire" in body["missing"]


def test_me_missing_empty_when_both_present(app_client: TestClient, db_session: Session) -> None:
    _seed_user(db_session)
    db_session.add(
        UserInvestmentContext(
            user_id=TEST_USER_ID,
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
            free_text=None,
        )
    )
    db_session.add(
        Holding(
            user_id=TEST_USER_ID,
            name="Apple",
            ticker="AAPL",
            currency="USD",
            pricing_mode="auto",
            asset_type="stock",
            asset_class="STOCK",
            position=0,
        )
    )
    db_session.flush()

    resp = app_client.get("/me")

    assert resp.json()["missing"] == []
