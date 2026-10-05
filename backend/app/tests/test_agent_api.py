"""Personal API token acceptance contracts (#651), backed by PostgreSQL."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from typing import cast
from unittest.mock import patch

import pytest
from celery.exceptions import Retry  # type: ignore[import-untyped]
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.timezones import ET, today_et
from app.tests.conftest import TEST_USER_ID, seed_user

TOKENS = "/me/api-tokens"
AGENT = "/agent/v1/snapshots"
NOW = datetime(2026, 10, 5, 18, tzinfo=ET)


def create(client: TestClient, name: str = "My agent") -> dict[str, object]:
    response = client.post(TOKENS, json={"name": name})
    assert response.status_code == 201
    return cast(dict[str, object], response.json())


def test_acceptance_01_show_once_hash_only(app_client: TestClient, db_session: Session) -> None:
    seed_user(db_session, TEST_USER_ID)
    body = create(app_client)
    token = str(body["token"])
    assert token.startswith("pfa_") and len(token) >= 40
    row = (
        db_session.execute(text("SELECT * FROM api_tokens WHERE id = :id"), {"id": body["id"]})
        .mappings()
        .one()
    )
    assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert row["token_prefix"] == token[:12] == body["prefix"]
    assert all(token not in value for value in row.values() if isinstance(value, str))
    response = app_client.get(TOKENS)
    assert response.status_code == 200
    assert token not in response.text
    assert response.json()[0]["status"] == "active"
    assert set(response.json()[0]) == {
        "id",
        "name",
        "prefix",
        "created_at",
        "expires_at",
        "last_used_at",
        "status",
    }


def test_acceptance_02_limit_and_revoke(app_client: TestClient, db_session: Session) -> None:
    seed_user(db_session, TEST_USER_ID)
    tokens = [create(app_client, f"Agent {i}") for i in range(5)]
    response = app_client.post(TOKENS, json={"name": "Sixth"})
    assert response.status_code == 409
    assert response.json() == {"detail": "token_limit"}
    target = tokens[0]["id"]
    assert app_client.delete(f"{TOKENS}/{target}").status_code == 204
    assert app_client.delete(f"{TOKENS}/{target}").status_code == 404
    assert len(app_client.get(TOKENS).json()) == 4
    assert create(app_client, "Replacement")["id"]
    assert (
        db_session.execute(
            text("SELECT revoked_by FROM api_tokens WHERE id = :id"), {"id": target}
        ).scalar_one()
        == "user"
    )


def test_token_creation_validation_and_expiry(app_client: TestClient, db_session: Session) -> None:
    seed_user(db_session, TEST_USER_ID)
    for body in [
        {"name": ""},
        {"name": "  "},
        {"name": "x" * 51},
        {"name": "ok", "expires_on": today_et().isoformat()},
    ]:
        assert app_client.post(TOKENS, json=body).status_code == 422
    tomorrow = today_et() + timedelta(days=1)
    response = app_client.post(
        TOKENS, json={"name": "  Trimmed  ", "expires_on": tomorrow.isoformat()}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Trimmed"
    expires = datetime.fromisoformat(body["expires_at"]).astimezone(ET)
    assert expires.date() == tomorrow
    assert (expires.hour, expires.minute, expires.second) == (23, 59, 59)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    from app.services import api_tokens

    moment = [NOW]
    monkeypatch.setattr(api_tokens, "now_et", lambda: moment[0])
    monkeypatch.setattr("app.routers.api_tokens.now_et", lambda: moment[0])
    return moment


def params() -> dict[str, str]:
    day = today_et().isoformat()
    return {"start": day, "end": day}


def bearer(body: dict[str, object]) -> dict[str, str]:
    return {"Authorization": f"Bearer {body['token']}"}


def test_acceptance_03_unused_expiry(
    app_client: TestClient, db_session: Session, clock: list[datetime]
) -> None:
    seed_user(db_session, TEST_USER_ID)
    body = create(app_client)
    db_session.execute(
        text("UPDATE api_tokens SET last_used_at = :last WHERE id = :id"),
        {"last": NOW - timedelta(days=60, seconds=1), "id": body["id"]},
    )
    db_session.commit()
    response = app_client.get(AGENT, params=params(), headers=bearer(body))
    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    assert app_client.get(TOKENS).json()[0]["status"] == "expired_unused"


def test_acceptance_04_auth_surfaces(app_client: TestClient, db_session: Session) -> None:
    from app.core.deps import current_principal
    from app.main import app

    seed_user(db_session, TEST_USER_ID)
    body = create(app_client)
    override = app.dependency_overrides.pop(current_principal)
    try:
        assert app_client.get("/me", headers=bearer(body)).status_code == 401
        for method in ("GET", "POST", "DELETE"):
            target = TOKENS if method != "DELETE" else f"{TOKENS}/{body['id']}"
            assert (
                app_client.request(
                    method, target, headers=bearer(body), json={"name": "Rejected"}
                ).status_code
                == 401
            )
    finally:
        app.dependency_overrides[current_principal] = override
    response = app_client.get(
        AGENT, params=params(), headers={"Authorization": "Bearer session-jwt"}
    )
    assert response.status_code == 401


def test_acceptance_05_snapshot_equivalence_and_entitlement(
    app_client: TestClient, db_session: Session
) -> None:
    from app.tests.test_portfolio_snapshot_export import _batch, _row

    user = seed_user(db_session, TEST_USER_ID)
    user.subscription_status = "active"
    user.subscription_type = "daily"
    db_session.flush()
    _batch(db_session, today_et())
    _row(db_session, today_et())
    body = create(app_client)
    web = app_client.get("/portfolio/snapshots", params=params())
    agent = app_client.get(AGENT, params=params(), headers=bearer(body))
    assert agent.status_code == web.status_code == 200
    assert agent.json() == web.json()
    user.subscription_type = "weekly"
    db_session.commit()
    response = app_client.get(AGENT, params=params(), headers=bearer(body))
    assert response.status_code == 403
    assert response.json() == {"detail": "subscription_required"}


@pytest.fixture
def advanced(
    app_client: TestClient, db_session: Session, clock: list[datetime]
) -> dict[str, object]:
    user = seed_user(db_session, TEST_USER_ID)
    user.subscription_status = "active"
    user.subscription_type = "daily"
    db_session.flush()
    return create(app_client)


def test_acceptance_06_hourly_limit(app_client: TestClient, advanced: dict[str, object]) -> None:
    from typing import cast

    from app.core.rate_limit import InMemoryBackend, get_backend

    store = cast(InMemoryBackend, get_backend())
    for _ in range(20):
        assert app_client.get(AGENT, params=params(), headers=bearer(advanced)).status_code == 200
        store.advance(61)
    response = app_client.get(AGENT, params=params(), headers=bearer(advanced))
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) == 2380


def test_acceptance_07_burst_lock(app_client: TestClient, advanced: dict[str, object]) -> None:
    from typing import cast

    from app.core.rate_limit import InMemoryBackend, get_backend

    store = cast(InMemoryBackend, get_backend())
    for _ in range(10):
        assert app_client.get(AGENT, params=params(), headers=bearer(advanced)).status_code == 200
    response = app_client.get(AGENT, params=params(), headers=bearer(advanced))
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "900"
    store.advance(60)
    response = app_client.get(AGENT, params=params(), headers=bearer(advanced))
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "840"
    assert store.ttl(f"agent:lock:{TEST_USER_ID}") == 840


@pytest.mark.parametrize(
    ("hour", "minute", "second", "key", "retry"),
    [
        (16, 56, 0, None, "540"),
        (17, 0, 3, None, "297"),
        (18, 0, 0, "agent:quiet:active:probe", "300"),
        (18, 0, 0, "agent:quiet:cooldown", "120"),
        (17, 5, 1, None, None),
        (18, 0, 0, None, None),
    ],
)
def test_acceptance_08_quiet_window(
    app_client: TestClient,
    advanced: dict[str, object],
    clock: list[datetime],
    hour: int,
    minute: int,
    second: int,
    key: str | None,
    retry: str | None,
) -> None:
    from app.core.rate_limit import get_backend

    clock[0] = NOW.replace(hour=hour, minute=minute, second=second)
    if key:
        get_backend().set_nx(key, 120 if key.endswith("cooldown") else 14400)
    response = app_client.get(AGENT, params=params(), headers=bearer(advanced))
    assert response.status_code == (429 if retry else 200)
    if retry:
        assert response.headers["Retry-After"] == retry


def test_agent_redis_failure_closed(app_client: TestClient, advanced: dict[str, object]) -> None:
    from app.core.rate_limit import RateLimitUnavailable, get_backend

    with patch.object(get_backend(), "ttl", side_effect=RateLimitUnavailable):
        assert app_client.get(AGENT, params=params(), headers=bearer(advanced)).status_code == 503


def test_acceptance_09_connected_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    from celery.signals import before_task_publish, task_postrun  # type: ignore[import-untyped]

    from app.core.rate_limit import get_backend
    from app.tasks import celery_app

    monkeypatch.setattr("app.core.operational_events.emit_dispatch", lambda *args, **kwargs: None)
    task_name = "app.tasks.report_tasks.generate_incremental_report"
    key = "agent:quiet:active:probe"
    before_task_publish.send(sender=task_name, headers={"id": "probe"})
    assert get_backend().ttl(key) == 14400
    task = celery_app.tasks[task_name]
    task_postrun.send(sender=task, task_id="probe", state="RETRY")
    assert get_backend().ttl(key) == 14400
    assert get_backend().ttl("agent:quiet:cooldown") == -2
    for state in ("SUCCESS", "FAILURE"):
        before_task_publish.send(sender=task_name, headers={"id": "probe"})
        task_postrun.send(sender=task, task_id="probe", state=state)
        assert get_backend().ttl(key) == -2
        assert get_backend().ttl("agent:quiet:cooldown") == 300
        get_backend().delete("agent:quiet:cooldown")
    light_name = "app.tasks.holdings_tasks.sweep_stale_upload_jobs"
    before_task_publish.send(sender=light_name, headers={"id": "light"})
    task_postrun.send(sender=celery_app.tasks[light_name], task_id="light", state="SUCCESS")
    assert get_backend().ttl("agent:quiet:active:light") == -2
    assert get_backend().ttl("agent:quiet:cooldown") == -2


def test_acceptance_10_explicit_beat_declarations() -> None:
    from app import tasks

    def check() -> None:
        mapping = getattr(tasks, "API_QUIET_BEAT_ENTRIES", {})
        assert set(mapping) == set(tasks.celery_app.conf.beat_schedule), (
            "Declare every Beat entry in API_QUIET_BEAT_ENTRIES and obtain owner confirmation in issue Design"
        )
        assert all(type(value) is bool for value in mapping.values())

    check()
    mapping = tasks.API_QUIET_BEAT_ENTRIES
    assert len(mapping) == 48
    assert {key for key, value in mapping.items() if value} == {
        "intel-slot-pre_open",
        "intel-slot-post_close",
        "report-incremental-weekday",
        "report-incremental-weekly",
    }
    schedule = tasks.celery_app.conf.beat_schedule
    schedule["undeclared-probe"] = {"task": "probe", "schedule": 30.0}
    try:
        with pytest.raises(AssertionError, match="Declare every Beat entry"):
            check()
    finally:
        del schedule["undeclared-probe"]


def audit_rows(session: Session) -> list[dict[str, object]]:
    return [
        dict(row)
        for row in session.execute(text("SELECT * FROM api_audit_log ORDER BY id")).mappings()
    ]


def test_acceptance_11_audit_all_statuses(
    app_client: TestClient, db_session: Session, advanced: dict[str, object], clock: list[datetime]
) -> None:
    from app.core.rate_limit import get_backend

    headers = {**bearer(advanced), "X-Forwarded-For": "203.0.113.7", "User-Agent": "test-agent"}
    assert app_client.get(AGENT, params=params(), headers=headers).status_code == 200
    db_session.execute(
        text("UPDATE users SET subscription_type = 'weekly' WHERE id = :id"), {"id": TEST_USER_ID}
    )
    db_session.commit()
    assert app_client.get(AGENT, params=params(), headers=headers).status_code == 403
    get_backend().set_nx(f"agent:lock:{TEST_USER_ID}", 900)
    assert app_client.get(AGENT, params=params(), headers=headers).status_code == 429
    unknown = "pfa_unknown_secret_not_to_persist"
    assert app_client.get(AGENT, headers={"Authorization": f"Bearer {unknown}"}).status_code == 401
    rows = audit_rows(db_session)
    assert [row["status_code"] for row in rows] == [200, 403, 429, 401]
    for row in rows[:3]:
        assert row["user_id"] == TEST_USER_ID and str(row["token_id"]) == advanced["id"]
        assert row["client_ip"] == "203.0.113.7" and row["user_agent"] == "test-agent"
        assert row["params"] == params() and row["endpoint"] == AGENT
    assert rows[0]["item_count"] == 0
    assert rows[1]["item_count"] is rows[2]["item_count"] is None
    assert rows[3]["user_id"] is rows[3]["token_id"] is None
    assert rows[3]["token_prefix"] == unknown[:12]
    assert unknown not in str(rows[3])


def test_acceptance_11_routing_and_validation(
    app_client: TestClient, db_session: Session, advanced: dict[str, object], clock: list[datetime]
) -> None:
    before = db_session.execute(
        text("SELECT last_used_at FROM api_tokens WHERE id = :id"), {"id": advanced["id"]}
    ).scalar_one()
    assert app_client.get("/agent/v1/nope", headers=bearer(advanced)).status_code == 404
    assert app_client.post(AGENT, headers=bearer(advanced)).status_code == 405
    after = db_session.execute(
        text("SELECT last_used_at FROM api_tokens WHERE id = :id"), {"id": advanced["id"]}
    ).scalar_one()
    assert after == before
    assert (
        app_client.get(
            AGENT, params={"start": "bad", "end": params()["end"]}, headers=bearer(advanced)
        ).status_code
        == 422
    )
    rows = audit_rows(db_session)
    assert [r["status_code"] for r in rows] == [404, 405, 422]
    assert [r["endpoint"] for r in rows] == ["/agent/v1/nope", AGENT, AGENT]
    assert all(r["user_id"] == TEST_USER_ID and str(r["token_id"]) == advanced["id"] for r in rows)
    assert rows[-1]["params"] == {"start": "bad", "end": params()["end"]}


def test_audit_rejected_known_tokens_and_500(
    app_client: TestClient, db_session: Session, advanced: dict[str, object], clock: list[datetime]
) -> None:
    with (
        patch("app.routers.agent.export_snapshot_range", side_effect=RuntimeError("test failure")),
        pytest.raises(RuntimeError, match="test failure"),
    ):
        app_client.get(AGENT, params=params(), headers=bearer(advanced))
    assert audit_rows(db_session)[0]["status_code"] == 500
    for column, value in [("expires_at", NOW - timedelta(seconds=1)), ("revoked_at", NOW)]:
        db_session.execute(
            text(f"UPDATE api_tokens SET {column} = :value WHERE id = :id"),
            {"value": value, "id": advanced["id"]},
        )
        db_session.commit()
        assert app_client.get(AGENT, params=params(), headers=bearer(advanced)).status_code == 401
        row = audit_rows(db_session)[-1]
        assert row["user_id"] == TEST_USER_ID and str(row["token_id"]) == advanced["id"]
    long_path = "/agent/v1/" + "x" * 240
    app_client.get(long_path, headers={"User-Agent": "u" * 600})
    row = audit_rows(db_session)[-1]
    assert row["endpoint"] == long_path[:200]
    assert row["user_agent"] == "u" * 512
    assert row["token_prefix"] is None
    count = len(audit_rows(db_session))
    app_client.get("/portfolio/snapshots", params=params())
    assert len(audit_rows(db_session)) == count


def test_acceptance_14_ops_audit_and_revoke(
    app_client: TestClient, db_session: Session, advanced: dict[str, object]
) -> None:
    from uuid import uuid4

    from app.core.config import get_settings
    from app.models.api_audit_log import ApiAuditLog

    other = uuid4()
    seed_user(db_session, other)
    entries = [
        ApiAuditLog(
            user_id=TEST_USER_ID,
            token_id=advanced["id"],
            endpoint=AGENT,
            params={},
            status_code=200,
            client_ip="test",
            occurred_at=NOW + timedelta(seconds=i),
        )
        for i in range(1001)
    ]
    db_session.add_all(
        [
            *entries,
            ApiAuditLog(
                user_id=other,
                endpoint=AGENT,
                params={},
                status_code=403,
                client_ip="other",
                occurred_at=NOW + timedelta(seconds=2000),
            ),
        ]
    )
    db_session.commit()
    headers = {"Authorization": "Bearer " + get_settings().ADMIN_API_TOKEN.get_secret_value()}
    url = f"/admin/users/{TEST_USER_ID}/api-audit"
    response = app_client.get(
        url,
        params={"start": NOW.date().isoformat(), "end": NOW.date().isoformat()},
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["truncated"] is True and len(body["rows"]) == 1000
    assert all(r["user_id"] == str(TEST_USER_ID) for r in body["rows"])
    assert [r["id"] for r in body["rows"]] == [r.id for r in entries[:0:-1]]
    assert set(body["rows"][0]) == {
        "id",
        "occurred_at",
        "user_id",
        "token_id",
        "token_prefix",
        "endpoint",
        "params",
        "status_code",
        "item_count",
        "client_ip",
        "user_agent",
    }
    response = app_client.post(
        f"/admin/users/{TEST_USER_ID}/api-tokens/revoke-all", headers=headers
    )
    assert response.status_code == 200 and response.json() == {"revoked_count": 1}
    assert (
        db_session.execute(
            text("SELECT revoked_by FROM api_tokens WHERE id = :id"), {"id": advanced["id"]}
        ).scalar_one()
        == "ops"
    )
    assert app_client.get(AGENT, params=params(), headers=bearer(advanced)).status_code == 401
    assert (
        app_client.get(
            url, params={"start": "2026-01-01", "end": "2026-02-01"}, headers=headers
        ).status_code
        == 422
    )
    unknown = uuid4()
    assert (
        app_client.get(
            f"/admin/users/{unknown}/api-audit",
            params={"start": "2026-01-01", "end": "2026-01-01"},
            headers=headers,
        ).status_code
        == 404
    )
    assert (
        app_client.post(
            f"/admin/users/{unknown}/api-tokens/revoke-all", headers=headers
        ).status_code
        == 404
    )
    assert app_client.get(url, params=params()).status_code == 401


def test_acceptance_15_purge_tokens_and_audit(
    app_client: TestClient, db_session: Session, advanced: dict[str, object]
) -> None:
    from app.services.user_purge import purge_user

    app_client.get(AGENT, params=params(), headers=bearer(advanced))
    assert len(audit_rows(db_session)) == 1
    result = purge_user(db_session, TEST_USER_ID)
    db_session.commit()
    assert result.api_tokens == result.api_audit_log == 1
    assert (
        db_session.execute(
            text("SELECT count(*) FROM api_tokens WHERE user_id = :id"), {"id": TEST_USER_ID}
        ).scalar_one()
        == 0
    )
    assert audit_rows(db_session) == []


def test_agent_audit_retention_existing_task(db_session: Session, clock: list[datetime]) -> None:
    from app.models.api_audit_log import ApiAuditLog
    from app.services.api_tokens import now_et
    from app.tasks.operational_events_tasks import cleanup_operational_events

    cutoff = now_et() - timedelta(days=90)
    db_session.add_all(
        [
            ApiAuditLog(
                endpoint=AGENT,
                params={},
                status_code=401,
                client_ip="test",
                occurred_at=cutoff - timedelta(seconds=1),
            )
            for _ in range(1001)
        ]
    )
    keep = ApiAuditLog(
        endpoint=AGENT, params={}, status_code=401, client_ip="test", occurred_at=cutoff
    )
    db_session.add(keep)
    db_session.commit()
    result = cleanup_operational_events.run()
    assert result["api_audit_deleted"] == 1001
    rows = audit_rows(db_session)
    assert len(rows) == 1 and rows[0]["id"] == keep.id


def test_acceptance_12_daily_notice(
    app_client: TestClient, advanced: dict[str, object], clock: list[datetime]
) -> None:
    from unittest.mock import MagicMock

    from app.tasks import notification_tasks

    task = getattr(notification_tasks, "send_api_access_notice_task", MagicMock())
    with patch.object(task, "delay") as enqueue:
        clock[0] = NOW.replace(hour=23, minute=59)
        for _ in range(2):
            assert (
                app_client.get(AGENT, params=params(), headers=bearer(advanced)).status_code == 200
            )
        assert enqueue.call_count == 1
        assert enqueue.call_args.args == (str(TEST_USER_ID),)
        clock[0] += timedelta(minutes=2)
        assert app_client.get(AGENT, params=params(), headers=bearer(advanced)).status_code == 200
        assert enqueue.call_count == 2


def signed_link(user_id: object, expires: datetime) -> str:
    import base64
    import hmac

    from app.core.config import get_settings

    payload = f"api-token-revoke-v1:{user_id}:{int(expires.timestamp())}"
    digest = hmac.new(
        get_settings().APP_SECRET_KEY.get_secret_value().encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}.{digest}".encode()).decode().rstrip("=")


def test_acceptance_13_revoke_link(
    app_client: TestClient, advanced: dict[str, object], db_session: Session, clock: list[datetime]
) -> None:
    second = create(app_client, "Second")
    link = signed_link(TEST_USER_ID, NOW + timedelta(days=30))
    url = "/api-tokens/revoke-by-link"
    response = app_client.post(url, json={"token": link})
    assert response.status_code == 200
    assert response.json() == {"revoked_count": 2}
    for body in [advanced, second]:
        assert app_client.get(AGENT, params=params(), headers=bearer(body)).status_code == 401
    assert app_client.post(url, json={"token": link}).json() == {"revoked_count": 0}
    for token in ["x" + link, signed_link(TEST_USER_ID, NOW - timedelta(seconds=1))]:
        response = app_client.post(url, json={"token": token})
        assert response.status_code == 400
        assert response.json() == {"detail": "invalid_link"}
    rows = db_session.execute(text("SELECT revoked_by FROM api_tokens")).scalars().all()
    assert rows == ["email_link", "email_link"]


def test_notice_task_recipient_and_locale(db_session: Session, clock: list[datetime]) -> None:
    from app.tasks import notification_tasks

    task = getattr(notification_tasks, "send_api_access_notice_task", None)
    assert task is not None
    user = seed_user(db_session, TEST_USER_ID)
    user.locale = "zh-Hant"
    db_session.commit()
    with patch("app.tasks.notification_tasks.send_api_access_notice") as send:
        task.run(str(TEST_USER_ID))
        send.assert_not_called()
        user.email_verified_at = NOW
        db_session.commit()
        task.run(str(TEST_USER_ID))
        assert send.call_args.args[0] == user.email
        assert send.call_args.kwargs["locale"] == "zh-Hant"
        assert send.call_args.args[1].startswith("https://portfonia.com/agent/revoke?t=")
        from app.services.api_token_revoke_link import verify_link

        assert verify_link(send.call_args.args[1].split("t=", 1)[1]) == TEST_USER_ID


@pytest.mark.parametrize(
    ("url", "status", "field"),
    [
        ("/agent/v1/%00x", 404, "endpoint"),
        ("/agent/v1/snapshots?start=%00&end=2026-10-01", 422, "params"),
    ],
)
def test_audit_sanitizes_nul(
    app_client: TestClient,
    db_session: Session,
    advanced: dict[str, object],
    url: str,
    status: int,
    field: str,
) -> None:
    response = app_client.get(url, headers=bearer(advanced))
    assert response.status_code == status
    rows = audit_rows(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row["status_code"] == status
    assert row["user_id"] == TEST_USER_ID
    value = (
        cast(dict[str, str], row["params"])["start"]
        if field == "params"
        else cast(str, row["endpoint"])
    )
    assert "\x00" not in value
    assert "\ufffd" in value


def test_audit_sanitizes_nul_user_agent(
    app_client: TestClient, db_session: Session, advanced: dict[str, object]
) -> None:
    headers = {**bearer(advanced), "User-Agent": "probe\x00agent"}
    assert app_client.get("/agent/v1/nope", headers=headers).status_code == 404
    assert audit_rows(db_session)[0]["user_agent"] == "probe\ufffdagent"


def test_acceptance_13_rejects_unsubscribe_token(
    app_client: TestClient, advanced: dict[str, object], clock: list[datetime]
) -> None:
    from app.services.unsubscribe_token import create_token

    token = create_token(
        user_id=TEST_USER_ID, purpose="account_email", email="agent@example.com", now=NOW
    )
    response = app_client.post("/api-tokens/revoke-by-link", json={"token": token})
    assert response.status_code == 400
    assert response.json() == {"detail": "invalid_link"}
    assert app_client.get(AGENT, params=params(), headers=bearer(advanced)).status_code == 200


def test_acceptance_13_rejects_wrong_prefix(
    app_client: TestClient, advanced: dict[str, object], clock: list[datetime]
) -> None:
    from app.services.unsubscribe_token import _encode, _sign

    payload = f"wrong-prefix:{TEST_USER_ID}:{int((NOW + timedelta(days=30)).timestamp())}"
    token = _encode(payload, _sign(payload))
    response = app_client.post("/api-tokens/revoke-by-link", json={"token": token})
    assert response.status_code == 400
    assert response.json() == {"detail": "invalid_link"}
    assert app_client.get(AGENT, params=params(), headers=bearer(advanced)).status_code == 200


@pytest.mark.parametrize("success", [False, True])
def test_notice_task_retries_once_or_succeeds(
    db_session: Session, clock: list[datetime], success: bool
) -> None:

    from app.tasks.notification_tasks import send_api_access_notice_task as task

    user = seed_user(db_session, TEST_USER_ID)
    user.email_verified_at = NOW
    db_session.commit()
    with (
        patch("app.tasks.notification_tasks.send_api_access_notice", return_value=success),
        patch.object(task, "retry", side_effect=Retry()) as retry,
    ):
        if success:
            task.run(str(TEST_USER_ID))
            retry.assert_not_called()
        else:
            with pytest.raises(Retry):
                task.run(str(TEST_USER_ID))
            retry.assert_called_once_with(countdown=300)
    assert task.max_retries == 1


def test_notice_task_final_failure_logs_user_id(
    db_session: Session, clock: list[datetime], caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from app.tasks import notification_tasks

    task = notification_tasks.send_api_access_notice_task
    user = seed_user(db_session, TEST_USER_ID)
    user.email_verified_at = NOW
    db_session.commit()
    logger = logging.getLogger(notification_tasks.__name__)
    logger.disabled = False
    with (
        patch("app.tasks.notification_tasks.send_api_access_notice", return_value=False) as send,
        patch.object(task, "retry", side_effect=Retry()) as retry,
        caplog.at_level(logging.ERROR, logger=logger.name),
    ):
        task.push_request(retries=0)
        try:
            with pytest.raises(Retry):
                task.run(str(TEST_USER_ID))
        finally:
            task.pop_request()
        task.push_request(retries=1)
        try:
            task.run(str(TEST_USER_ID))
        finally:
            task.pop_request()
    assert send.call_count == 2
    retry.assert_called_once_with(countdown=300)
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    assert str(TEST_USER_ID) in errors[0].getMessage()
    assert user.email not in errors[0].getMessage()
    assert "revoke?t=" not in errors[0].getMessage()


@pytest.mark.parametrize("success", [False, True])
def test_notice_sender_reports_delivery_result(success: bool) -> None:
    from app.services.email_sender import send_api_access_notice

    with patch("app.services.email_sender.httpx.Client") as client:
        response = client.return_value.__enter__.return_value.post.return_value
        response.raise_for_status.side_effect = None if success else RuntimeError("provider failed")
        assert send_api_access_notice("agent@example.com", "https://example.com/revoke") is success


def test_notice_sender_logs_failure_cause_without_address(caplog: pytest.LogCaptureFixture) -> None:
    import httpx

    from app.services.email_sender import send_api_access_notice

    logging.getLogger("app.services.email_sender").disabled = False
    request = httpx.Request("POST", "https://api.resend.com/emails")
    error = httpx.HTTPStatusError(
        "agent@example.com rejected", request=request, response=httpx.Response(422, request=request)
    )
    with (
        patch("app.services.email_sender.httpx.Client") as client,
        caplog.at_level(logging.WARNING, logger="app.services.email_sender"),
    ):
        client.return_value.__enter__.return_value.post.return_value.raise_for_status.side_effect = error
        assert send_api_access_notice("agent@example.com", "https://example.com/revoke") is False
    messages = [r.getMessage() for r in caplog.records if r.name == "app.services.email_sender"]
    assert messages == ["API access notice delivery attempt failed: HTTPStatusError status=422"]
    assert "agent@example.com" not in caplog.text
