"""Strict current-holdings API contracts (#754), using real PostgreSQL."""

from datetime import timedelta
from decimal import Decimal
from typing import TypedDict, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import today_et
from app.models.holding import Holding
from app.tests.conftest import TEST_USER_ID, seed_user


class CreateResult(TypedDict):
    version: str
    holding: dict[str, str | None]


URL = "/agent/v1/holdings"
ROW = {"name": "Widget", "currency": "USD", "pricing_mode": "manual"}


@pytest.fixture
def writer(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> dict[str, str]:
    monkeypatch.setattr("app.tasks.capture_tasks.backfill_ohlcv_task.delay", lambda _: None)
    monkeypatch.setattr("app.tasks.capture_tasks.backfill_fund_navs_task.delay", lambda _: None)
    user = seed_user(db_session, TEST_USER_ID)
    user.subscription_status = "active"
    user.subscription_type = "daily"
    user.subscription_expires_on = today_et() + timedelta(days=30)
    db_session.commit()
    monkeypatch.setattr("app.core.agent_limits.quiet_retry_after", lambda _: None)
    monkeypatch.setattr(
        "app.tasks.notification_tasks.send_api_access_notice_task.delay", lambda _: None
    )
    monkeypatch.setattr(
        "app.tasks.notification_tasks.send_api_modification_notice_task.delay", lambda _: None
    )
    token = app_client.post(
        "/me/api-tokens", json={"name": "writer", "permission": "holdings_write"}
    )
    assert token.status_code == 201
    return {"Authorization": f"Bearer {token.json()['token']}", "Idempotency-Key": "create-1"}


def test_get_empty_and_create_precision_reusable_book(
    app_client: TestClient, db_session: Session, writer: dict[str, str]
) -> None:
    empty = app_client.get(URL, headers=writer)
    assert empty.status_code == 200
    assert empty.json() == {"version": "0", "holdings": [], "metadata": []}
    row = {**ROW, "shares": "0.123456789123456789", "watch_tier": "focus", "account": "one"}
    response = app_client.post(URL, headers=writer, json={"version": "0", "holding": row})
    assert response.status_code == 201
    assert response.json()["version"] == "1"
    book = app_client.get(URL, headers=writer).json()
    assert book["holdings"][0]["shares"] == "0.123456789123456789"
    assert book["holdings"][0]["watch_tier"] == "focus"
    assert "position" not in book["holdings"][0]
    assert book["metadata"][0]["position"] == 0
    result = app_client.put(
        URL,
        headers={**writer, "Idempotency-Key": "book-1"},
        json={"version": "1", "holdings": book["holdings"], "delete_ids": []},
    )
    assert result.status_code == 200
    assert result.json() == book
    holding = db_session.get(Holding, UUID(book["holdings"][0]["id"]))
    assert holding is not None
    assert holding.shares == Decimal("0.123456789123456789")


def test_post_response_loss_replays_before_version_check(
    app_client: TestClient, db_session: Session, writer: dict[str, str]
) -> None:
    body = {"version": "0", "holding": ROW}
    first = app_client.post(URL, headers=writer, json=body)
    assert first.status_code == 201
    replay = app_client.post(URL, headers=writer, json=body)
    assert replay.status_code == 201
    assert replay.json() == first.json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert len(db_session.scalars(select(Holding)).all()) == 1
    conflict = app_client.post(
        URL, headers=writer, json={**body, "holding": {**ROW, "name": "Changed"}}
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"


def _create(
    client: TestClient,
    headers: dict[str, str],
    version: str = "0",
    key: str = "create-1",
    **fields: object,
) -> CreateResult:
    response = client.post(
        URL,
        headers={**headers, "Idempotency-Key": key},
        json={"version": version, "holding": {**ROW, **fields}},
    )
    assert response.status_code == 201, response.text
    return cast(CreateResult, response.json())


def test_explicit_deletion_completeness_and_order(
    app_client: TestClient, db_session: Session, writer: dict[str, str]
) -> None:
    a = _create(app_client, writer, account="A", ticker="MSFT")
    b = _create(
        app_client, writer, "1", "b", account="B", ticker="MSFT", shares="0", watch_tier="watch"
    )
    c = _create(app_client, writer, "2", "c")
    book = app_client.get(URL, headers=writer).json()
    assert len({r["id"] for r in book["holdings"]}) == 3
    bad = {"version": "3", "holdings": [a["holding"]], "delete_ids": [c["holding"]["id"]]}
    response = app_client.put(URL, headers={**writer, "Idempotency-Key": "bulk"}, json=bad)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "incomplete_collection"
    assert response.json()["detail"]["missing_ids"] == [b["holding"]["id"]]
    assert app_client.get(URL, headers=writer).json() == book
    good = {**bad, "holdings": [b["holding"], a["holding"]]}
    result = app_client.put(URL, headers={**writer, "Idempotency-Key": "bulk"}, json=good)
    assert result.status_code == 200
    assert result.json()["version"] == "4"
    assert result.json()["holdings"][0]["shares"] == "0"
    assert result.json()["holdings"][0]["watch_tier"] == "watch"
    assert [r["id"] for r in result.json()["holdings"]] == [b["holding"]["id"], a["holding"]["id"]]
    assert [m["position"] for m in result.json()["metadata"]] == [0, 1]


@pytest.mark.parametrize(
    "case,code,status",
    [
        ("empty", "empty_holdings", 422),
        ("duplicate", "duplicate_id", 422),
        ("overlap", "conflicting_id", 422),
        ("foreign", "holding_not_found", 404),
        ("unknown", None, 422),
    ],
)
def test_atomic_invalid_bulk(
    app_client: TestClient,
    db_session: Session,
    writer: dict[str, str],
    case: str,
    code: str | None,
    status: int,
) -> None:
    from uuid import uuid4

    saved = _create(app_client, writer)
    book = app_client.get(URL, headers=writer).json()
    rows = [saved["holding"]]
    deletes: list[str] = []
    if case == "empty":
        rows = []
    if case == "duplicate":
        rows *= 2
    if case == "overlap":
        deletes = [str(saved["holding"]["id"])]
    if case == "foreign":
        deletes = [str(uuid4())]
    if case == "unknown":
        rows[0]["asset_class"] = "STOCK"
    response = app_client.put(
        URL,
        headers={**writer, "Idempotency-Key": "invalid"},
        json={"version": "1", "holdings": rows, "delete_ids": deletes},
    )
    assert response.status_code == status
    if code:
        assert response.json()["detail"]["code"] == code
    assert app_client.get(URL, headers=writer).json() == book


@pytest.mark.parametrize(
    "amount", [1, True, "-1", "+1", "01", "1e-3", "NaN", "Infinity", "1.", ".1"]
)
def test_strict_decimal_and_unknown_structure(
    app_client: TestClient, writer: dict[str, str], amount: object
) -> None:
    response = app_client.post(
        URL, headers=writer, json={"version": "0", "holding": {**ROW, "shares": amount}}
    )
    assert response.status_code == 422
    assert app_client.get(URL, headers=writer).json()["version"] == "0"


def test_patch_null_put_defaults_noop_and_identity_price_reset(
    app_client: TestClient, db_session: Session, writer: dict[str, str]
) -> None:
    saved = _create(
        app_client, writer, ticker="MSFT", notes="keep", shares="0", watch_tier="critical"
    )
    hid = saved["holding"]["id"]
    target = f"{URL}/{hid}"
    before = app_client.get(URL, headers=writer).json()
    no_op = app_client.patch(target, headers=writer, json={"version": "1", "changes": {}})
    assert no_op.status_code == 200 and no_op.json() == saved
    assert app_client.get(URL, headers=writer).json() == before
    changed = app_client.patch(
        target, headers=writer, json={"version": "1", "changes": {"notes": None}}
    )
    assert changed.status_code == 200 and changed.json()["version"] == "2"
    assert changed.json()["holding"]["ticker"] == "MSFT"
    assert changed.json()["holding"]["notes"] is None
    assert (
        app_client.patch(
            target, headers=writer, json={"version": "2", "changes": {"name": None}}
        ).status_code
        == 422
    )
    h = db_session.get(Holding, UUID(str(hid)))
    assert h is not None
    h.market_price = Decimal("100")
    db_session.commit()
    replaced = app_client.put(target, headers=writer, json={"version": "2", "holding": ROW})
    assert replaced.status_code == 200 and replaced.json()["version"] == "3"
    assert replaced.json()["holding"]["ticker"] is None
    assert replaced.json()["holding"]["watch_tier"] is None
    db_session.refresh(h)
    assert h.market_price is None and h.position == 0


@pytest.mark.parametrize("source", ["web", "import"])
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "WHOLE"])
def test_all_writes_conflict_after_web_edit(
    app_client: TestClient, writer: dict[str, str], method: str, source: str
) -> None:
    saved = _create(app_client, writer)
    hid = saved["holding"]["id"]
    if source == "web":
        assert app_client.patch(f"/holdings/{hid}", json={"notes": "browser"}).status_code == 200
    else:
        assert app_client.post("/holdings/confirm?mode=replace", json=[ROW]).status_code == 200
    target = URL if method in ("POST", "WHOLE") else f"{URL}/{hid}"
    body: dict[str, object] = (
        {"version": "1", "changes": {"notes": "agent"}}
        if method == "PATCH"
        else {"version": "1", "holding": ROW}
    )
    headers = (
        {**writer, "Idempotency-Key": "stale", "If-Match": '"1"'}
        if method == "DELETE"
        else {**writer, "Idempotency-Key": "stale"}
    )
    if method == "WHOLE":
        body = {"version": "1", "holdings": [saved["holding"]], "delete_ids": []}
    response = app_client.request(
        "PUT" if method == "WHOLE" else method,
        target,
        headers=headers,
        json=None if method == "DELETE" else body,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "version_conflict"
    assert response.json()["detail"]["current_version"] == "2"


def test_last_row_delete_headers_and_web_clear(
    app_client: TestClient, writer: dict[str, str]
) -> None:
    saved = _create(app_client, writer)
    target = f"{URL}/{saved['holding']['id']}"
    assert app_client.delete(target, headers=writer).status_code == 428
    response = app_client.delete(target, headers={**writer, "If-Match": '"1"'})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "last_holding_requires_ui"
    assert app_client.delete(target, headers={**writer, "If-Match": '"01"'}).status_code == 422
    assert app_client.delete(f"/holdings/{saved['holding']['id']}").status_code == 204
    assert app_client.get(URL, headers=writer).json()["holdings"] == []


@pytest.mark.parametrize("operation", ["create", "replace_all", "replace", "patch"])
def test_validation_normalization_stale_and_no_side_effects(
    app_client: TestClient, db_session: Session, writer: dict[str, str], operation: str
) -> None:
    saved = _create(app_client, writer)
    book = app_client.get(URL, headers=writer).json()
    candidate = {**ROW, "shares": "1.000", "ticker": "700.HK", "currency": "USD"}
    body: dict[str, object] = {"operation": operation, "version": "1"}
    if operation == "replace_all":
        body.update(holdings=[{**candidate, "id": saved["holding"]["id"]}], delete_ids=[])
    elif operation == "patch":
        body.update(holding_id=saved["holding"]["id"], changes={"shares": "1.000"})
    else:
        body["holding"] = candidate
        if operation == "replace":
            body["holding_id"] = saved["holding"]["id"]
    response = app_client.post(f"{URL}/validate", headers=writer, json=body)
    assert response.status_code == 200
    assert response.json()["valid"] is True
    assert response.json()["version"] == "1"
    normalized = response.json()["normalized"]
    row = (
        normalized["holdings"][0]
        if operation == "replace_all"
        else normalized["changes"]
        if operation == "patch"
        else normalized["holding"]
    )
    assert row["shares"] == "1"
    stale = app_client.post(f"{URL}/validate", headers=writer, json={**body, "version": "0"})
    assert stale.status_code == 200 and stale.json()["valid"] is False
    assert stale.json()["errors"][0]["code"] == "version_conflict"
    assert app_client.get(URL, headers=writer).json() == book


def test_modification_email_sender_locales_and_no_rollback_claim() -> None:
    from unittest.mock import patch

    from app.services import email_sender

    for locale, words in [
        ("en", "does not undo"),
        ("zh", "不会撤销"),
        ("zh-Hant", "不會撤銷"),
        ("es", "does not undo"),
    ]:
        with patch("app.services.email_sender.httpx.Client") as mocked:
            assert email_sender.send_api_modification_notice(
                "verified@example.com", "https://example.com/revoke", locale=locale
            )
            sent = mocked.return_value.__enter__.return_value.post.call_args.kwargs["json"]
            assert words in sent["text"]
            assert "https://example.com/revoke" in sent["text"]
            assert "pfa_" not in sent["text"]


def test_retention_sweep_and_account_purge_remove_results(
    app_client: TestClient, db_session: Session, writer: dict[str, str]
) -> None:
    from datetime import timedelta
    from unittest.mock import patch

    from app.models.agent_idempotency import AgentIdempotency
    from app.services import api_tokens
    from app.services.user_purge import purge_user
    from app.tasks.operational_events_tasks import cleanup_operational_events

    _create(app_client, writer)
    now = api_tokens.now_et()
    first = db_session.scalar(select(AgentIdempotency))
    assert first is not None
    first.expires_at = now
    db_session.add_all(
        [
            AgentIdempotency(
                user_id=TEST_USER_ID,
                operation="holding_create",
                key_hash=f"old-{i}",
                request_hash="digest",
                response_status=201,
                response_json="{}",
                created_at=now - timedelta(hours=24),
                expires_at=now,
            )
            for i in range(1000)
        ]
    )
    db_session.commit()
    with patch("app.services.api_tokens.now_et", return_value=now):
        result = cleanup_operational_events.run()
    assert result["agent_idempotency_deleted"] == 1001
    assert db_session.scalars(select(AgentIdempotency)).all() == []
    _create(app_client, writer, "1", "purge")
    purge_user(db_session, TEST_USER_ID)
    db_session.commit()
    assert db_session.scalars(select(AgentIdempotency)).all() == []


def test_bulk_replay_cross_token_expiry_and_encrypted_storage(
    app_client: TestClient,
    db_session: Session,
    writer: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import text

    from app.models.agent_idempotency import AgentIdempotency
    from app.services import api_tokens

    first = _create(app_client, writer, shares="1.00")
    decimal_replay = app_client.post(
        URL, headers=writer, json={"version": "0", "holding": {**ROW, "shares": "1"}}
    )
    assert decimal_replay.status_code == 201 and decimal_replay.json() == first
    body = {
        "version": "1",
        "holdings": [first["holding"], {**ROW, "name": "Secret candidate"}],
        "delete_ids": [],
    }
    headers = {**writer, "Idempotency-Key": "whole"}
    bulk = app_client.put(URL, headers=headers, json=body)
    assert bulk.status_code == 200 and bulk.json()["version"] == "2"
    token = app_client.post(
        "/me/api-tokens", json={"name": "other", "permission": "holdings_write"}
    ).json()
    replay = app_client.put(
        URL, headers={**headers, "Authorization": f"Bearer {token['token']}"}, json=body
    )
    assert replay.status_code == 200 and replay.json() == bulk.json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert len(db_session.scalars(select(Holding)).all()) == 2
    ciphertext = db_session.execute(
        text("SELECT response_json FROM agent_idempotency WHERE operation='holdings_replace'")
    ).scalar_one()
    assert (
        "Secret candidate" not in ciphertext and bulk.json()["holdings"][1]["id"] not in ciphertext
    )
    stored = db_session.scalar(
        select(AgentIdempotency).where(AgentIdempotency.operation == "holding_create")
    )
    assert stored is not None
    assert (stored.expires_at - stored.created_at).total_seconds() == 86400
    expires = stored.expires_at
    monkeypatch.setattr(api_tokens, "now_et", lambda: expires)
    reused = _create(app_client, writer, "2", "create-1", name="Reused at expiry")
    assert reused["version"] == "3" and reused["holding"]["id"] != first["holding"]["id"]


@pytest.mark.parametrize(
    "state,status",
    [("revoked", 401), ("expired_token", 401), ("expired_subscription", 403), ("inactive", 401)],
)
def test_replay_rechecks_current_auth_and_entitlement(
    app_client: TestClient, db_session: Session, writer: dict[str, str], state: str, status: int
) -> None:
    from app.models.api_token import ApiToken
    from app.models.user import User
    from app.services import api_tokens

    _create(app_client, writer)
    token = db_session.scalar(select(ApiToken))
    user = db_session.get(User, TEST_USER_ID)
    assert token is not None and user is not None
    if state == "revoked":
        token.revoked_at = api_tokens.now_et()
    if state == "expired_token":
        token.expires_at = api_tokens.now_et()
    if state == "expired_subscription":
        user.subscription_status = "expired"
    if state == "inactive":
        user.status = "deleted"
    db_session.commit()
    assert (
        app_client.post(URL, headers=writer, json={"version": "0", "holding": ROW}).status_code
        == status
    )


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", ""),
        ("POST", ""),
        ("PUT", ""),
        ("POST", "/validate"),
        ("PUT", "/00000000-0000-0000-0000-000000000099"),
        ("PATCH", "/00000000-0000-0000-0000-000000000099"),
        ("DELETE", "/00000000-0000-0000-0000-000000000099"),
    ],
)
def test_holdings_routes_require_agent_auth(app_client: TestClient, method: str, path: str) -> None:
    for headers in [{}, {"Authorization": "Bearer browser-session-jwt"}]:
        assert app_client.request(method, URL + path, headers=headers).status_code == 401


