"""Issue #770 acceptance contracts with cached data and real PostgreSQL."""

from datetime import timedelta
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.rate_limit import get_backend
from app.models.user import User
from app.services import jade_replay
from app.services.email_sender import _API_ACCESS_NOTICE_COPY, _API_MODIFICATION_NOTICE_COPY
from app.services.jade_replay_config import SCENARIOS
from app.tests.conftest import TEST_USER_ID, seed_user
from app.tests.test_agent_api import bearer, create
from app.tests.test_jade_replay import E, calendar, holding, series

ROOT = "/agent/v1/jade/"
TOOLS = ["replay", "tail-risk", "stress", "style", "tracking"]


@pytest.fixture
def reader(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> dict[str, str]:
    user = seed_user(db_session, TEST_USER_ID)
    user.subscription_status = "active"
    user.subscription_type = "jade"
    user.report_cadence = "weekly"
    db_session.commit()
    monkeypatch.setattr("app.core.agent_limits.quiet_retry_after", lambda _: None)
    monkeypatch.setattr(jade_replay, "today_et", lambda: E)
    monkeypatch.setattr(
        "app.tasks.notification_tasks.send_api_access_notice_task.delay", lambda _: None
    )
    days = [E - timedelta(days=i) for i in reversed(range(16))]
    calendar(db_session, days)
    holding(db_session, asset_type="cash")
    series(db_session, "yf:SPY", days, [100 + i for i in range(16)])
    db_session.commit()
    return bearer(create(app_client))


@pytest.mark.parametrize("tool", TOOLS)
def test_01_non_jade(
    app_client: TestClient, db_session: Session, reader: dict[str, str], tool: str
) -> None:
    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    user.subscription_type = "daily"
    db_session.commit()
    response = app_client.get(ROOT + tool, headers=reader, params={"scenario": "covid_2020"})
    assert response.status_code == 403 and response.json()["detail"] == "subscription_required"


def test_02_replay_matches_service(
    app_client: TestClient, db_session: Session, reader: dict[str, str]
) -> None:
    expected = jade_replay.compute_replay(db_session, TEST_USER_ID, "USD", "sp500", "5Y")
    assert expected.status == "ok" and expected.points
    response = app_client.get(ROOT + "replay?range=5Y", headers=reader)
    assert response.status_code == 200, response.text
    assert response.json() == expected.model_dump(mode="json")


def test_03_single_scenario_and_web(app_client: TestClient, reader: dict[str, str]) -> None:
    assert app_client.get(ROOT + "stress", headers=reader).status_code == 422
    get_backend().delete(f"agent:jade:interval:{TEST_USER_ID}")
    response = app_client.get(ROOT + "stress?scenario=covid_2020", headers=reader)
    assert response.status_code == 200, response.text
    assert [s["id"] for s in response.json()["scenarios"]] == ["covid_2020"]
    web = app_client.get("/jade/stress")
    assert web.status_code == 200
    assert [s["id"] for s in web.json()["scenarios"]] == [s.id for s in SCENARIOS]
    assert response.json()["scenarios"][0] == web.json()["scenarios"][2]


def test_04_shared_interval(app_client: TestClient, reader: dict[str, str]) -> None:
    assert app_client.get(ROOT + "replay", headers=reader).status_code == 200
    response = app_client.get(ROOT + "tracking", headers=reader)
    assert response.status_code == 429 and 1 <= int(response.headers["Retry-After"]) <= 180


def test_05_shared_hour(app_client: TestClient, reader: dict[str, str]) -> None:
    store = get_backend()
    for i in range(11):
        store.delete(f"agent:jade:interval:{TEST_USER_ID}")
        # Model calls spaced by the interval without a real three-minute wait.
        store.delete(f"agent:burst:{TEST_USER_ID}")
        response = app_client.get(ROOT + ("replay" if i % 2 else "tracking"), headers=reader)
        assert response.status_code == (200 if i < 10 else 429), response.text
    assert int(response.headers["Retry-After"]) > 0


def test_06_existing_burst(app_client: TestClient, reader: dict[str, str]) -> None:
    for _ in range(10):
        assert app_client.get("/agent/v1/token", headers=reader).status_code == 200
    response = app_client.get(ROOT + "replay", headers=reader)
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) == 900
    assert get_backend().ttl(f"agent:lock:{TEST_USER_ID}") > 0


def test_07_shared_notice(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, reader: dict[str, str]
) -> None:
    notice = Mock()
    monkeypatch.setattr("app.tasks.notification_tasks.send_api_access_notice_task.delay", notice)
    assert app_client.get(ROOT + "replay", headers=reader).status_code == 200
    response = app_client.get(
        "/agent/v1/snapshots", headers=reader, params={"start": str(E), "end": str(E)}
    )
    assert response.status_code == 200, response.text
    notice.assert_called_once_with(str(TEST_USER_ID))


def test_08_schema(app_client: TestClient) -> None:
    document = app_client.get("/agent/openapi.json").json()
    paths = document["paths"]
    for tool in TOOLS:
        assert "get" in paths[ROOT + tool]
    assert not any(p.startswith("/jade/") for p in paths)
    assert "jade" in document["components"]["schemas"]["AgentCapabilities"]["required"]


def test_09_read_copy() -> None:
    assert "Jade risk results" in _API_ACCESS_NOTICE_COPY["en"]["body"]
    assert "Jade" in _API_ACCESS_NOTICE_COPY["zh"]["body"]
    assert "Jade" in _API_ACCESS_NOTICE_COPY["zh-Hant"]["body"]


def test_11_capability(app_client: TestClient, db_session: Session, reader: dict[str, str]) -> None:
    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    for plan, jade in [("jade", True), ("daily", False)]:
        user.subscription_type = plan
        db_session.commit()
        response = app_client.get("/agent/v1/token", headers=reader)
        assert response.status_code == 200
        assert response.json()["permission"] == "read_only"
        assert response.json()["capabilities"] == {
            "reports": True,
            "snapshots": True,
            "intel": True,
            "holdings_read": True,
            "holdings_write": False,
            "jade": jade,
        }


@pytest.mark.parametrize("copies", [_API_ACCESS_NOTICE_COPY, _API_MODIFICATION_NOTICE_COPY])
@pytest.mark.parametrize("locale", ["en", "zh", "zh-Hant"])
def test_12_link_layout(copies: dict[str, dict[str, str]], locale: str) -> None:
    url = "https://portfonia.com/agent/revoke?t=sample"
    assert f"\n\n{url}\n\n" in copies[locale]["body"].format(revoke_url=url)
