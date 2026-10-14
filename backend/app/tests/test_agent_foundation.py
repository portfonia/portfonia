"""Issue #753: token permission, holding caps, collection version, rate profiles."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest
from alembic.config import Config
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.core.agent_limits import enforce_agent_limits
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.rate_limit import InMemoryBackend, RateLimitUnavailable, get_backend
from app.core.timezones import ET
from app.models.account import Account
from app.models.api_token import ApiToken
from app.models.holding import Holding
from app.models.user import User
from app.schemas.holdings import ParsedRow
from app.services.holdings_write import create_holding
from app.services.price_fetcher import update_holding_prices
from app.tests.conftest import TEST_USER_ID, seed_user

NOW = datetime(2026, 10, 13, 18, tzinfo=ET)
QUIET = datetime(2026, 10, 13, 16, 56, tzinfo=ET)
_ROW: dict[str, object] = {
    "name": "Widget",
    "ticker": None,
    "fund_code": None,
    "currency": "USD",
    "shares": 1,
    "avg_cost": 1,
    "current_value": None,
    "pricing_mode": "manual",
    "asset_type": "other",
    "broker": None,
    "account": None,
    "portfolio": None,
    "notes": None,
    "issues": [],
    "confidence": 1.0,
}


def _parsed(**overrides: object) -> ParsedRow:
    return ParsedRow.model_validate({**_ROW, **overrides})


def _add_rows(session: Session, count: int, **fields: object) -> None:
    for index in range(count):
        session.add(
            Holding(
                user_id=TEST_USER_ID,
                name=f"Row {index}",
                pricing_mode="manual",
                currency="USD",
                asset_type="other",
                asset_class="STOCK",
                shares=Decimal("1"),
                position=index,
                market="Other",
                capture_supported=False,
                **fields,
            )
        )


def _version(session: Session) -> int:
    session.expire_all()
    user = session.get(User, TEST_USER_ID)
    assert user is not None
    return int(user.holdings_version)


def _count(session: Session) -> int:
    session.expire_all()
    return int(
        session.scalar(
            select(func.count()).select_from(Holding).where(Holding.user_id == TEST_USER_ID)
        )
        or 0
    )


def test_permission_is_chosen_at_creation_and_not_mutable(
    app_client: TestClient, db_session: Session
) -> None:
    seed_user(db_session, TEST_USER_ID)
    omitted = app_client.post("/me/api-tokens", json={"name": "plain"})
    assert omitted.status_code == 201
    assert omitted.json()["permission"] == "read_only"
    chosen = app_client.post(
        "/me/api-tokens", json={"name": "writer", "permission": "holdings_write"}
    )
    assert chosen.status_code == 201
    assert chosen.json()["permission"] == "holdings_write"
    listed = {row["name"]: row["permission"] for row in app_client.get("/me/api-tokens").json()}
    assert listed == {"writer": "holdings_write", "plain": "read_only"}
    assert (
        app_client.post("/me/api-tokens", json={"name": "bad", "permission": "admin"}).status_code
        == 422
    )
    token_id = chosen.json()["id"]
    assert (
        app_client.patch(f"/me/api-tokens/{token_id}", json={"permission": "read_only"}).status_code
        == 405
    )
    stored = db_session.get(ApiToken, uuid.UUID(token_id))
    assert stored is not None
    assert stored.permission == "holdings_write"


def test_plan_caps_count_every_row_and_keep_over_limit_books(
    app_client: TestClient, db_session: Session
) -> None:
    seed_user(db_session, TEST_USER_ID)
    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    _add_rows(db_session, 28)
    db_session.add(
        Holding(
            user_id=TEST_USER_ID,
            name="Cash",
            pricing_mode="manual",
            currency="USD",
            asset_type="cash",
            asset_class="CASH_EQUIV",
            current_value=Decimal("3"),
            position=28,
            market="Other",
            capture_supported=False,
        )
    )
    db_session.add(
        Holding(
            user_id=TEST_USER_ID,
            name="Watch",
            pricing_mode="manual",
            currency="USD",
            asset_type="other",
            asset_class="STOCK",
            shares=Decimal("1"),
            watch_tier="watch",
            position=29,
            market="Other",
            capture_supported=False,
        )
    )
    db_session.commit()
    assert _count(db_session) == 30
    blocked = app_client.post("/holdings", json=_ROW)
    assert blocked.status_code == 409
    assert blocked.json() == {"detail": "holding_cap"}
    assert _count(db_session) == 30

    user.subscription_status = "active"
    user.subscription_type = "daily"
    user.subscription_cancel_pending = True
    db_session.commit()
    assert app_client.post("/holdings", json=_ROW).status_code == 201
    user.subscription_status = "expired"
    user.subscription_cancel_pending = False
    db_session.commit()
    assert app_client.post("/holdings", json=_ROW).status_code == 409

    user.subscription_status = "active"
    user.subscription_type = "jade"
    db_session.commit()
    _add_rows(db_session, 68)
    db_session.commit()
    assert _count(db_session) == 99
    assert app_client.post("/holdings", json=_ROW).status_code == 201
    assert app_client.post("/holdings", json=_ROW).status_code == 409

    user.subscription_status = "inactive"
    user.subscription_type = None
    db_session.commit()
    listed = app_client.get("/holdings").json()
    target = listed[0]
    assert app_client.patch(f"/holdings/{target['id']}", json={"notes": "kept"}).status_code == 200
    version_before_replace = _version(db_session)
    rejected = app_client.post(
        "/holdings/confirm?mode=replace",
        json=[{**_ROW, "name": f"New {index}"} for index in range(40)],
    )
    assert rejected.status_code == 409
    assert _count(db_session) == 100
    assert _version(db_session) == version_before_replace
    cleared = app_client.post("/holdings/confirm?mode=replace", json=[])
    assert cleared.status_code == 200
    assert cleared.json() == []
    one = app_client.post("/holdings", json=_ROW)
    assert one.status_code == 201
    assert app_client.delete(f"/holdings/{one.json()['id']}").status_code == 204
    assert app_client.get("/holdings").json() == []


@pytest.mark.parametrize(
    "patch_body",
    [{"name": None}, {"currency": None}, {"pricing_mode": None}],
)
def test_patch_rejects_invalid_merged_row_without_changes(
    app_client: TestClient, db_session: Session, patch_body: dict[str, object]
) -> None:
    seed_user(db_session, TEST_USER_ID)
    created = app_client.post("/holdings", json=_ROW)
    assert created.status_code == 201
    before = app_client.get("/holdings").json()
    version_before = _version(db_session)
    rejected = app_client.patch(f"/holdings/{created.json()['id']}", json=patch_body)
    assert rejected.status_code == 422, rejected.text
    assert app_client.get("/holdings").json() == before
    assert _version(db_session) == version_before


@pytest.mark.parametrize("subscription_type, cap", [("daily", 50), ("weekly", 30), ("mwf", 30)])
def test_active_subscription_holding_cap_boundary(
    app_client: TestClient, db_session: Session, subscription_type: str, cap: int
) -> None:
    seed_user(db_session, TEST_USER_ID)
    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    user.subscription_status = "active"
    user.subscription_type = subscription_type
    _add_rows(db_session, cap - 1)
    db_session.commit()
    assert app_client.post("/holdings", json=_ROW).status_code == 201
    assert _count(db_session) == cap
    version_before = _version(db_session)
    rejected = app_client.post("/holdings", json=_ROW)
    assert rejected.status_code == 409
    assert rejected.json() == {"detail": "holding_cap"}
    assert _count(db_session) == cap
    assert _version(db_session) == version_before


def test_collection_version_bumps_once_per_successful_write(
    app_client: TestClient, db_session: Session
) -> None:
    seed_user(db_session, TEST_USER_ID)
    created = app_client.post("/holdings", json={**_ROW, "notes": "first"})
    assert created.status_code == 201
    holding_id = created.json()["id"]
    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    user.holdings_version = 7
    db_session.commit()

    edited = app_client.patch(f"/holdings/{holding_id}", json={"shares": "2"})
    assert edited.status_code == 200
    assert _version(db_session) == 8
    same = app_client.patch(f"/holdings/{holding_id}", json={"shares": "2.0"})
    assert same.status_code == 200
    assert _version(db_session) == 9

    db_session.expire_all()
    holding = db_session.get(Holding, uuid.UUID(holding_id))
    assert holding is not None
    holding.ticker = "AAPL"
    holding.pricing_mode = "auto"
    holding.asset_type = "stock"
    holding.market = "US"
    holding.capture_supported = True
    db_session.commit()
    as_of = datetime(2026, 10, 13, 20, tzinfo=UTC)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "app.services.price_fetcher.fetch_last_close",
            lambda _tickers: {"AAPL": (191.25, as_of)},
        )
        update_holding_prices(db_session)
    db_session.commit()
    assert _version(db_session) == 9
    db_session.expire_all()
    priced = db_session.get(Holding, uuid.UUID(holding_id))
    assert priced is not None
    assert priced.market_price == Decimal("191.25")

    second = app_client.post("/holdings", json={**_ROW, "name": "Second"})
    assert second.status_code == 201
    assert _version(db_session) == 10
    order = [second.json()["id"], holding_id]
    assert app_client.patch("/holdings/reorder", json={"ids": order}).status_code == 200
    assert _version(db_session) == 11
    assert app_client.patch("/holdings/reorder", json={"ids": order}).status_code == 200
    assert _version(db_session) == 12
    appended = app_client.post(
        "/holdings/confirm?mode=append",
        json=[{**_ROW, "name": "A"}, {**_ROW, "name": "B"}],
    )
    assert appended.status_code == 200
    assert _version(db_session) == 13
    replaced = app_client.post("/holdings/confirm?mode=replace", json=[_ROW])
    assert replaced.status_code == 200
    assert _version(db_session) == 14
    assert app_client.delete(f"/holdings/{replaced.json()[0]['id']}").status_code == 204
    assert _version(db_session) == 15

    tiny = app_client.post("/holdings", json=_ROW)
    assert (
        app_client.patch(
            f"/holdings/{tiny.json()['id']}",
            json={"shares": "0.00000000000000000017"},
        ).status_code
        == 200
    )
    db_session.expire_all()
    stored = db_session.get(Holding, uuid.UUID(tiny.json()["id"]))
    assert stored is not None
    assert stored.shares == Decimal("0.00000000000000000017")


def test_single_row_limits_are_separate_from_existing_reads() -> None:
    store = cast(InMemoryBackend, get_backend())
    minute_user = "minute-user"
    for _ in range(30):
        enforce_agent_limits(minute_user, "POST", "/agent/v1/holdings", NOW)
    assert f"agent:burst:{minute_user}" not in store.stored_keys()
    for _ in range(10):
        enforce_agent_limits(minute_user, "GET", "/agent/v1/snapshots", NOW)
    with pytest.raises(HTTPException) as locked:
        enforce_agent_limits(minute_user, "POST", "/agent/v1/holdings", NOW)
    assert locked.value.status_code == 429
    assert locked.value.headers is not None
    assert locked.value.headers["Retry-After"] == "900"
    with pytest.raises(HTTPException) as blocked:
        enforce_agent_limits(minute_user, "GET", "/agent/v1/snapshots", NOW)
    assert blocked.value.status_code == 429

    hour_user = "hour-user"
    for batch in (30, 30, 30, 10):
        for _ in range(batch):
            enforce_agent_limits(hour_user, "PATCH", "/agent/v1/holdings/{holding_id}", NOW)
        store.advance(60)
    with pytest.raises(HTTPException) as hourly:
        enforce_agent_limits(hour_user, "DELETE", "/agent/v1/holdings/{holding_id}", NOW)
    assert hourly.value.status_code == 429
    assert hourly.value.headers is not None
    assert int(hourly.value.headers["Retry-After"]) > 0
    assert store.ttl(f"agent:lock:{hour_user}") < 0

    whole_user = "whole-user"
    for _ in range(10):
        enforce_agent_limits(whole_user, "PUT", "/agent/v1/holdings", NOW)
    with pytest.raises(HTTPException) as whole:
        enforce_agent_limits(whole_user, "PUT", "/agent/v1/holdings", NOW)
    assert whole.value.status_code == 429
    assert whole.value.headers is not None
    assert whole.value.headers["Retry-After"] == "900"

    read_user = "read-holdings"
    for _ in range(10):
        enforce_agent_limits(read_user, "GET", "/agent/v1/holdings", NOW)
    with pytest.raises(HTTPException) as read_cap:
        enforce_agent_limits(read_user, "GET", "/agent/v1/holdings", NOW)
    assert read_cap.value.status_code == 429
    assert read_cap.value.headers is not None
    assert read_cap.value.headers["Retry-After"] == "900"

    with pytest.raises(HTTPException) as quiet:
        enforce_agent_limits("quiet-user", "GET", "/agent/v1/snapshots", QUIET)
    assert quiet.value.status_code == 429
    assert quiet.value.headers is not None
    assert int(quiet.value.headers["Retry-After"]) > 0

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store, "ttl", lambda _key: (_ for _ in ()).throw(RateLimitUnavailable))
        with pytest.raises(HTTPException) as down:
            enforce_agent_limits("redis-user", "GET", "/agent/v1/snapshots", NOW)
    assert down.value.status_code == 503


def test_migration_backfills_permission_and_version(alembic_cfg: Config) -> None:
    command.upgrade(alembic_cfg, "d72900000001")
    engine = create_engine(get_settings().database_url)
    uid = uuid.uuid4()
    token_hash = uuid.uuid4().hex
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (id, auth_provider, email, status, locale, "
                    "base_currency, report_cadence) VALUES "
                    "(:id, 'supabase', :email, 'active', 'en', 'USD', 'mwf')"
                ),
                {"id": uid, "email": f"{uid}@example.com"},
            )
            conn.execute(
                text(
                    "INSERT INTO api_tokens (user_id, name, token_hash, token_prefix) "
                    "VALUES (:user_id, 'legacy', :token_hash, 'pfa_legacy00')"
                ),
                {"user_id": uid, "token_hash": token_hash},
            )
        command.upgrade(alembic_cfg, "head")
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT holdings_version FROM users WHERE id=:id"), {"id": uid}
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    text("SELECT permission FROM api_tokens WHERE token_hash=:token_hash"),
                    {"token_hash": token_hash},
                ).scalar_one()
                == "read_only"
            )
            names = {
                str(row[0])
                for row in conn.execute(
                    text(
                        "SELECT conname FROM pg_constraint WHERE contype='c' "
                        "AND conrelid IN ('users'::regclass, 'api_tokens'::regclass)"
                    )
                )
            }
        assert "ck_users_holdings_version" in names
        assert "ck_api_tokens_permission" in names
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text("UPDATE api_tokens SET permission='admin' WHERE token_hash=:token_hash"),
                {"token_hash": token_hash},
            )
        command.downgrade(alembic_cfg, "d72900000001")
        command.upgrade(alembic_cfg, "head")
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM api_tokens WHERE user_id=:id"), {"id": uid})
            conn.execute(text("DELETE FROM users WHERE id=:id"), {"id": uid})
        engine.dispose()


def test_concurrent_creates_stop_at_the_cap(session_test_db: None) -> None:
    del session_test_db
    user_id = uuid.uuid4()
    with SessionLocal() as setup:
        seed_user(setup, user_id)
        for index in range(29):
            setup.add(
                Holding(
                    user_id=user_id,
                    name=f"Row {index}",
                    pricing_mode="manual",
                    currency="USD",
                    asset_type="other",
                    asset_class="STOCK",
                    shares=Decimal("1"),
                    position=index,
                    market="Other",
                    capture_supported=False,
                )
            )
        setup.commit()
    barrier = threading.Barrier(2)
    codes: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        with SessionLocal() as session:
            try:
                create_holding(session, user_id, _parsed(name=uuid.uuid4().hex))
                code = 201
            except HTTPException as exc:
                code = exc.status_code
        with lock:
            codes.append(code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    try:
        assert sorted(codes) == [201, 409]
        with SessionLocal() as check:
            count = check.scalar(
                select(func.count()).select_from(Holding).where(Holding.user_id == user_id)
            )
            assert count == 30
    finally:
        with SessionLocal() as cleanup:
            cleanup.execute(delete(Holding).where(Holding.user_id == user_id))
            cleanup.execute(delete(Account).where(Account.user_id == user_id))
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()
