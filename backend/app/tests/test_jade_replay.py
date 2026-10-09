"""Issue #714 replay contract on real Postgres; no provider calls."""

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.benchmark_price import BenchmarkPrice
from app.models.fx_rate import FxRate
from app.models.holding import Holding
from app.models.jade_price import JadePricePoint, JadePriceSeries
from app.schemas.jade import JadeReplayOut
from app.services import jade_replay as replay
from app.tests.conftest import TEST_USER_ID, seed_user

E = date(2026, 10, 8)
S = date(2021, 10, 8)


def calendar(session: Session, days: list[date]) -> None:
    session.add_all(
        [BenchmarkPrice(index_code="sp500", price_date=d, close_price=Decimal(100)) for d in days]
    )
    session.flush()


def series(
    session: Session,
    key: str,
    days: list[date],
    prices: list[float],
    *,
    attempted: bool = True,
    unusable: bool = False,
) -> None:
    session.add(
        JadePriceSeries(
            series_key=key,
            last_attempt_on=E if attempted else None,
            unusable_reason="unparsed_distribution" if unusable else None,
        )
    )
    session.flush()
    session.add_all(
        [
            JadePricePoint(series_key=key, trade_date=d, close=Decimal(str(p)))
            for d, p in zip(days, prices, strict=True)
        ]
    )
    session.flush()


def holding(
    session: Session,
    *,
    value: str = "100",
    currency: str = "USD",
    asset_type: str = "other",
    asset_class: str = "STOCK",
    market: str = "US",
    auto: bool = False,
    ticker: str | None = "AAPL",
    fund_code: str | None = None,
) -> Holding:
    h = Holding(
        user_id=TEST_USER_ID,
        name="Example",
        current_value=Decimal(value),
        currency=currency,
        pricing_mode="auto" if auto else "manual",
        asset_type=asset_type,
        asset_class=asset_class,
        market=market,
        ticker=ticker,
        fund_code=fund_code,
        shares=Decimal(1),
        market_price=Decimal(value),
        position=len(session.new),
    )
    session.add(h)
    session.flush()
    return h


