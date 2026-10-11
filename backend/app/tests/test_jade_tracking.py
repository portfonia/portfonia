"""Issue #729 contract tests on cached real Postgres fixtures."""

import importlib
from datetime import date, timedelta
from decimal import Decimal
from types import ModuleType
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.fx_rate import FxRate
from app.models.user import User
from app.schemas.jade import JadeTrackingOut
from app.services import jade_replay as replay
from app.services import jade_replay_config as config
from app.tests.conftest import TEST_USER_ID, seed_user
from app.tests.test_jade_replay import calendar, holding, series

SYMBOLS: list[str] = [
    "SPY",
    "QQQ",
    "DIA",
    "ONEQ",
    "IWF",
    "IWD",
    "IWM",
    "ACWI",
    "EFA",
    "EEM",
    "2800.HK",
    "510300.SS",
    "ISF.L",
    "EXSA.DE",
    "1306.T",
    "069500.KS",
    "AGG",
    "TLT",
    "BIL",
    "GLD",
    "DBC",
    "VNQ",
    "XLE",
    "CASH",
]
P: list[float] = [100, 101, 99, 102, 103, 101, 104, 106, 105, 107, 108, 106, 109, 111, 110, 112]
S: list[float] = [400, 402, 398, 405, 404, 401, 408, 410, 409, 412, 415, 411, 416, 419, 417, 421]
A: list[float] = [
    100,
    100.1,
    100.3,
    100.2,
    100.0,
    100.4,
    100.5,
    100.3,
    100.6,
    100.7,
    100.5,
    100.8,
    100.9,
    100.7,
    101.0,
    101.1,
]
DAYS: list[date] = [
    date(2026, 9, 16) + timedelta(days=i)
    for i in range(22)
    if (date(2026, 9, 16) + timedelta(days=i)).weekday() < 5
]


def module() -> ModuleType:
    return importlib.import_module("app.services.jade_tracking")


def user(session: Session) -> User:
    row = session.get(User, TEST_USER_ID)
    assert row
    return row


