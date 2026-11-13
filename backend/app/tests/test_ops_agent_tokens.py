"""Focused acceptance coverage for scoped Ops callers (#888)."""

import json
import logging
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.credit_ledger import CreditLedgerEntry
from app.models.invite import Invite
from app.models.report_currency_change import ReportCurrencyChange
from app.models.ticker_leverage import TickerLeverageOverride
from app.models.user_activity import UserActivity
from app.tests.conftest import seed_user


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            " reader : standard : secret-one , helper:elevated:secret-two ",
            [("reader", "standard", "secret-one"), ("helper", "elevated", "secret-two")],
        ),
        ("", []),
        ("   ", []),
        ("reader:secret-one", None),
        ("reader:other:secret-one", None),
        ("admin:standard:secret-one", None),
        ("Bad_name:standard:secret-one", None),
        ("reader:standard:", None),
        ("reader:standard:secret-one,reader:elevated:secret-two", None),
        ("reader:standard:secret-one,helper:elevated:secret-one", None),
    ],
)
def test_agent_settings(value: str, expected: list[tuple[str, str, str]] | None) -> None:
    values = get_settings().model_dump()
    values["OPS_AGENT_TOKENS"] = value
    if expected is None:
        with pytest.raises(ValidationError) as exc:
            Settings(_env_file=None, **values)
        assert "entry" in str(exc.value)
        assert "secret-one" not in str(exc.value)
        assert "secret-two" not in str(exc.value)
    else:
        settings = Settings(_env_file=None, **values)
        assert settings.ops_agent_tokens == expected


@pytest.fixture
def agent_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    values = get_settings().model_dump()
    values["OPS_AGENT_TOKENS"] = SecretStr(
        "reader:standard:agent-secret-one,helper:elevated:agent-secret-two"
    )
    configured = Settings(_env_file=None, **values)
    settings = get_settings()
    monkeypatch.setattr(settings, "OPS_AGENT_TOKENS", configured.OPS_AGENT_TOKENS)
    monkeypatch.setattr(settings, "_ops_agent_tokens", configured.ops_agent_tokens)


def headers(token: str = "agent-secret-one") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_standard_tier(app_client: TestClient, agent_settings: None) -> None:
    assert app_client.get("/admin/usage/host", headers=headers()).status_code == 200
    assert (
        app_client.post(f"/admin/users/{uuid4()}/bind-subject", headers=headers()).status_code
        == 403
    )


def test_elevated_tier(app_client: TestClient, agent_settings: None) -> None:
    auth = headers("agent-secret-two")
    response = app_client.post(
        f"/admin/users/{uuid4()}/bind-subject", headers=auth, json={"auth_subject": "new-subject"}
    )
    assert response.status_code == 404
    assert app_client.delete(f"/admin/users/{uuid4()}", headers=auth).status_code == 403
    assert app_client.post("/admin/payments/refunds", headers=auth).status_code == 403


def test_admin_tier_unchanged(app_client: TestClient) -> None:
    auth = headers(get_settings().ADMIN_API_TOKEN.get_secret_value())
    assert app_client.delete(f"/admin/users/{uuid4()}", headers=auth).status_code == 404


def test_caller_audit(
    app_client: TestClient,
    db_session: Session,
    agent_settings: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    logging.getLogger("app.core.ops_log").disabled = False
    for token, caller in [
        ("agent-secret-one", "reader"),
        (get_settings().ADMIN_API_TOKEN.get_secret_value(), "admin"),
        ("unknown-secret", None),
    ]:
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="app.core.ops_log"):
            response = app_client.get("/admin/usage/host", headers=headers(token))
        assert response.status_code == (401 if caller is None else 200)
        lines = [r.message for r in caplog.records if "admin.call" in r.message]
        assert len(lines) == 1
        detail = json.loads(lines[0].split("OPS_EVENT ", 1)[1])
        assert detail["caller"] == caller
        row = db_session.scalars(
            select(UserActivity)
            .where(UserActivity.event == "admin_call")
            .order_by(UserActivity.id.desc())
        ).first()
        assert row is not None
        assert row.detail["caller"] == caller
        assert token not in lines[0]
        assert token not in str(row.detail)


def test_credit_actor(app_client: TestClient, db_session: Session, agent_settings: None) -> None:
    user = seed_user(db_session, uuid4())
    for token, actor in [
        ("agent-secret-one", "reader"),
        (get_settings().ADMIN_API_TOKEN.get_secret_value(), None),
    ]:
        key = str(uuid4())
        response = app_client.post(
            "/admin/users/by-email/credit-adjustments",
            headers=headers(token),
            json={"email": user.email, "amount": "1.00", "note": "test", "idempotency_key": key},
        )
        assert response.status_code == 200
        row = db_session.scalar(
            select(CreditLedgerEntry).where(
                CreditLedgerEntry.idempotency_key == "admin_adjustment:" + key
            )
        )
        assert row is not None
        assert row.actor_type == "admin"
        assert row.actor_id == actor


def test_currency_actor(app_client: TestClient, db_session: Session, agent_settings: None) -> None:
    user = seed_user(db_session, uuid4())
    response = app_client.post(
        "/admin/users/by-email/report-currency",
        params={"email": user.email},
        headers=headers(),
        json={"report_currency": "HKD"},
    )
    assert response.status_code == 200
    row = db_session.scalar(
        select(ReportCurrencyChange).where(ReportCurrencyChange.user_id == user.id)
    )
    assert row is not None
    assert row.ops_agent == "reader"
    assert row.source == "admin" and row.actor_user_id is None


def test_invite_actor(app_client: TestClient, db_session: Session, agent_settings: None) -> None:
    response = app_client.post("/admin/invites", headers=headers(), json={})
    assert response.status_code == 201
    row = db_session.get(Invite, UUID(response.json()["id"]))
    assert row is not None
    assert row.ops_agent == "reader"
    assert str(row.created_by) == get_settings().ADMIN_ID


def test_leverage_actor(app_client: TestClient, db_session: Session, agent_settings: None) -> None:
    response = app_client.post(
        "/admin/ticker-leverage",
        headers=headers(),
        json={"ticker": "TOKEN888", "leverage_multiple": "2"},
    )
    assert response.status_code == 201
    row = db_session.get(TickerLeverageOverride, "TOKEN888")
    assert row is not None
    assert row.ops_agent == "reader"
    assert str(row.created_by) == get_settings().ADMIN_ID
