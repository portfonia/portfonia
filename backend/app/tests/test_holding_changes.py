"""Issue #769 acceptance contracts backed by PostgreSQL."""

import json
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from app.core.timezones import ET, today_et
from app.models.holding import Holding
from app.models.holding_change_group import HoldingChangeGroup
from app.models.user import User
from app.tests.conftest import TEST_USER_ID, seed_user

WEB = "/holdings"
AGENT = "/agent/v1/holdings"
ROW = {
    "name": "Apple",
    "ticker": "AAPL",
    "currency": "USD",
    "pricing_mode": "auto",
    "shares": 10,
    "market": "US",
    "asset_type": "stock",
}


@pytest.fixture
def writer(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> dict[str, str]:
    user = seed_user(db_session, TEST_USER_ID)
    user.subscription_status = "active"
    user.subscription_type = "daily"
    user.subscription_expires_on = today_et() + timedelta(days=30)
    db_session.commit()
    monkeypatch.setattr("app.core.agent_limits.quiet_retry_after", lambda _: None)
    monkeypatch.setattr("app.tasks.capture_tasks.backfill_ohlcv_task.delay", lambda _: None)
    monkeypatch.setattr("app.tasks.capture_tasks.backfill_fund_navs_task.delay", lambda _: None)
    monkeypatch.setattr(
        "app.tasks.notification_tasks.send_api_access_notice_task.delay", lambda _: None
    )
    monkeypatch.setattr(
        "app.tasks.notification_tasks.send_api_modification_notice_task.delay", lambda _: None
    )
    response = app_client.post(
        "/me/api-tokens", json={"name": "writer", "permission": "holdings_write"}
    )
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['token']}", "Idempotency-Key": "create-1"}


def create(client: TestClient) -> dict[str, object]:
    response = client.post(WEB, json=ROW)
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


def groups(session: Session) -> list[HoldingChangeGroup]:
    return list(
        session.scalars(
            select(HoldingChangeGroup).order_by(
                HoldingChangeGroup.created_at, HoldingChangeGroup.id
            )
        ).all()
    )


def clear(session: Session) -> None:
    session.execute(delete(HoldingChangeGroup))
    session.commit()


def params() -> dict[str, str]:
    return {"start": str(today_et()), "end": str(today_et())}


def test_01_web_patch(app_client: TestClient, db_session: Session, writer: dict[str, str]) -> None:
    row = create(app_client)
    clear(db_session)
    assert app_client.patch(f"{WEB}/{row['id']}", json={"shares": 12}).status_code == 200
    [group] = groups(db_session)
    assert group.source == "web"
    assert json.loads(group.changes) == [
        {
            "holding_id": row["id"],
            "holding_name": "Apple",
            "kind": "updated",
            "fields": [{"field": "shares", "before": "10", "after": "12"}],
        }
    ]


def test_02_web_create(app_client: TestClient, db_session: Session, writer: dict[str, str]) -> None:
    row = create(app_client)
    [group] = groups(db_session)
    [change] = json.loads(group.changes)
    assert change["kind"] == "created" and change["holding_id"] == row["id"]
    assert all(f["before"] is None and f["after"] is not None for f in change["fields"])
    assert change["fields"][-1] == {"field": "position", "before": None, "after": 0}
    assert [f["field"] for f in change["fields"]] == [
        "name",
        "ticker",
        "currency",
        "shares",
        "pricing_mode",
        "asset_type",
        "market",
        "position",
    ]


def test_03_web_delete(app_client: TestClient, db_session: Session, writer: dict[str, str]) -> None:
    row = create(app_client)
    original = json.loads(groups(db_session)[0].changes)[0]["fields"]
    clear(db_session)
    assert app_client.delete(f"{WEB}/{row['id']}").status_code == 204
    [group] = groups(db_session)
    [change] = json.loads(group.changes)
    assert change["kind"] == "deleted"
    assert change["fields"] == [
        {"field": f["field"], "before": f["after"], "after": None} for f in original
    ]


