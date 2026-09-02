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
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import current_principal
from app.main import app
from app.models.email_verification import EmailVerification
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
    email_verified_at: object = None,
    delivery_email_verified_at: object = None,
    locale: str = "zh",
) -> User:
    row = User(
        id=user_id,
        auth_provider="supabase",
        auth_subject=f"sub-{user_id}",
        email=email,
        status="active",
        locale=locale,
        base_currency="USD",
        report_cadence="mwf",
        delivery_email=delivery_email,
        tos_accepted_at=tos_accepted_at,
        email_verified_at=email_verified_at,
        delivery_email_verified_at=delivery_email_verified_at,
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


# --- pending_email_verifications (issue #262, Profile Page.md §8.2) ---


def _seed_verification(
    db_session: Session,
    *,
    user_id: uuid.UUID | None,
    purpose: str,
    status: str,
    email: str = "pending@example.com",
) -> EmailVerification:
    row = EmailVerification(
        user_id=user_id,
        purpose=purpose,
        email=email,
        token_hash=f"hash-{uuid.uuid4()}",
        status=status,
        expires_at=datetime.now(UTC) + timedelta(hours=24),
        last_sent_at=datetime.now(UTC),
        resend_count=0,
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_me_lists_pending_and_undeliverable_for_own_user(
    app_client: TestClient, db_session: Session
) -> None:
    _seed_user(db_session)
    pending = _seed_verification(
        db_session, user_id=TEST_USER_ID, purpose="account_email", status="pending"
    )
    undeliverable = _seed_verification(
        db_session,
        user_id=TEST_USER_ID,
        purpose="delivery_email",
        status="undeliverable",
        email="typo@example.com",
    )

    body = app_client.get("/me").json()
    listed = {item["id"]: item for item in body["pending_email_verifications"]}

    assert set(listed) == {str(pending.id), str(undeliverable.id)}
    assert listed[str(pending.id)]["purpose"] == "account_email"
    assert listed[str(pending.id)]["status"] == "pending"
    assert listed[str(pending.id)]["email"] == "pending@example.com"
    assert listed[str(undeliverable.id)]["status"] == "undeliverable"
    assert listed[str(undeliverable.id)]["email"] == "typo@example.com"
    assert "expires_at" in listed[str(pending.id)]
    assert "last_sent_at" in listed[str(pending.id)]


def test_me_excludes_terminal_and_other_user_statuses(
    app_client: TestClient, db_session: Session
) -> None:
    """expired/superseded/verified/revoked are history, not actionable
    (Profile Page.md §8.2) — and another user's live pending rows are not
    ours to see."""
    _seed_user(db_session)
    _seed_verification(db_session, user_id=TEST_USER_ID, purpose="account_email", status="expired")
    _seed_verification(
        db_session, user_id=TEST_USER_ID, purpose="account_email", status="superseded"
    )
    _seed_verification(db_session, user_id=TEST_USER_ID, purpose="account_email", status="verified")
    other_user_id = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
    _seed_user(db_session, user_id=other_user_id, email="other@example.com")
    _seed_verification(
        db_session,
        user_id=other_user_id,
        purpose="delivery_email",
        status="pending",
    )

    assert app_client.get("/me").json()["pending_email_verifications"] == []


def test_me_never_lists_ops_manual_rows(app_client: TestClient, db_session: Session) -> None:
    """ops_manual is always user_id=NULL (§3.5) so it cannot belong to any
    user — belt-and-braces against a row ever carrying a user_id."""
    _seed_user(db_session)
    _seed_verification(db_session, user_id=None, purpose="ops_manual", status="pending")

    assert app_client.get("/me").json()["pending_email_verifications"] == []


def test_me_pending_list_empty_when_no_verifications(
    app_client: TestClient, db_session: Session
) -> None:
    _seed_user(db_session)

    assert app_client.get("/me").json()["pending_email_verifications"] == []


# --- email_verified_at / delivery_email_verified_at (issue #269) ---


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_me_exposes_email_verification_timestamps(
    app_client: TestClient, db_session: Session
) -> None:
    verified_at = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    _seed_user(
        db_session,
        email_verified_at=verified_at,
        delivery_email_verified_at=verified_at,
    )

    body = app_client.get("/me").json()
    assert _parse_iso(body["email_verified_at"]) == verified_at
    assert _parse_iso(body["delivery_email_verified_at"]) == verified_at


def test_me_verification_timestamps_null_when_unset(
    app_client: TestClient, db_session: Session
) -> None:
    _seed_user(db_session)

    body = app_client.get("/me").json()
    assert body["email_verified_at"] is None
    assert body["delivery_email_verified_at"] is None


# --- report_language (issue #308) ---


def test_me_exposes_report_language_from_locale(
    app_client: TestClient, db_session: Session
) -> None:
    _seed_user(db_session, locale="en")

    body = app_client.get("/me").json()
    assert body["report_language"] == "en"