@pytest.fixture(autouse=True)
def setup(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_user(db_session, TEST_USER_ID)
    u = user(db_session)
    u.subscription_status = "active"
    u.subscription_type = "jade"
    db_session.flush()
    monkeypatch.setattr(replay, "today_et", lambda: DAYS[-1])
    from app.services import jade_price_history as history

    monkeypatch.setattr(
        history, "fetch_ohlcv_range_bounded", Mock(side_effect=AssertionError("provider call"))
    )
    monkeypatch.setattr(
        history, "fetch_nav_history_pages", Mock(side_effect=AssertionError("provider call"))
    )


def fixture(session: Session, n: int = 16, kind: str = "own", ref: str = "ok") -> list[date]:
    days = DAYS[:n]
    calendar(session, days)
    series(session, "yf:SPY", days, S[:n])
    if ref != "missing":
        rd = [] if ref == "empty" else days
        series(session, "yf:AGG", rd, A[:n] if rd else [], attempted=ref != "unattempted")
    if kind == "own":
        series(session, "yf:AAPL", [date(2025, 1, 1), *days], [100, *P[:n]])
        holding(session, auto=True)
    elif kind == "proxy":
        holding(session)
    elif kind == "head":
        series(session, "yf:AAPL", days[11:], P[11:n])
        holding(session, auto=True)
    elif kind == "pending":
        holding(session, auto=True)
    session.flush()
    return days


def compute(session: Session, base: str = "USD") -> JadeTrackingOut:
    result: JadeTrackingOut = module().compute_tracking(session, TEST_USER_ID, base, "1Y")
    return result


def test_b1_constants() -> None:
    assert list(config.TRACKING_SYMBOLS) == SYMBOLS
    assert {"yf:" + s for s in config.FIXED_ETF_SYMBOLS} | config.STYLE_KEYS >= config.TRACKING_KEYS
    assert config.DEFAULT_TRACKING_REFERENCE == (("SPY", 60), ("AGG", 40))


def test_b3_save(app_client: TestClient, db_session: Session) -> None:
    original = {
        "components": [
            {"symbol": "ACWI", "weight": 50},
            {"symbol": "AGG", "weight": 30},
            {"symbol": "CASH", "weight": 20},
        ]
    }
    for body in [
        original,
        {"components": [{"symbol": "SPY", "weight": 100}]},
        {"components": [{"symbol": "SPY", "weight": 100}]},
    ]:
        out = app_client.put("/jade/benchmark", json=body)
        assert out.status_code == 200, out.text
        assert out.json() == {**body, "saved": True}
        db_session.expire_all()
        assert user(db_session).jade_benchmark == body


INVALID = [
    [],
    [{"symbol": s, "weight": 10} for s in SYMBOLS[:6]],
    [{"symbol": "XYZ", "weight": 100}],
    [{"symbol": "SPY", "weight": 0}],
    [{"symbol": "SPY", "weight": 101}],
    [{"symbol": "SPY", "weight": 60.5}],
    [{"symbol": "SPY", "weight": 50}] * 2,
    [{"symbol": "SPY", "weight": 99}],
    [{"symbol": "SPY", "weight": 50}, {"symbol": "AGG", "weight": 51}],
]


@pytest.mark.parametrize(
    "components",
    INVALID,
    ids=["empty", "six", "unknown", "zero", "101", "fraction", "duplicate", "sum99", "sum101"],
)
def test_b4_reject(
    app_client: TestClient, db_session: Session, components: list[dict[str, object]]
) -> None:
    before: dict[str, object] = {"components": [{"symbol": "CASH", "weight": 100}]}
    user(db_session).jade_benchmark = before
    db_session.flush()
    assert app_client.put("/jade/benchmark", json={"components": components}).status_code == 422
    db_session.expire_all()
    assert user(db_session).jade_benchmark == before


@pytest.mark.parametrize(
    "components",
    [
        [{"symbol": "CASH", "weight": 100}],
        [{"symbol": "SPY", "weight": 100}],
        [{"symbol": s, "weight": 20} for s in SYMBOLS[:5]],
    ],
    ids=["cash", "spy", "five"],
)
def test_b4_accept(app_client: TestClient, components: list[dict[str, object]]) -> None:
    assert app_client.put("/jade/benchmark", json={"components": components}).status_code == 200


@pytest.mark.parametrize("method", ["get", "put"])
def test_b5_non_jade(app_client: TestClient, db_session: Session, method: str) -> None:
    user(db_session).subscription_type = "daily"
    db_session.flush()
    out = (
        app_client.get("/jade/tracking")
        if method == "get"
        else app_client.put(
            "/jade/benchmark", json={"components": [{"symbol": "CASH", "weight": 100}]}
        )
    )
    assert out.status_code == 403 and out.json()["detail"] == "subscription_required"


def test_b5_defaults_and_read_only(app_client: TestClient, db_session: Session) -> None:
    fixture(db_session)
    u = user(db_session)
    u.base_currency = "EUR"
    db_session.add_all([FxRate(pair="USDEUR", rate_date=d, rate=Decimal(".9")) for d in DAYS])
    db_session.flush()
    from sqlalchemy import event

    writes = []

    def observe(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        many: bool,
    ) -> None:
        if statement.lstrip().split()[0].upper() in ("INSERT", "UPDATE", "DELETE"):
            writes.append(statement)

    event.listen(db_session.get_bind(), "before_cursor_execute", observe)
    try:
        out = app_client.get("/jade/tracking")
    finally:
        event.remove(db_session.get_bind(), "before_cursor_execute", observe)
    assert out.status_code == 200, out.text
    schema = importlib.import_module("app.schemas.jade").JadeTrackingOut
    data = schema.model_validate(out.json())
    assert (data.base_currency, data.range) == ("EUR", "1Y")
    assert writes == [] and not db_session.new and not db_session.dirty and not db_session.deleted


@pytest.mark.parametrize("query", ["range=bad", "base_currency=bad"])
def test_b5_invalid_query(app_client: TestClient, query: str) -> None:
    assert app_client.get("/jade/tracking?" + query).status_code == 422


@pytest.mark.parametrize(
    "cash,expected", [(False, [1.0, 1.012, 1.010095]), (True, [1.0, 1.01, 1.005049])]
)
def test_b6_reference(cash: bool, expected: list[float]) -> None:
    values = [
        [100.0, 50.0 if not cash else 1.0],
        [102.0, 50.0 if not cash else 1.0],
        [101.0, 50.5 if not cash else 1.0],
    ]
    actual = module().reference_index(values, [0.5, 0.5] if cash else [0.6, 0.4])
    assert [replay.ratio(v) for v in actual] == [replay.ratio(v) for v in expected]
    if not cash:
        assert replay.ratio(actual[-1]) != "1.010000"


def test_b7_worked_metrics(db_session: Session) -> None:
    fixture(db_session)
    out = compute(db_session)
    assert out.metrics is not None
    assert out.metrics.model_dump() == {
        "portfolio_return": "0.120000",
        "benchmark_return": "0.035898",
        "excess_return": "0.084102",
        "annualized_excess": "5.331889",
        "tracking_error": "0.104434",
    }
    assert out.sample_count == 11
    b = replay.compute_replay(db_session, TEST_USER_ID, "USD", "sp500", "1Y")
    assert b.metrics.portfolio is not None
    assert out.metrics.portfolio_return == b.metrics.portfolio.cumulative_return


@pytest.mark.parametrize("n,count,available", [(14, 9, False), (15, 10, True)])
def test_b7_minimum(db_session: Session, n: int, count: int, available: bool) -> None:
    fixture(db_session, n)
    out = compute(db_session)
    assert out.metrics is not None
    assert out.sample_count == count and (out.metrics.tracking_error is not None) == available
    assert out.metrics.portfolio_return is not None


@pytest.mark.parametrize(
    "kind,n,ref,status,benchmark",
    [
        ("none", 16, "ok", "no_holdings", None),
        ("pending", 16, "ok", "pending", None),
        ("own", 5, "ok", "insufficient", None),
        ("own", 16, "missing", "ok", "pending"),
        ("own", 16, "unattempted", "ok", "pending"),
        ("own", 16, "empty", "ok", "unavailable"),
    ],
)
def test_b8_status(
    db_session: Session, kind: str, n: int, ref: str, status: str, benchmark: str | None
) -> None:
    fixture(db_session, n, kind, ref)
    out = compute(db_session)
    assert (out.status, out.benchmark_status) == (status, benchmark)
    assert out.metrics is None and out.points == [] and out.sample_count == 0
    assert (
        out.components
        and out.benchmark_saved is False
        and (out.horizon_days, out.min_samples) == (5, 10)
    )


@pytest.mark.parametrize(
    "gap,expected", [("first", "unavailable"), ("last", "unavailable"), ("interior", "ok")]
)
def test_b8_common_dates(db_session: Session, gap: str, expected: str) -> None:
    from app.models.jade_price import JadePricePoint

    fixture(db_session)
    # An interior gap must exceed the existing ten-day carry.
    days = [date(2026, 8, 3) + timedelta(days=i * 2) for i in range(16)]
    db_session.query(JadePricePoint).delete()
    db_session.flush()
    db_session.query(importlib.import_module("app.models.benchmark_price").BenchmarkPrice).delete()
    db_session.flush()
    calendar(db_session, days)
    for key, values in [("yf:AAPL", P), ("yf:SPY", S), ("yf:AGG", A)]:
        for i, d in enumerate(days):
            if key == "yf:AGG" and (
                (gap == "first" and i == 0)
                or (gap == "last" and i >= 10)
                or (gap == "interior" and 1 <= i <= 7)
            ):
                continue
            db_session.add(
                JadePricePoint(series_key=key, trade_date=d, close=Decimal(str(values[i])))
            )
    db_session.flush()
    out = compute(db_session)
    assert out.benchmark_status == expected
    if expected == "ok":
        assert [p.date for p in out.points] == [d for i, d in enumerate(days) if i not in (6, 7)]
    else:
        assert out.points == [] and out.metrics is None


def test_b9_currency_cash(db_session: Session) -> None:
    fixture(db_session)
    user(db_session).jade_benchmark = {
        "components": [{"symbol": "2800.HK", "weight": 50}, {"symbol": "CASH", "weight": 50}]
    }
    series(db_session, "yf:2800.HK", DAYS, S)
    for i, d in enumerate(DAYS):
        db_session.add_all(
            [
                FxRate(pair="USDHKD", rate_date=d, rate=Decimal(7) + Decimal(i) / 100),
                FxRate(pair="USDEUR", rate_date=d, rate=Decimal(".8") + Decimal(i) / 100),
            ]
        )
    db_session.flush()
    out = compute(db_session, "EUR")
    c = [S[i] / (7 + i / 100) * (0.8 + i / 100) for i in range(16)]
    index = 1.0
    for i in range(1, 16):
        index *= 1 + 0.5 * (c[i] / c[i - 1] - 1)
    assert out.metrics is not None
    assert out.metrics.benchmark_return == replay.ratio(index - 1)
    b = replay.compute_replay(db_session, TEST_USER_ID, "EUR", "sp500", "1Y")
    assert b.metrics.portfolio is not None
    assert out.metrics.portfolio_return == b.metrics.portfolio.cumulative_return


@pytest.mark.parametrize("kind,flag", [("own", False), ("proxy", True), ("head", True)])
def test_b10_proxy(db_session: Session, kind: str, flag: bool) -> None:
    fixture(db_session, kind=kind)
    out = compute(db_session)
    assert out.proxy_note is flag
    assert (out.horizon_days, out.min_samples) == (5, 10)


@pytest.mark.parametrize("kind", ["none", "pending", "short", "own"])
@pytest.mark.parametrize("saved", [False, True])
def test_b10_reference_every_status(db_session: Session, kind: str, saved: bool) -> None:
    fixture(db_session, 5 if kind == "short" else 16, "own" if kind == "short" else kind)
    components = [
        {"symbol": "ACWI", "weight": 50},
        {"symbol": "AGG", "weight": 30},
        {"symbol": "CASH", "weight": 20},
    ]
    if saved:
        user(db_session).jade_benchmark = {"components": components}
        db_session.flush()
    out = compute(db_session)
    assert [c.model_dump() for c in out.components] == (
        components if saved else [{"symbol": "SPY", "weight": 60}, {"symbol": "AGG", "weight": 40}]
    )
    assert out.benchmark_saved is saved


def test_b11_points(db_session: Session) -> None:
    fixture(db_session)
    out = compute(db_session)
    assert [p.date for p in out.points] == DAYS
    assert out.metrics is not None
    assert out.points[0].excess == "0.000000" and out.points[-1].excess == out.metrics.excess_return