def test_04_reorder(app_client: TestClient, db_session: Session, writer: dict[str, str]) -> None:
    ids = [create(app_client)["id"] for _ in range(3)]
    clear(db_session)
    assert (
        app_client.patch(WEB + "/reorder", json={"ids": [ids[2], ids[0], ids[1]]}).status_code
        == 200
    )
    [group] = groups(db_session)
    assert [r["fields"] for r in json.loads(group.changes)] == [
        [{"field": "position", "before": old, "after": new}]
        for old, new in [(2, 0), (0, 1), (1, 2)]
    ]


def test_05_file_replace_append(
    app_client: TestClient, db_session: Session, writer: dict[str, str]
) -> None:
    old = {create(app_client)["id"] for _ in range(2)}
    clear(db_session)
    response = app_client.post(WEB + "/confirm?mode=replace", json=[ROW, ROW])
    assert response.status_code == 200
    [group] = groups(db_session)
    changes = json.loads(group.changes)
    assert group.source == "file"
    assert [r["kind"] for r in changes] == ["created", "created", "deleted", "deleted"]
    assert {r["holding_id"] for r in changes if r["kind"] == "deleted"} == old
    assert old.isdisjoint(r["id"] for r in response.json())
    clear(db_session)
    assert app_client.post(WEB + "/confirm?mode=append", json=[ROW]).status_code == 200
    [group] = groups(db_session)
    assert group.source == "file" and [r["kind"] for r in json.loads(group.changes)] == ["created"]


def test_06_agent_token(
    app_client: TestClient, db_session: Session, writer: dict[str, str]
) -> None:
    row = create(app_client)
    clear(db_session)
    token = app_client.post(
        "/me/api-tokens", json={"name": "sync", "permission": "holdings_write"}
    ).json()
    result = app_client.patch(
        f"{AGENT}/{row['id']}",
        headers={"Authorization": "Bearer " + token["token"]},
        json={"version": "1", "changes": {"shares": "12"}},
    )
    assert result.status_code == 200, result.text
    response = app_client.get(AGENT + "/changes", headers=writer, params=params())
    assert response.status_code == 200, response.text
    [item] = response.json()["items"]
    assert item["source"] == "agent" and item["token"] == {
        "name": "sync",
        "prefix": token["prefix"],
    }
    assert datetime.fromisoformat(item["changed_at"]).utcoffset() == datetime.now(ET).utcoffset()


def test_07_agent_delete_ids(
    app_client: TestClient, db_session: Session, writer: dict[str, str]
) -> None:
    create(app_client)
    b = create(app_client)
    clear(db_session)
    book = app_client.get(AGENT, headers=writer).json()
    response = app_client.put(
        AGENT,
        headers=writer,
        json={"version": "2", "holdings": [book["holdings"][0]], "delete_ids": [b["id"]]},
    )
    assert response.status_code == 200, response.text
    [group] = groups(db_session)
    [row] = json.loads(group.changes)
    assert row["kind"] == "deleted" and row["holding_id"] == b["id"]


def test_08_system_fields_excluded(
    app_client: TestClient, db_session: Session, writer: dict[str, str]
) -> None:
    row = create(app_client)
    holding = db_session.get(Holding, UUID(str(row["id"])))
    assert holding is not None
    holding.market_price = Decimal("100")
    holding.price_as_of = datetime.now(ET)
    db_session.commit()
    clear(db_session)
    assert app_client.patch(f"{WEB}/{row['id']}", json={"ticker": "MSFT"}).status_code == 200
    [group] = groups(db_session)
    assert json.loads(group.changes)[0]["fields"] == [
        {"field": "ticker", "before": "AAPL", "after": "MSFT"}
    ]
    db_session.refresh(holding)
    assert holding.market_price is None and holding.price_as_of is None