@pytest.fixture(autouse=True)
def setup(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_user(db_session, TEST_USER_ID)
    monkeypatch.setattr(replay, "today_et", lambda: E)


def compute(session: Session, currency: str = "USD", benchmark: str = "sp500") -> JadeReplayOut:
    return replay.compute_replay(session, TEST_USER_ID, currency, benchmark, "5Y")


def test_d7_1_anchoring_fx(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    days = [E - timedelta(days=2), E - timedelta(days=1), E]
    calendar(db_session, days)
    series(db_session, "yf:AAPL", [S, *days], [100, 100, 110, 99])
    holding(db_session, value="990", auto=True)
    db_session.add_all(
        [
            FxRate(pair="USDCNY", rate_date=d, rate=Decimal(str(r)))
            for d, r in zip(days, [7, 7.2, 7.1], strict=True)
        ]
    )
    db_session.flush()
    captured: list[tuple[date, Decimal]] = []
    original = replay.anchored_values

    def observe(
        amount: Decimal, values: dict[date, Decimal], end: date
    ) -> list[tuple[date, Decimal]]:
        assert amount == Decimal("7029")
        assert [values[d] for d in days] == [Decimal("700"), Decimal("792"), Decimal("702.9")]
        points = original(amount, values, end)
        captured.extend(points)
        return points

    monkeypatch.setattr(replay, "anchored_values", observe)
    result = compute(db_session, "CNY")
    assert [v for _, v in captured] == [Decimal("7000"), Decimal("7920"), Decimal("7029")]
    assert result.holdings[0].method == "own"


def test_d7_2_cash_stock_metrics() -> None:
    days = [date(2026, 10, 6), date(2026, 10, 7), E]
    m = replay.metrics(list(zip(days, [2000.0, 2100.0, 1990.0], strict=True)), False)
    assert m.cumulative_return == "-0.005000"
    assert m.max_drawdown == m.worst_day == "-0.052381"
    assert m.max_drawdown_peak == days[1] and m.max_drawdown_trough == m.worst_day_date == E


@pytest.mark.parametrize("count,expected", [(60, "2.0000"), (59, "1.0000")])
def test_a2_d7_4_beta_fill(count: int, expected: str) -> None:
    pairs = [(2 * p, p) for p in ([0.01, -0.01] * 30)[:count]]
    assert f"{replay.estimate_beta(pairs):.4f}" == expected
    beta = replay.estimate_beta(pairs)
    assert replay.backward_value(50.0, 101.0, 100.0, beta) == pytest.approx(
        49.019608 if count == 60 else 49.504950, abs=1e-6
    )


def test_a2_flat_proxy_beta() -> None:
    assert replay.estimate_beta([(0.02, 0.0)] * 60) == 1


def test_a3_d7_3_carry_skip() -> None:
    mon = date(2026, 9, 21)
    prices = {mon: Decimal(100), mon + timedelta(days=12): Decimal(120)}
    assert replay.price_on(prices, mon + timedelta(days=2)) == 100
    assert replay.price_on(prices, mon + timedelta(days=10)) == 100
    assert replay.price_on(prices, mon + timedelta(days=11)) is None
    assert replay.valid_returns([(mon, 100.0), (mon + timedelta(days=12), 120.0)])[0][
        1
    ] == pytest.approx(0.2)


def test_a4_metrics_month_boundary() -> None:
    days = [date(2026, 9, 25) + timedelta(days=i) for i in range(15)]
    values = [
        100.0,
        105.0,
        110.0,
        100.0,
        90.0,
        95.0,
        100.0,
        99.0,
        98.0,
        97.0,
        96.0,
        95.0,
        94.0,
        93.0,
        92.0,
    ]
    m = replay.metrics(list(zip(days, values, strict=True)), False)
    assert m.model_dump() == dict(
        cumulative_return="-0.080000",
        annualized_return="-0.886433",
        annualized_vol="0.755157",
        max_drawdown="-0.181818",
        max_drawdown_peak=days[2],
        max_drawdown_trough=days[4],
        worst_day="-0.100000",
        worst_day_date=days[4],
        worst_month="-0.050000",
        worst_month_label="2026-09",
    )


@pytest.mark.parametrize(
    "asset_type,asset_class,currency,auto,key,first,method,reason",
    [
        ("cash", "CASH_EQUIV", "HKD", False, None, None, "cash", None),
        ("wmf", "BOND_FUND", "CNY", False, None, None, "cash_assumed", None),
        ("other", "CASH_EQUIV", "USD", False, None, None, "cash", None),
        ("fund", "BOND_FUND", "CNY", False, None, None, "cash_assumed", None),
        ("fund", "BOND_FUND", "CNH", False, None, None, "cash_assumed", None),
        ("fund", "BOND_FUND", "USD", False, None, None, "proxy", None),
        ("other", "EQUITY_DM", "EUR", False, None, None, "proxy", None),
        ("stock", "STOCK", "USD", True, "yf:AAPL", S, "own", None),
        ("stock", "STOCK", "USD", True, "yf:SPCX", date(2026, 6, 12), "head_proxy", None),
        ("stock", "STOCK", "USD", True, None, None, "excluded", "pending"),
        ("fund", "EQUITY_CN", "CNY", True, "nav:161725", S, "fund_nav", None),
    ],
)
def test_a1_d7_7_classification(
    db_session: Session,
    asset_type: str,
    asset_class: str,
    currency: str,
    auto: bool,
    key: str | None,
    first: date | None,
    method: str,
    reason: str | None,
) -> None:
    days = [E - timedelta(days=14 - i) for i in range(15)]
    calendar(db_session, days)
    for symbol in ["SPY", "AGG", "EFA"]:
        series(db_session, "yf:" + symbol, [S, *days], [100.0] * 16)
    if key and first:
        series(db_session, key, [first, E], [100.0, 110.0])
    for pair in ["USDCNY", "USDCNH", "USDEUR", "USDHKD"]:
        db_session.add(FxRate(pair=pair, rate_date=E, rate=Decimal(7)))
    holding(
        db_session,
        asset_type=asset_type,
        asset_class=asset_class,
        currency=currency,
        auto=auto,
        ticker=None if key and key.startswith("nav:") else key[3:] if key else "AAPL",
        fund_code="161725" if key and key.startswith("nav:") else None,
    )
    db_session.flush()
    row = compute(db_session).holdings[0]
    assert (row.method, row.excluded_reason) == (method, reason)
    if method == "proxy":
        assert row.proxy_symbol == ("AGG" if asset_class == "BOND_FUND" else "EFA")


@pytest.mark.parametrize(
    "asset_type,asset_class",
    [("cash", "STOCK"), ("other", "CASH_EQUIV")],
    ids=["cash_asset_type", "cash_equiv_class"],
)
def test_a1_zero_cash_is_unvalued(db_session: Session, asset_type: str, asset_class: str) -> None:
    calendar(db_session, [E])
    zero = holding(db_session, value="0", asset_type=asset_type, asset_class=asset_class)
    holding(db_session, value="100", asset_type="cash")
    row = next(h for h in compute(db_session).holdings if h.holding_id == zero.id)
    assert (row.method, row.excluded_reason) == ("excluded", "unvalued")


@pytest.mark.parametrize(
    "market,symbol",
    [
        ("US", "SPY"),
        ("HK", "2800.HK"),
        ("A-Share", "510300.SS"),
        ("UK", "ISF.L"),
        ("Europe", "EXSA.DE"),
        ("Japan", "1306.T"),
        ("Korea", "069500.KS"),
        ("Other", "ACWI"),
    ],
)
def test_a1_stock_markets(db_session: Session, market: str, symbol: str) -> None:
    calendar(db_session, [E])
    series(db_session, "yf:" + symbol, [E], [100.0])
    for pair in ["USDHKD", "USDCNY", "USDGBP", "USDEUR", "USDJPY", "USDKRW"]:
        db_session.add(FxRate(pair=pair, rate_date=E, rate=Decimal(7)))
    holding(db_session, market=market)
    db_session.flush()
    assert compute(db_session).holdings[0].proxy_symbol == symbol


@pytest.mark.parametrize(
    "kind",
    [
        "empty",
        "stale",
        "unusable",
        "never_attempted",
        "proxy_pending",
        "proxy_stale",
        "unvalued",
        "zero",
        "missing_fx",
    ],
)
def test_a1_unavailable(db_session: Session, kind: str) -> None:
    calendar(db_session, [E])
    if kind not in ("proxy_pending",):
        series(
            db_session,
            "yf:SPY",
            [E - timedelta(days=11)] if kind == "proxy_stale" else [E],
            [100.0],
        )
    if kind in ("empty", "stale", "unusable", "never_attempted"):
        ds = [] if kind == "empty" else [E - timedelta(days=11) if kind == "stale" else E]
        series(
            db_session,
            "yf:AAPL",
            ds,
            [100.0] * len(ds),
            attempted=kind != "never_attempted",
            unusable=kind == "unusable",
        )
    h = holding(
        db_session,
        auto=kind in ("empty", "stale", "unusable", "never_attempted"),
        value="0" if kind == "zero" else "100",
        currency="CNY" if kind == "missing_fx" else "USD",
        asset_type="cash" if kind == "missing_fx" else "stock",
    )
    if kind == "unvalued":
        h.current_value = None
    if kind == "missing_fx":
        db_session.add(FxRate(pair="USDCNY", rate_date=E + timedelta(days=1), rate=Decimal(7)))
    db_session.flush()
    r = compute(db_session)
    row = r.holdings[0]
    if kind in ("empty", "stale", "unusable"):
        assert row.method == "proxy" and row.own_history_unavailable
    else:
        assert row.method == "excluded"
        assert row.excluded_reason == (
            "unvalued"
            if kind in ("zero", "unvalued")
            else "data_unavailable"
            if kind in ("missing_fx", "proxy_stale")
            else "pending"
        )
    if kind in ("zero", "unvalued"):
        assert r.status == "no_holdings"
    if r.status in ("pending", "no_holdings"):
        assert r.coverage.own_share == "0.000000" and not r.coverage.data_quality


@pytest.mark.parametrize(
    "count,status", [(0, "no_holdings"), (1, "pending"), (9, "insufficient"), (10, "ok")]
)
def test_a5_status(db_session: Session, count: int, status: str) -> None:
    days = [E - timedelta(days=count - i) for i in range(count + 1)]
    calendar(db_session, days)
    if count:
        holding(db_session, auto=count == 1, asset_type="stock" if count == 1 else "cash")
    r = compute(db_session)
    assert r.status == status
    assert (r.points == []) == (status != "ok")
    assert (r.metrics.portfolio is None) == (status != "ok")
    if count == 1:
        assert r.coverage.pending_share == "1.000000"


def test_d7_8_data_quality(db_session: Session) -> None:
    days = [E - timedelta(days=14 - i) for i in range(15)]
    calendar(db_session, days)
    series(db_session, "yf:AAPL", [S, *days], [100.0] * 16)
    series(db_session, "yf:SPY", [S, *days], [100.0] * 16)
    holding(db_session, value="300", auto=True)
    holding(db_session, value="500")
    holding(db_session, value="200", asset_type="wmf")
    r = compute(db_session)
    assert r.status == "ok" and r.metrics.portfolio is not None
    assert r.coverage.approx_share_at_start == "0.700000" and r.coverage.data_quality


@pytest.mark.parametrize(
    "n,expected", [(0, "pending"), (1, "unavailable"), (10, "unavailable"), (11, "ok")]
)
def test_a6_benchmark(db_session: Session, n: int, expected: str) -> None:
    days = [E - timedelta(days=14 - i) for i in range(15)]
    calendar(db_session, days)
    holding(db_session, asset_type="cash")
    if n:
        series(db_session, "yf:510300.SS", days[-n:], [100.0 + i for i in range(n)])
    db_session.add_all(
        [FxRate(pair="USDCNY", rate_date=d, rate=Decimal(14 if d == E else 7)) for d in days]
    )
    db_session.flush()
    r = compute(db_session, benchmark="csi300")
    assert r.benchmark_symbol == "510300.SS" and r.benchmark_status == expected
    assert r.metrics.portfolio is not None
    assert (r.metrics.benchmark is not None) == (expected == "ok")
    if expected == "ok":
        assert r.points[-1].benchmark == "-0.450000"
    else:
        assert all(p.benchmark is None for p in r.points)


@pytest.mark.parametrize(
    "plan,pending,code",
    [("jade", False, 200), ("jade", True, 200), ("daily", False, 403), ("weekly", False, 403)],
)
def test_a7_d7_9_access_readonly(
    app_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    plan: str,
    pending: bool,
    code: int,
) -> None:
    from app.models.user import User

    user = db_session.get(User, TEST_USER_ID)
    assert user
    user.subscription_status = "active"
    user.subscription_type = plan
    user.subscription_cancel_pending = pending
    calendar(db_session, [E])
    db_session.flush()
    yf = Mock(side_effect=AssertionError("request-time provider call"))
    nav = Mock(side_effect=AssertionError("request-time provider call"))
    monkeypatch.setattr("app.services._yfinance.fetch_ohlcv_range_bounded", yf)
    monkeypatch.setattr("app.services.fund_nav_fetcher.fetch_nav_history_pages", nav)
    r = app_client.get("/jade/replay")
    assert r.status_code == code, r.text
    if code == 403:
        assert r.json()["detail"] == "subscription_required"
    yf.assert_not_called()
    nav.assert_not_called()
    assert not db_session.new and not db_session.dirty and not db_session.deleted


def test_d7_2_cash_stock_computation(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    days = [E - timedelta(days=2), E - timedelta(days=1), E]
    calendar(db_session, days)
    series(db_session, "yf:AAPL", [S, *days], [100, 100, 110, 99])
    holding(db_session, value="990", auto=True)
    holding(db_session, value="7000", asset_type="cash", currency="CNY")
    db_session.add_all([FxRate(pair="USDCNY", rate_date=d, rate=Decimal(7)) for d in days])
    db_session.flush()
    captured: list[dict[date, Decimal]] = []
    original = replay.anchored_values

    def observe(
        amount: Decimal, values: dict[date, Decimal], end: date
    ) -> list[tuple[date, Decimal]]:
        points = original(amount, values, end)
        captured.append(dict(points))
        return points

    monkeypatch.setattr(replay, "anchored_values", observe)
    compute(db_session)
    assert [sum(p[d] for p in captured) for d in days] == [
        Decimal(2000),
        Decimal(2100),
        Decimal(1990),
    ]


def test_a3_carry_skip_computation(db_session: Session) -> None:
    days = [E - timedelta(days=12 - i) for i in range(13)]
    calendar(db_session, days)
    series(db_session, "yf:AAPL", [S, days[0], E], [100, 100, 120])
    holding(db_session, value="120", auto=True)
    result = compute(db_session)
    assert result.status == "ok" and result.skipped_days == 1 and result.sample_count == 11
    assert result.points[-2].portfolio is None
    assert (
        result.metrics.portfolio is not None
        and result.metrics.portfolio.cumulative_return == "0.200000"
    )


@pytest.mark.parametrize("count", [59, 60])
def test_a2_d7_4_head_fill_computation(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    first = E - timedelta(days=count)
    before = first - timedelta(days=1)
    days = [S, before, *[first + timedelta(days=i) for i in range(count + 1)]]
    calendar(db_session, days)
    proxy = [101.0]
    own = [50.0]
    for i in range(count):
        p = 0.01 if i % 2 else -0.01
        proxy.append(proxy[-1] * (1 + p))
        own.append(own[-1] * (1 + 2 * p))
    series(db_session, "yf:SPY", days, [100.0, 100.0, *proxy])
    series(db_session, "yf:AAPL", days[2:], own)
    holding(db_session, auto=True)
    captured: list[dict[date, Decimal]] = []
    original = replay.anchored_values

    def observe(
        amount: Decimal, values: dict[date, Decimal], end: date
    ) -> list[tuple[date, Decimal]]:
        captured.append(values)
        return original(amount, values, end)

    monkeypatch.setattr(replay, "anchored_values", observe)
    r = compute(db_session)
    assert r.holdings[0].beta == ("2.0000" if count == 60 else "1.0000")
    assert r.holdings[0].beta_samples == count
    assert float(captured[0][before]) == pytest.approx(
        49.019608 if count == 60 else 49.504950, abs=1e-6
    )
