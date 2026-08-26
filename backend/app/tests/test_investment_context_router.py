"""Integration tests for /investment-context — real Postgres (issue #129
checkpoint B6, Ring 1-B design.md §8.6)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.user_investment_context import UserInvestmentContext
from app.services.questionnaire_taxonomy import QUESTIONNAIRE_VERSION
from app.tests.conftest import TEST_USER_ID

_VALID_QUESTIONNAIRE: dict[str, object] = {
    "asset_scale": "100K_500K",
    "markets": ["US", "HK"],
    "style": "GROWTH",
    "horizon": "LONG",
    "risk_appetite": "BALANCED",
    "sectors_of_interest": ["Technology", "Healthcare"],
    "objective": "GROWTH",
    "intel_focus": "MACRO",
}


def _seed_user(db_session: Session, user_id: object = TEST_USER_ID) -> None:
    """FK target: user_investment_context.user_id -> users.id (this table
    postdates B4's users, unlike holdings/reports — see the model docstring).
    No other table in this codebase enforces this FK yet (B7 is still
    pending for those four), so app_client's default fixture never needed a
    seeded users row before this router existed."""
    db_session.add(
        User(
            id=user_id,
            auth_provider="supabase",
            auth_subject=f"sub-{user_id}",
            email="questionnaire-test@example.com",
            status="active",
            locale="zh",
            base_currency="USD",
            report_cadence="mwf",
        )
    )
    db_session.flush()


def test_get_investment_context_404_when_none_on_file(app_client: TestClient) -> None:
    resp = app_client.get("/investment-context")
    assert resp.status_code == 404


def test_put_then_get_round_trips(app_client: TestClient, db_session: Session) -> None:
    _seed_user(db_session)
    put_resp = app_client.put(
        "/investment-context",
        json={"questionnaire": _VALID_QUESTIONNAIRE, "free_text": "Long-term, tech-heavy."},
    )
    assert put_resp.status_code == 200, put_resp.text
    body = put_resp.json()
    assert body["questionnaire"]["style"] == "GROWTH"
    assert body["questionnaire_version"] == QUESTIONNAIRE_VERSION
    assert body["free_text"] == "Long-term, tech-heavy."

    get_resp = app_client.get("/investment-context")
    assert get_resp.status_code == 200
    assert get_resp.json()["questionnaire"] == _VALID_QUESTIONNAIRE


def test_put_rejects_unrecognized_enum_value_with_422(
    app_client: TestClient, db_session: Session
) -> None:
    _seed_user(db_session)
    bad = {**_VALID_QUESTIONNAIRE, "style": "MOMENTUM"}
    resp = app_client.put("/investment-context", json={"questionnaire": bad, "free_text": None})
    assert resp.status_code == 422


def test_reanswer_overwrites_not_merges(app_client: TestClient, db_session: Session) -> None:
    """Concept §4.2: re-answering the questionnaire replaces the record
    wholesale, not a partial-field merge."""
    _seed_user(db_session)
    app_client.put(
        "/investment-context",
        json={"questionnaire": _VALID_QUESTIONNAIRE, "free_text": "first answer"},
    )
    second = {**_VALID_QUESTIONNAIRE, "style": "VALUE", "risk_appetite": "CONSERVATIVE"}
    resp = app_client.put("/investment-context", json={"questionnaire": second, "free_text": None})
    assert resp.status_code == 200
    body = resp.json()
    assert body["questionnaire"]["style"] == "VALUE"
    assert body["questionnaire"]["risk_appetite"] == "CONSERVATIVE"
    # free_text was overwritten to None, not left as "first answer".
    assert body["free_text"] is None


def test_no_system_inference_endpoint_exists(app_client: TestClient, db_session: Session) -> None:
    """§8.4: there must be no endpoint that reads back a system-inferred
    conclusion — GET only ever returns what the user themselves submitted."""
    _seed_user(db_session)
    app_client.put(
        "/investment-context",
        json={"questionnaire": _VALID_QUESTIONNAIRE, "free_text": None},
    )
    resp = app_client.get("/investment-context")
    body = resp.json()
    assert set(body.keys()) == {"questionnaire", "questionnaire_version", "free_text", "updated_at"}


def test_row_survives_free_text_round_trip_via_encryption(
    app_client: TestClient, db_session: Session
) -> None:
    """The model wraps free_text in EncryptedString — confirm the router
    reads back plaintext, not ciphertext, and that a direct ORM read also
    decrypts transparently (holdings.py's own encryption tests follow this
    same read-through-the-ORM pattern)."""
    _seed_user(db_session)
    text = "特殊说明: 部分仓位是历史遗留。"
    app_client.put(
        "/investment-context",
        json={"questionnaire": _VALID_QUESTIONNAIRE, "free_text": text},
    )
    row = db_session.get(UserInvestmentContext, TEST_USER_ID)
    assert row is not None
    assert row.free_text == text