def test_09_empty_patch(
    app_client: TestClient, db_session: Session, writer: dict[str, str]
) -> None:
    row = create(app_client)
    clear(db_session)
    assert app_client.patch(f"{WEB}/{row['id']}", json={}).status_code == 200
    response = app_client.patch(
        f"{AGENT}/{row['id']}", headers=writer, json={"version": "2", "changes": {}}
    )
    assert response.status_code == 200 and response.json()["version"] == "3"
    assert groups(db_session) == []


def test_10_replay_and_conflict(
    app_client: TestClient, db_session: Session, writer: dict[str, str]
) -> None:
    body = {"version": "0", "holding": {**ROW, "shares": "10"}}
    assert app_client.post(AGENT, headers=writer, json=body).status_code == 201
    assert (
        app_client.post(AGENT, headers=writer, json=body).headers["Idempotency-Replayed"] == "true"
    )
    holding_id = json.loads(groups(db_session)[0].changes)[0]["holding_id"]
    response = app_client.patch(
        AGENT + "/" + holding_id, headers=writer, json={"version": "0", "changes": {"shares": "12"}}
    )
    assert response.status_code == 409 and response.json()["detail"]["code"] == "version_conflict"
    assert len(groups(db_session)) == 1


def test_11_encryption(app_client: TestClient, db_session: Session, writer: dict[str, str]) -> None:
    create(app_client)
    raw = db_session.execute(text("SELECT changes FROM holding_change_groups")).scalar_one()
    assert "Apple" not in raw and "shares" not in raw


def test_12_range(app_client: TestClient, writer: dict[str, str]) -> None:
    today = today_et()
    for start, end in [
        (today - timedelta(days=30), today),
        (today, today + timedelta(days=1)),
        (today, today - timedelta(days=1)),
    ]:
        assert (
            app_client.get(
                WEB + "/changes", params={"start": str(start), "end": str(end)}
            ).status_code
            == 422
        )


def test_13_limit(app_client: TestClient, db_session: Session, writer: dict[str, str]) -> None:
    now = datetime.now(ET)
    records = [
        HoldingChangeGroup(
            user_id=TEST_USER_ID,
            source="web",
            changes="[]",
            created_at=now - timedelta(microseconds=i),
        )
        for i in range(101)
    ]
    db_session.add_all(records)
    db_session.commit()
    response = app_client.get(WEB + "/changes", params=params())
    assert response.status_code == 200
    assert response.json()["truncated"] is True and [r["id"] for r in response.json()["items"]] == [
        str(r.id) for r in records[:100]
    ]


def test_14_ownership(app_client: TestClient, db_session: Session, writer: dict[str, str]) -> None:
    other = uuid4()
    seed_user(db_session, other)
    db_session.add_all(
        [
            HoldingChangeGroup(user_id=other, source="web", changes="[]"),
            HoldingChangeGroup(user_id=TEST_USER_ID, source="web", changes="[]"),
        ]
    )
    db_session.commit()
    for path, headers in [(WEB, {}), (AGENT, writer)]:
        response = app_client.get(path + "/changes", headers=headers, params=params())
        assert response.status_code == 200
        assert len(response.json()["items"]) == 1


