"""Issue #643 snapshot export acceptance tests using real encrypted ORM rows."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import cast
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.core import rate_limit
from app.core.deps import current_principal
from app.core.timezones import today_et
from app.main import app
from app.models.portfolio_snapshot_batch import PortfolioSnapshotBatch
from app.models.portfolio_value_snapshot import PortfolioValueSnapshot
from app.models.user import User
from app.services import subscription
from app.tests.conftest import TEST_USER_ID, seed_user

URL = "/portfolio/snapshots"
TODAY = today_et()
START = TODAY - timedelta(days=29)
KEY = f"rl:snapshot_export:user:{TEST_USER_ID}:3600"
FIELDS = {
    "holding_id",
    "ticker",
    "fund_code",
    "market",
    "broker",
    "account",
    "portfolio",
    "asset_class",
    "pricing_mode",
    "currency",
    "shares",
    "current_value",
    "market_value",
    "market_value_base",
    "cost_basis_base",
    "fx_rate_used",
    "price_as_of",
    "fx_as_of",
    "data_quality",
}


def _user(session: Session, status: str = "active", plan: str = "mwf") -> User:
    user = seed_user(session, TEST_USER_ID)
    user.subscription_status = status
    user.subscription_type = plan
    session.flush()
    return user


@pytest.fixture
def allowed(monkeypatch: pytest.MonkeyPatch, db_session: Session) -> User:
    monkeypatch.setattr(subscription, "ADVANCED_SUBSCRIPTION_TYPES", ("mwf",))
    return _user(db_session)


def _params(start: date = START, end: date = TODAY) -> dict[str, str]:
    return {"start": start.isoformat(), "end": end.isoformat()}


def _batch(
    session: Session, day: date, status: str = "complete", uid: uuid.UUID = TEST_USER_ID
) -> None:
    session.add(PortfolioSnapshotBatch(user_id=uid, snapshot_date=day, status=status))
    session.flush()


def _row(
    session: Session,
    day: date,
    uid: uuid.UUID = TEST_USER_ID,
    ticker: str | None = "NVDA",
    holding_id: uuid.UUID | None = None,
    base_currency: str = "USD",
    fund_code: str | None = None,
) -> PortfolioValueSnapshot:
    row = PortfolioValueSnapshot(
        user_id=uid,
        snapshot_date=day,
        holding_id=holding_id or uuid.uuid4(),
        ticker=ticker,
        fund_code=fund_code,
        market="US",
        broker="Example Broker",
        account="Main",
        portfolio="Growth",
        asset_class="EQUITY_US_TECH",
        pricing_mode="auto",
        currency="USD",
        shares=Decimal("10.125"),
        current_value=None,
        market_value=Decimal("1250.40"),
        base_currency=base_currency,
        market_value_base=Decimal("1250.40"),
        cost_basis_base=Decimal("900.00"),
        fx_rate_used=Decimal("1"),
        price_as_of=day,
        fx_as_of=day,
        data_quality="ok",
    )
    session.add(row)
    session.flush()
    return row


def test_snapshot_export_1_basic_plan_denies_without_counting(
    app_client: TestClient, db_session: Session
) -> None:
    _user(db_session)
    resp = app_client.get(URL, params=_params())
    assert resp.status_code == 403
    assert resp.json() == {"detail": "subscription_required"}
    assert KEY not in cast(rate_limit.InMemoryBackend, rate_limit.get_backend()).stored_keys()
    assert subscription.ADVANCED_SUBSCRIPTION_TYPES == ("daily",)


def test_snapshot_export_2_accepts_30_days(
    app_client: TestClient, allowed: User, db_session: Session
) -> None:
    assert (TODAY - START).days == 29
    for day in (START, TODAY):
        _batch(db_session, day)
        _row(db_session, day)
    resp = app_client.get(URL, params=_params())
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/json"
    body = resp.json()
    assert body["start"] == START.isoformat()
    assert body["end"] == TODAY.isoformat()
    assert [day["date"] for day in body["days"]] == [START.isoformat(), TODAY.isoformat()]


@pytest.mark.parametrize(
    ("start", "end", "detail"),
    [
        (TODAY - timedelta(days=30), TODAY, "range must not exceed 30 days"),
        (TODAY, TODAY - timedelta(days=1), "start must not be after end"),
        (TODAY, TODAY + timedelta(days=1), "end must not be in the future"),
    ],
    ids=["31-days", "reversed", "future-et"],
)
def test_snapshot_export_2_rejects_invalid_ranges(
    app_client: TestClient, allowed: User, start: date, end: date, detail: str
) -> None:
    resp = app_client.get(URL, params=_params(start, end))
    assert resp.status_code == 422
    assert resp.json() == {"detail": detail}


@pytest.mark.parametrize(
    "params",
    [
        {"end": TODAY.isoformat()},
        {"start": START.isoformat()},
        {"start": "invalid", "end": TODAY.isoformat()},
    ],
    ids=["missing-start", "missing-end", "malformed-start"],
)
def test_snapshot_export_2_invalid_parameters_count(
    app_client: TestClient, allowed: User, params: dict[str, str]
) -> None:
    resp = app_client.get(URL, params=params)
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"][0] == "query"
    # The next increment returns two: the invalid request already consumed one.
    assert rate_limit.get_backend().incr_with_ttl(KEY, 3600) == 2


def test_snapshot_export_2_complete_days_order_and_capture_currency(
    app_client: TestClient, allowed: User, db_session: Session
) -> None:
    for offset, status, currency in [
        (3, "complete", "CNY"),
        (1, "complete", "USD"),
        (2, "pending", "USD"),
        (4, "skipped_deps", "USD"),
    ]:
        day = START + timedelta(days=offset)
        _batch(db_session, day, status)
        _row(db_session, day, base_currency=currency)
    # Rows outside the range and rows without batches cannot leak into the response.
    for day in [START - timedelta(days=1), TODAY + timedelta(days=1)]:
        _batch(db_session, day)
        _row(db_session, day)
    _row(db_session, START + timedelta(days=5))
    resp = app_client.get(URL, params=_params())
    assert resp.status_code == 200
    assert [(d["date"], d["base_currency"]) for d in resp.json()["days"]] == [
        ((START + timedelta(days=1)).isoformat(), "USD"),
        ((START + timedelta(days=3)).isoformat(), "CNY"),
    ]


def test_snapshot_export_2_exact_fields_decryption_decimals_and_id(
    app_client: TestClient, allowed: User, db_session: Session
) -> None:
    _batch(db_session, START)
    row = _row(db_session, START)
    row.current_value = Decimal("37.1250")
    db_session.flush()
    stored_id = str(row.holding_id)
    # Raw SQL bypasses TypeDecorator; prove fixtures are encrypted in the database.
    raw = db_session.execute(
        text(
            "SELECT ticker, broker, shares, current_value FROM portfolio_value_snapshots WHERE id = :id"
        ),
        {"id": row.id},
    ).one()
    assert raw.ticker != "NVDA"
    assert raw.broker != "Example Broker"
    assert raw.shares != "10.125"
    assert raw.current_value != "37.1250"
    db_session.expire_all()
    resp = app_client.get(URL, params=_params())
    assert resp.status_code == 200
    holding = resp.json()["days"][0]["holdings"][0]
    assert set(holding) == FIELDS
    assert holding["ticker"] == "NVDA"
    assert holding["broker"] == "Example Broker"
    assert holding["account"] == "Main"
    assert holding["portfolio"] == "Growth"
    assert holding["fund_code"] is None
    assert holding["price_as_of"] == START.isoformat()
    assert holding["fx_as_of"] == START.isoformat()
    assert holding["market"] == "US"
    assert holding["asset_class"] == "EQUITY_US_TECH"
    assert holding["pricing_mode"] == "auto"
    assert holding["currency"] == "USD"
    assert holding["data_quality"] == "ok"
    assert holding["holding_id"] == stored_id
    expected = {
        "shares": "10.125",
        "current_value": "37.1250",
        "market_value": "1250.40",
        "market_value_base": "1250.40",
        "cost_basis_base": "900.00",
        "fx_rate_used": "1",
    }
    for field, value in expected.items():
        assert isinstance(holding[field], str)
        assert holding[field] == value


def test_snapshot_export_2_sorts_decrypted_ticker_fund_and_id(
    app_client: TestClient, allowed: User, db_session: Session
) -> None:
    _batch(db_session, START)
    ids = [uuid.UUID(int=i) for i in (3, 2, 1)]
    _row(db_session, START, holding_id=ids[0])
    _row(db_session, START, holding_id=ids[1])
    _row(db_session, START, ticker=None, fund_code="110011", holding_id=ids[2])
    resp = app_client.get(URL, params=_params())
    assert resp.status_code == 200
    holdings = resp.json()["days"][0]["holdings"]
    assert [h["holding_id"] for h in holdings] == [str(ids[2]), str(ids[1]), str(ids[0])]
    assert holdings[0]["fund_code"] == "110011"
    assert holdings[0]["current_value"] is None


@pytest.mark.parametrize(
    ("status", "plan"),
    [("expired", "mwf"), ("inactive", "mwf"), ("cancelled", "mwf"), ("active", "weekly")],
)
def test_snapshot_export_3_subscription_gate(
    app_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    plan: str,
) -> None:
    monkeypatch.setattr(subscription, "ADVANCED_SUBSCRIPTION_TYPES", ("mwf",))
    _user(db_session, status, plan)
    resp = app_client.get(URL, params=_params())
    assert resp.status_code == 403
    assert resp.json() == {"detail": "subscription_required"}
    assert KEY not in cast(rate_limit.InMemoryBackend, rate_limit.get_backend()).stored_keys()


def test_snapshot_export_3_cancel_pending_remains_allowed(
    app_client: TestClient, allowed: User, db_session: Session
) -> None:
    allowed.subscription_cancel_pending = True
    allowed.subscription_expires_on = TODAY
    db_session.flush()
    assert app_client.get(URL, params=_params()).status_code == 200


def test_snapshot_export_4_only_callers_rows_and_batches(
    app_client: TestClient, allowed: User, db_session: Session
) -> None:
    other = uuid.uuid4()
    seed_user(db_session, other)
    _batch(db_session, START)
    own = _row(db_session, START)
    _batch(db_session, START, uid=other)
    _row(db_session, START, uid=other, ticker="OTHER")
    # Another user's complete batch cannot make the caller's pending date visible.
    second = START + timedelta(days=1)
    _batch(db_session, second, "pending")
    _row(db_session, second, ticker="PENDING")
    _batch(db_session, second, uid=other)
    _row(db_session, second, uid=other, ticker="OTHER2")
    resp = app_client.get(URL, params=_params())
    assert resp.status_code == 200
    days = resp.json()["days"]
    assert len(days) == 1
    assert days[0]["date"] == START.isoformat()
    assert [h["holding_id"] for h in days[0]["holdings"]] == [str(own.holding_id)]
    assert days[0]["holdings"][0]["ticker"] == "NVDA"


@pytest.mark.parametrize("valid", [True, False], ids=["successes", "validation-failures"])
def test_snapshot_export_5_twenty_then_429_with_retry_after(
    app_client: TestClient, allowed: User, valid: bool
) -> None:
    params = _params() if valid else {"end": TODAY.isoformat()}
    for _ in range(20):
        assert app_client.get(URL, params=params).status_code == (200 if valid else 422)
    resp = app_client.get(URL, params=params)
    assert resp.status_code == 429
    assert resp.json() == {"detail": rate_limit.RATE_LIMIT_DETAIL}
    assert resp.headers["Retry-After"] == "3600"
    backend = cast(rate_limit.InMemoryBackend, rate_limit.get_backend())
    backend.advance(3600)
    assert app_client.get(URL, params=_params()).status_code == 200


def test_snapshot_export_5_unavailable_returns_503(app_client: TestClient, allowed: User) -> None:
    with patch.object(
        rate_limit.get_backend(), "incr_with_ttl", side_effect=rate_limit.RateLimitUnavailable
    ):
        resp = app_client.get(URL, params=_params())
    assert resp.status_code == 503
    assert resp.json() == {"detail": rate_limit.UNAVAILABLE_DETAIL}


def test_snapshot_export_6_empty_complete_day_has_null_currency(
    app_client: TestClient, allowed: User, db_session: Session
) -> None:
    allowed.base_currency = "CNY"
    db_session.flush()
    _batch(db_session, START)
    resp = app_client.get(URL, params=_params())
    assert resp.status_code == 200
    assert resp.json()["days"] == [
        {"date": START.isoformat(), "base_currency": None, "holdings": []}
    ]


def test_snapshot_export_auth_precedes_gate_and_validation(app_client: TestClient) -> None:
    override = app.dependency_overrides.pop(current_principal)
    try:
        resp = app_client.get(URL)
    finally:
        app.dependency_overrides[current_principal] = override
    assert resp.status_code == 401
    assert resp.json() == {"detail": "unauthorized"}


def test_snapshot_export_gate_precedes_parameter_validation(
    app_client: TestClient, db_session: Session
) -> None:
    _user(db_session)
    resp = app_client.get(URL)
    assert resp.status_code == 403
    assert resp.json() == {"detail": "subscription_required"}
    assert KEY not in cast(rate_limit.InMemoryBackend, rate_limit.get_backend()).stored_keys()


def test_snapshot_export_read_has_no_table_writes(
    app_client: TestClient, allowed: User, db_session: Session
) -> None:
    _batch(db_session, START)
    _row(db_session, START)
    statements: list[str] = []

    def record(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement.strip().split()[0].upper())

    connection = db_session.connection()
    event.listen(connection, "before_cursor_execute", record)
    try:
        resp = app_client.get(URL, params=_params())
    finally:
        event.remove(connection, "before_cursor_execute", record)
    assert resp.status_code == 200
    assert statements and set(statements) == {"SELECT"}