def test_read_only_validate_basic_denied_and_route_limit_profiles(
    app_client: TestClient, db_session: Session, writer: dict[str, str]
) -> None:
    from app.core.rate_limit import get_backend
    from app.models.user import User

    token = app_client.post("/me/api-tokens", json={"name": "reader"}).json()
    headers = {"Authorization": f"Bearer {token['token']}"}
    assert app_client.get(URL, headers=headers).status_code == 200
    body = {"operation": "create", "version": "0", "holding": ROW}
    assert app_client.post(URL + "/validate", headers=headers, json=body).json()["valid"] is True
    assert (
        app_client.post(URL, headers=headers, json={"version": "0", "holding": ROW}).json()[
            "detail"
        ]["code"]
        == "holdings_write_required"
    )
    # Reads and validation increment the old bucket, but not single-write counters.
    store = get_backend()
    assert store.ttl(f"agent:burst:{TEST_USER_ID}") > 0
    assert store.ttl(f"agent:hour:{URL}/validate:{TEST_USER_ID}") > 0
    assert store.ttl(f"agent:single:minute:{TEST_USER_ID}") > 0  # denied POST still counts
    single_before = store.incr_with_ttl(f"agent:single:minute:{TEST_USER_ID}", 60)
    bulk = app_client.put(
        URL, headers=writer, json={"version": "0", "holdings": [ROW], "delete_ids": []}
    )
    assert bulk.status_code == 200
    assert store.incr_with_ttl(f"agent:single:minute:{TEST_USER_ID}", 60) == single_before + 1
    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    user.subscription_status = "expired"
    db_session.commit()
    for method, path in [("GET", URL), ("POST", URL + "/validate")]:
        result = app_client.request(
            method, path, headers=headers, json=body if method == "POST" else None
        )
        assert (
            result.status_code == 403 and result.json()["detail"]["code"] == "subscription_required"
        )