def test_15_cleanup(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tasks.operational_events_tasks import cleanup_operational_events

    seed_user(db_session, TEST_USER_ID)
    now = datetime.now(ET)
    db_session.add_all(
        [
            HoldingChangeGroup(
                user_id=TEST_USER_ID,
                source="web",
                changes="[]",
                created_at=now - timedelta(days=31),
            ),
            HoldingChangeGroup(
                user_id=TEST_USER_ID,
                source="web",
                changes="[]",
                created_at=now - timedelta(days=29),
            ),
        ]
    )
    db_session.commit()
    monkeypatch.setattr("app.core.database.SessionLocal", lambda: db_session)
    result = cleanup_operational_events.run()
    assert result["holding_changes_deleted"] == 1 and len(groups(db_session)) == 1


def test_16_cascade(db_session: Session) -> None:
    uid = uuid4()
    seed_user(db_session, uid)
    db_session.add(HoldingChangeGroup(user_id=uid, source="web", changes="[]"))
    db_session.commit()
    db_session.execute(delete(User).where(User.id == uid))
    db_session.commit()
    assert groups(db_session) == []


def test_17_entitlement(
    app_client: TestClient, db_session: Session, writer: dict[str, str]
) -> None:
    token = app_client.post("/me/api-tokens", json={"name": "reader"}).json()
    headers = {"Authorization": "Bearer " + token["token"]}
    assert app_client.get(AGENT + "/changes", params=params(), headers=headers).status_code == 200
    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    user.subscription_status = "expired"
    db_session.commit()
    result = app_client.get(AGENT + "/changes", params=params(), headers=headers)
    assert result.status_code == 403 and result.json()["detail"]["code"] == "subscription_required"


def test_18_shared_notice(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, writer: dict[str, str]
) -> None:
    notice = Mock()
    monkeypatch.setattr("app.tasks.notification_tasks.send_api_access_notice_task.delay", notice)
    for path in ["/agent/v1/snapshots", AGENT, AGENT + "/changes"]:
        response = app_client.get(path, headers=writer, params=params())
        assert response.status_code == 200, response.text
    assert notice.call_count == 1
    from app.core.rate_limit import get_backend

    get_backend().delete(f"agent:notice:{TEST_USER_ID}:{today_et()}")
    assert (
        app_client.post(
            AGENT + "/validate",
            headers=writer,
            json={
                "operation": "create",
                "version": "0",
                "holding": {"name": "Widget", "currency": "USD", "pricing_mode": "manual"},
            },
        ).status_code
        == 200
    )
    assert notice.call_count == 1


def test_19_token(
    app_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    writer: dict[str, str],
) -> None:
    from app.core.rate_limit import get_backend

    notice = Mock()
    monkeypatch.setattr("app.tasks.notification_tasks.send_api_access_notice_task.delay", notice)
    token = app_client.post(
        "/me/api-tokens", json={"name": "reader", "expires_on": str(today_et() + timedelta(days=1))}
    ).json()
    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    for advanced, permission, headers in [
        (False, "read_only", {"Authorization": "Bearer " + token["token"]}),
        (True, "read_only", {"Authorization": "Bearer " + token["token"]}),
        (True, "holdings_write", writer),
    ]:
        user.subscription_status = "active" if advanced else "expired"
        db_session.commit()
        response = app_client.get("/agent/v1/token", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["capabilities"] == {
            "reports": True,
            "snapshots": advanced,
            "intel": advanced,
            "holdings_read": advanced,
            "holdings_write": advanced and permission == "holdings_write",
        }
        assert body["permission"] == permission and len(body["prefix"]) == 12
        assert body["name"] == ("writer" if permission == "holdings_write" else "reader")
        assert (
            datetime.fromisoformat(body["created_at"]).utcoffset() == datetime.now(ET).utcoffset()
        )
        assert (body["expires_at"] is None) == (permission == "holdings_write")
    get_backend().delete(f"agent:burst:{TEST_USER_ID}")
    for _ in range(10):
        assert app_client.get("/agent/v1/token", headers=writer).status_code == 200
    response = app_client.get("/agent/v1/token", headers=writer)
    assert response.status_code == 429 and int(response.headers["Retry-After"]) > 0
    notice.assert_not_called()


def test_20_notice_copy() -> None:
    from app.services.email_sender import _API_ACCESS_NOTICE_COPY, _API_MODIFICATION_NOTICE_COPY

    for copy in [_API_ACCESS_NOTICE_COPY, _API_MODIFICATION_NOTICE_COPY]:
        assert "This link revokes every API token on your account at once" in copy["en"]["body"]
        for locale in ["zh", "zh-Hant"]:
            assert "token" in copy[locale]["body"].lower() and len(copy[locale]["body"]) > 100
            assert "\u5176\u4ed6\u667a\u80fd" in copy[locale]["body"]
    assert "holding change history" in _API_ACCESS_NOTICE_COPY["en"]["body"]