def test_invalid_later_row_and_account_failure_rollback(
    app_client: TestClient,
    db_session: Session,
    writer: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.account import Account
    from app.models.agent_idempotency import AgentIdempotency
    from app.services import holdings_write
    from app.services.accounts import resolve_accounts_for_holdings

    saved = _create(app_client, writer)
    book = app_client.get(URL, headers=writer).json()
    body = {
        "version": "1",
        "holdings": [
            {**saved["holding"], "notes": "preceding edit"},
            {**ROW, "name": "Bad cash", "asset_type": "cash"},
        ],
        "delete_ids": [],
    }
    result = app_client.put(URL, headers={**writer, "Idempotency-Key": "failed"}, json=body)
    assert result.status_code == 422 and result.json()["detail"]["code"] == "invalid_holding"
    assert app_client.get(URL, headers=writer).json() == book

    def fail_after_account(
        session: Session,
        uid: UUID,
        rows: list[tuple[str | None, str | None, str | None]],
        *,
        archive_unreferenced: bool,
    ) -> list[UUID | None]:
        resolve_accounts_for_holdings(session, uid, rows, archive_unreferenced=archive_unreferenced)
        raise RuntimeError("simulated account failure")

    monkeypatch.setattr(holdings_write, "resolve_accounts_for_holdings", fail_after_account)
    cast(list[dict[str, object]], body["holdings"])[1] = {**ROW, "broker": "new account"}
    with pytest.raises(RuntimeError, match="simulated account failure"):
        app_client.put(URL, headers={**writer, "Idempotency-Key": "failed"}, json=body)
    assert db_session.scalars(select(Account)).all() == []
    assert len(db_session.scalars(select(AgentIdempotency)).all()) == 1
    assert app_client.get(URL, headers=writer).json() == book


def test_shared_cap_conversion_downgrade_edits_and_validation(
    app_client: TestClient, db_session: Session, writer: dict[str, str]
) -> None:
    from app.tests.test_agent_foundation import _add_rows

    _add_rows(db_session, 50)
    db_session.commit()
    result = app_client.post(URL, headers=writer, json={"version": "0", "holding": ROW})
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "holding_limit"
    assert result.json()["detail"]["limit"] == 50 and result.json()["detail"]["current_count"] == 50
    validation = app_client.post(
        URL + "/validate",
        headers=writer,
        json={"operation": "create", "version": "0", "holding": ROW},
    )
    assert (
        validation.status_code == 200 and validation.json()["errors"][0]["code"] == "holding_limit"
    )
    # Existing web payload stays exactly the pre-Agent shape.
    web = app_client.post("/holdings", json={**ROW, "shares": 1})
    assert web.status_code == 409 and web.json() == {"detail": "holding_cap"}


def test_notices_shared_read_distinct_modification_noop_replay_and_failures(
    app_client: TestClient,
    db_session: Session,
    writer: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from app.core.rate_limit import RateLimitUnavailable
    from app.models.api_audit_log import ApiAuditLog
    from app.services import api_tokens

    read = Mock()
    modified = Mock()
    monkeypatch.setattr("app.tasks.notification_tasks.send_api_access_notice_task.delay", read)
    monkeypatch.setattr(
        "app.tasks.notification_tasks.send_api_modification_notice_task.delay", modified
    )
    assert app_client.get(URL, headers=writer).status_code == 200
    date = api_tokens.now_et().date().isoformat()
    assert (
        app_client.get(
            "/agent/v1/snapshots", headers=writer, params={"start": date, "end": date}
        ).status_code
        == 200
    )
    assert read.call_count == 1
    saved = _create(app_client, writer)
    assert modified.call_count == 1
    assert (
        app_client.patch(
            f"{URL}/{saved['holding']['id']}", headers=writer, json={"version": "1", "changes": {}}
        ).json()["version"]
        == "1"
    )
    assert (
        app_client.post(URL, headers=writer, json={"version": "0", "holding": ROW}).json() == saved
    )
    assert modified.call_count == 1
    assert (
        app_client.post(
            URL + "/validate",
            headers=writer,
            json={"operation": "create", "version": "1", "holding": ROW},
        ).status_code
        == 200
    )
    assert modified.call_count == 1 and read.call_count == 1
    from app.core.rate_limit import get_backend

    monkeypatch.setattr(get_backend(), "set_nx", Mock(side_effect=RateLimitUnavailable()))
    second = _create(app_client, writer, "1", "failure", name="Private second")
    assert second["version"] == "2"
    replay = app_client.post(
        URL,
        headers={**writer, "Idempotency-Key": "failure"},
        json={"version": "1", "holding": {**ROW, "name": "Private second"}},
    )
    assert replay.status_code == 201 and replay.json() == second
    audits = db_session.scalars(select(ApiAuditLog)).all()
    assert audits[-1].status_code == 201 and audits[-1].item_count == 1
    assert all(
        "failure" not in str(a.params) and "Private second" not in str(a.params) for a in audits
    )


@pytest.mark.parametrize("whole", [False, True])
def test_real_postgres_concurrent_same_key_replay(whole: bool, session_test_db: None) -> None:
    import threading
    from uuid import uuid4

    from sqlalchemy import delete

    from app.core.database import SessionLocal
    from app.models.account import Account
    from app.models.agent_idempotency import AgentIdempotency
    from app.models.user import User
    from app.schemas.agent_holdings import Create, ReplaceAll
    from app.services.agent_holdings import mutate

    uid = uuid4()
    with SessionLocal() as setup:
        user = seed_user(setup, uid)
        user.subscription_status = "active"
        user.subscription_type = "daily"
        setup.commit()
    body = (
        ReplaceAll.model_validate({"version": "0", "holdings": [ROW], "delete_ids": []})
        if whole
        else Create.model_validate({"version": "0", "holding": ROW})
    )
    barrier = threading.Barrier(2)
    results: list[tuple[int, dict[str, object], bool, bool]] = []
    failures: list[Exception] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            with SessionLocal() as session:
                results.append(mutate(session, uid, "holdings_write", body, key="same"))
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    try:
        assert not any(t.is_alive() for t in threads) and not failures
        assert len(results) == 2
        assert results[0][:2] == results[1][:2]
        assert sorted(result[2] for result in results) == [False, True]
        with SessionLocal() as check:
            assert len(check.scalars(select(Holding).where(Holding.user_id == uid)).all()) == 1
            assert (
                len(
                    check.scalars(
                        select(AgentIdempotency).where(AgentIdempotency.user_id == uid)
                    ).all()
                )
                == 1
            )
            checked_user = check.get(User, uid)
            assert checked_user is not None and checked_user.holdings_version == 1
    finally:
        with SessionLocal() as cleanup:
            cleanup.execute(delete(Holding).where(Holding.user_id == uid))
            cleanup.execute(delete(Account).where(Account.user_id == uid))
            cleanup.execute(delete(User).where(User.id == uid))
            cleanup.commit()


def test_real_postgres_concurrent_delete_preserves_last_row(session_test_db: None) -> None:
    import threading
    from uuid import uuid4

    from fastapi import HTTPException
    from sqlalchemy import delete

    from app.core.database import SessionLocal
    from app.models.user import User
    from app.services.agent_holdings import mutate

    uid = uuid4()
    with SessionLocal() as setup:
        user = seed_user(setup, uid)
        user.subscription_status = "active"
        user.subscription_type = "daily"
        rows = [
            Holding(user_id=uid, name=f"row-{i}", pricing_mode="manual", currency="USD", position=i)
            for i in range(2)
        ]
        setup.add_all(rows)
        setup.commit()
        ids = [r.id for r in rows]
    barrier = threading.Barrier(2)
    codes: list[int] = []

    def worker(hid: UUID) -> None:
        barrier.wait(timeout=5)
        with SessionLocal() as session:
            try:
                status, _, _, _ = mutate(session, uid, "holdings_write", None, hid=hid, version="0")
                codes.append(status)
            except HTTPException as exc:
                codes.append(exc.status_code)

    threads = [threading.Thread(target=worker, args=(hid,)) for hid in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    try:
        assert not any(t.is_alive() for t in threads)
        assert sorted(codes) == [200, 409]
        with SessionLocal() as check:
            remaining = check.scalars(select(Holding).where(Holding.user_id == uid)).all()
            assert len(remaining) == 1
            with pytest.raises(HTTPException) as refused:
                mutate(check, uid, "holdings_write", None, hid=remaining[0].id, version="1")
            assert isinstance(refused.value.detail, dict)
            assert refused.value.detail["code"] == "last_holding_requires_ui"
    finally:
        with SessionLocal() as cleanup:
            cleanup.execute(delete(Holding).where(Holding.user_id == uid))
            cleanup.execute(delete(User).where(User.id == uid))
            cleanup.commit()


def test_real_postgres_get_waits_for_writer_coherent_version(session_test_db: None) -> None:
    import threading
    from uuid import uuid4

    from sqlalchemy import delete

    from app.core.database import SessionLocal
    from app.models.user import User
    from app.services.agent_holdings import get_book
    from app.services.holdings_write import lock_user_book

    uid = uuid4()
    with SessionLocal() as setup:
        user = seed_user(setup, uid)
        user.subscription_status = "active"
        user.subscription_type = "daily"
        setup.add(Holding(user_id=uid, name="old", pricing_mode="manual", currency="USD"))
        setup.commit()
    entered, finished = threading.Event(), threading.Event()
    result: list[dict[str, object]] = []

    def read() -> None:
        with SessionLocal() as session:
            entered.set()
            result.append(get_book(session, uid))
            finished.set()

    try:
        with SessionLocal() as writer_session:
            user, rows = lock_user_book(writer_session, uid)
            user.holdings_version = 1
            rows[0].name = "new"
            writer_session.flush()
            thread = threading.Thread(target=read)
            thread.start()
            assert entered.wait(timeout=5)
            assert not finished.wait(timeout=0.1)
            writer_session.commit()
        thread.join(timeout=10)
        assert finished.is_set()
        assert (
            result[0]["version"] == "1"
            and cast(list[dict[str, object]], result[0]["holdings"])[0]["name"] == "new"
        )
    finally:
        with SessionLocal() as cleanup:
            cleanup.execute(delete(Holding).where(Holding.user_id == uid))
            cleanup.execute(delete(User).where(User.id == uid))
            cleanup.commit()


def test_empty_patch_is_noop_even_for_legacy_normalization(
    app_client: TestClient, db_session: Session, writer: dict[str, str]
) -> None:
    row = Holding(
        user_id=TEST_USER_ID,
        name="Legacy",
        ticker="700.HK",
        pricing_mode="auto",
        currency="USD",
        market="HK",
        asset_class="STOCK",
        capture_supported=False,
        position=5,
    )
    db_session.add(row)
    db_session.commit()
    before = app_client.get(URL, headers=writer).json()
    result = app_client.patch(
        f"{URL}/{row.id}", headers=writer, json={"version": "0", "changes": {}}
    )
    assert result.status_code == 200 and result.json()["version"] == "0"
    assert app_client.get(URL, headers=writer).json() == before


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "WHOLE"])
def test_read_only_cannot_use_any_mutation(
    app_client: TestClient, writer: dict[str, str], method: str
) -> None:
    token = app_client.post("/me/api-tokens", json={"name": "read-only"}).json()
    headers = {"Authorization": f"Bearer {token['token']}"}
    target = URL if method in ("POST", "WHOLE") else URL + "/00000000-0000-0000-0000-000000000001"
    result = app_client.request("PUT" if method == "WHOLE" else method, target, headers=headers)
    assert result.status_code == 403
    assert result.json()["detail"]["code"] == "holdings_write_required"


@pytest.mark.parametrize("key", [None, "", "white space", "é", "a" * 129])
def test_idempotency_key_boundary_rejection(
    app_client: TestClient, writer: dict[str, str], key: str | None
) -> None:
    headers = {k: v for k, v in writer.items() if k != "Idempotency-Key"}
    # Non-ASCII cannot be transmitted by httpx headers; use the pure boundary.
    if key == "é":
        from fastapi import HTTPException

        from app.services.agent_idempotency import key_hash

        with pytest.raises(HTTPException) as rejected:
            key_hash(key)
        assert rejected.value.status_code == 422
        return
    if key is not None:
        headers["Idempotency-Key"] = key
    result = app_client.post(URL, headers=headers, json={"version": "0", "holding": ROW})
    assert result.status_code == 422
    assert result.json()["detail"]["code"] == "invalid_idempotency_key"


def test_write_and_version_rollback_when_replay_save_fails(
    app_client: TestClient,
    db_session: Session,
    writer: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.account import Account
    from app.models.agent_idempotency import AgentIdempotency
    from app.services import agent_idempotency

    saved = _create(app_client, writer)
    original = app_client.get(URL, headers=writer).json()
    body = {
        "version": "1",
        "holdings": [
            {**saved["holding"], "notes": "must roll back"},
            {**ROW, "broker": "new broker"},
        ],
        "delete_ids": [],
    }
    save = agent_idempotency.save

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated replay-save failure")

    monkeypatch.setattr(agent_idempotency, "save", broken)
    with pytest.raises(RuntimeError, match="simulated replay-save failure"):
        app_client.put(URL, headers={**writer, "Idempotency-Key": "bulk-failure"}, json=body)
    assert app_client.get(URL, headers=writer).json() == original
    assert db_session.scalars(select(Account)).all() == []
    assert len(db_session.scalars(select(AgentIdempotency)).all()) == 1
    monkeypatch.setattr(agent_idempotency, "save", save)
    success = app_client.put(URL, headers={**writer, "Idempotency-Key": "bulk-failure"}, json=body)
    assert success.status_code == 200 and success.json()["version"] == "2"
    assert len(success.json()["holdings"]) == 2


def test_modification_task_retries_and_uses_verified_recipient(db_session: Session) -> None:
    from unittest.mock import patch

    from celery.exceptions import Retry  # type: ignore[import-untyped]

    from app.tasks.notification_tasks import send_api_modification_notice_task

    user = seed_user(db_session, TEST_USER_ID)
    user.locale = "zh-Hant"
    db_session.commit()
    with (
        patch(
            "app.services.user_directory.recipient_email_with_purpose",
            return_value=("verified@example.com", "delivery"),
        ),
        patch("app.services.email_sender.send_api_modification_notice", return_value=False) as send,
        patch.object(send_api_modification_notice_task, "retry", side_effect=Retry()) as retry,
    ):
        with pytest.raises(Retry):
            send_api_modification_notice_task.run(str(TEST_USER_ID))
        retry.assert_called_once_with(countdown=300)
        assert send.call_args.args[0] == "verified@example.com"
        assert send.call_args.kwargs["locale"] == "zh-Hant"
        assert "/agent/revoke?t=" in send.call_args.args[1]


def test_malformed_json_obeys_auth_limits_and_entitlement(
    app_client: TestClient, db_session: Session, writer: dict[str, str]
) -> None:
    from app.core.rate_limit import get_backend
    from app.models.user import User

    missing = app_client.post(URL, content="{broken", headers={"Content-Type": "application/json"})
    assert missing.status_code == 401
    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    user.subscription_status = "expired"
    db_session.commit()
    denied = app_client.post(
        URL + "/validate", content="{broken", headers={**writer, "Content-Type": "application/json"}
    )
    assert denied.status_code == 403
    get_backend().set_ttl(f"agent:lock:{TEST_USER_ID}", 900)
    locked = app_client.post(
        URL + "/validate", content="{broken", headers={**writer, "Content-Type": "application/json"}
    )
    assert locked.status_code == 429
