"""Issue #720 acceptance contract, real Postgres and fake providers."""

import importlib
from datetime import date, timedelta
from decimal import Decimal
from types import ModuleType
from typing import cast
from unittest.mock import Mock

import httpx
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from alembic import command
from app.core.config import get_settings
from app.models.fx_rate import FxRate
from app.models.jade_price import JadeScenarioSeries
from app.models.user import User
from app.schemas.jade import StressScenario
from app.services import (
    fx_fetcher,
)
from app.services import (
    jade_price_history as history,
)
from app.services import (
    jade_replay as replay,
)
from app.services import (
    jade_replay_config as config,
)
from app.services._yfinance import OhlcvPoint
from app.services.fund_nav_fetcher import NavRow
from app.tests.conftest import TEST_USER_ID, seed_user
from app.tests.test_jade_replay import E, S, calendar, holding, series

P = date(2020, 2, 19)
T = date(2020, 3, 23)
START = date(2019, 8, 19)
DAYS = [
    START,
    *[P - timedelta(days=i) for i in reversed(range(5))],
    *[T + timedelta(days=i) for i in range(6)],
]


def stress() -> ModuleType:

    assert importlib.util.find_spec("app.services.jade_stress") is not None, (
        "missing stress computation (issue #720)"
    )
    return importlib.import_module("app.services.jade_stress")


def models() -> ModuleType:

    m = importlib.import_module("app.models.jade_price")
    assert hasattr(m, "JadeScenarioSeries"), "missing per-window cache (issue #720)"
    return m


def cached(
    session: Session,
    key: str,
    days: list[date],
    values: list[float],
    sid: str = "covid_2020",
    attempted: bool = True,
) -> None:

    m = models()
    session.add(
        m.JadeScenarioSeries(
            scenario_id=sid, series_key=key, last_attempt_on=E if attempted else None
        )
    )
    session.flush()
    session.add_all(
        [
            m.JadeScenarioPoint(
                scenario_id=sid, series_key=key, trade_date=d, close=Decimal(str(v))
            )
            for d, v in zip(days, values, strict=True)
        ]
    )
    session.flush()


def path(session: Session) -> None:

    cached(session, "yf:SPY", DAYS, [100.0] * len(DAYS))


def compute(session: Session, benchmark: str = "sp500") -> StressScenario:

    return cast(
        StressScenario,
        stress().compute_stress(session, TEST_USER_ID, "USD", benchmark).scenarios[0],
    )


@pytest.fixture(autouse=True)
def setup(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:

    seed_user(db_session, TEST_USER_ID)
    monkeypatch.setattr(replay, "today_et", lambda: E)
    if importlib.util.find_spec("app.services.jade_stress"):
        monkeypatch.setattr(stress(), "today_et", lambda: E)
    guard = Mock(side_effect=AssertionError("real provider call"))
    monkeypatch.setattr(history, "fetch_ohlcv_range_bounded", guard)
    monkeypatch.setattr(history, "fetch_nav_history_pages", guard)


def test_a1_config() -> None:

    scenarios = getattr(config, "SCENARIOS", None)
    assert scenarios is not None
    assert [(s.id, s.start, s.end) for s in scenarios] == [
        ("covid_2020", START, date(2020, 9, 23)),
        ("rates_2022", date(2021, 7, 3), date(2023, 4, 12)),
        ("tariffs_2025", date(2024, 8, 19), date(2025, 10, 8)),
    ]
    assert config.months_after(date(2025, 8, 31), 6) == date(2026, 2, 28)


def test_a2_migration(alembic_cfg: Config) -> None:

    command.upgrade(alembic_cfg, "head")
    engine = create_engine(get_settings().database_url)
    with engine.connect() as conn:
        i = inspect(conn)
        assert "jade_scenario_series" in i.get_table_names()
        assert i.get_pk_constraint("jade_scenario_series")["constrained_columns"] == [
            "scenario_id",
            "series_key",
        ]
        assert i.get_pk_constraint("jade_scenario_points")["constrained_columns"] == [
            "scenario_id",
            "series_key",
            "trade_date",
        ]
        fk = i.get_foreign_keys("jade_scenario_points")[0]
        assert (
            fk["referred_table"] == "jade_scenario_series"
            and fk["options"]["ondelete"] == "CASCADE"
        )
        conn.execute(
            text("INSERT INTO jade_scenario_series(scenario_id,series_key) VALUES ('test','yf:A')")
        )
        conn.execute(
            text("INSERT INTO jade_scenario_points VALUES ('test','yf:A','2020-01-01',100,NULL)")
        )
        conn.execute(text("DELETE FROM jade_scenario_series WHERE scenario_id='test'"))
        assert conn.scalar(text("SELECT count(*) FROM jade_scenario_points")) == 0
        conn.commit()
    command.downgrade(alembic_cfg, "d71400000001")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT to_regclass('jade_scenario_points')")) is None
        assert conn.scalar(text("SELECT to_regclass('jade_scenario_series')")) is None
    command.upgrade(alembic_cfg, "head")
    engine.dispose()


def test_a3_fill_retry_future_holdings(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:

    fill = getattr(history, "fill_scenarios", None)
    assert callable(fill), "missing scenario fill/retry (issue #720)"
    calls: list[tuple[list[str], date, date]] = []

    def yf(symbols: list[str], start: date, end: date) -> dict[str, list[OhlcvPoint]]:

        calls.append((symbols, start, end))
        return {k: [(start, 100, 100, 100, 100, 1)] for k in symbols if k != "EMPTY"}

    monkeypatch.setattr(history, "fetch_ohlcv_range_bounded", yf)
    r = fill(db_session, E, {"yf:A", "yf:EMPTY"})
    assert (r.attempted, r.written, r.failed) == (6, 3, 3)
    assert calls == [
        (["A", "EMPTY"], s.start - timedelta(days=10), s.end + timedelta(days=1))
        for s in config.SCENARIOS
    ]
    calls.clear()
    fill(db_session, E + timedelta(days=1), {"yf:A", "yf:ABC", "yf:EMPTY"})
    assert [c[0] for c in calls] == [["ABC", "EMPTY"]] * 3
    for s in config.SCENARIOS:
        abc = db_session.get(JadeScenarioSeries, (s.id, "yf:ABC"))
        empty = db_session.get(JadeScenarioSeries, (s.id, "yf:EMPTY"))
        assert abc is not None and empty is not None
        assert abc.last_success_on == E + timedelta(days=1)
        assert empty.last_attempt_on == E + timedelta(days=1)


def test_a3_nav_distribution_isolation(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:

    fill = getattr(history, "fill_scenarios", None)
    assert callable(fill)
    calls: list[tuple[date, date, date | None]] = []

    def nav(
        code: str, client: httpx.Client, start: date, end: date, stop: date | None
    ) -> list[NavRow]:

        calls.append((start, end, stop))
        return [
            NavRow(start, Decimal("1.5"), ""),
            NavRow(
                start + timedelta(days=1),
                Decimal("1.45"),
                "unknown" if end.year == 2020 else "每10份派现金0.5元",
            ),
        ]

    monkeypatch.setattr(history, "fetch_nav_history_pages", nav)
    cached(db_session, "nav:008142", [START], [99])
    fill(db_session, E, {"nav:008142"})
    m = models()
    assert (
        cast(
            JadeScenarioSeries, db_session.get(m.JadeScenarioSeries, ("covid_2020", "nav:008142"))
        ).unusable_reason
        == "unparsed_distribution"
    )
    assert not list(
        db_session.scalars(
            select(m.JadeScenarioPoint).where(m.JadeScenarioPoint.scenario_id == "covid_2020")
        )
    )
    pts = list(
        db_session.scalars(
            select(m.JadeScenarioPoint)
            .where(m.JadeScenarioPoint.scenario_id == "rates_2022")
            .order_by(m.JadeScenarioPoint.trade_date)
        )
    )
    assert [p.close for p in pts] == [Decimal("1.5"), Decimal("1.5")]
    assert all(c[2] is None for c in calls)
    calls.clear()
    fill(db_session, E + timedelta(days=1), {"nav:008142"})
    assert calls == []


def test_a3_nightly_boundary_summary(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:

    fill = getattr(history, "fill_scenarios", None)
    assert callable(fill)
    yf = Mock(return_value={})
    monkeypatch.setattr(history, "fetch_ohlcv_range_bounded", yf)
    assert history.refresh_jade_price_history(db_session, E) == history.FillSummary()
    yf.assert_not_called()
    assert not list(db_session.scalars(select(models().JadeScenarioSeries)))
    u = db_session.get(User, TEST_USER_ID)
    assert u is not None
    u.subscription_status = "active"
    u.subscription_type = "jade"
    h = holding(db_session, auto=True, ticker="ABC")
    monkeypatch.setattr(history, "fill_scenarios", Mock(return_value=history.FillSummary()))
    before = history.refresh_jade_price_history(db_session, E)
    from app.models.jade_price import JadePricePoint, JadePriceSeries

    def snapshot() -> list[tuple[str, date | None, date | None]]:

        return [
            (s.series_key, s.last_attempt_on, s.last_success_on)
            for s in db_session.scalars(
                select(JadePriceSeries).order_by(JadePriceSeries.series_key)
            )
        ]

    old = snapshot()
    monkeypatch.setattr(history, "fill_scenarios", fill)
    assert history.refresh_jade_price_history(db_session, E) == before
    assert snapshot() == old
    assert not list(db_session.scalars(select(JadePricePoint)))
    assert all(
        db_session.get(models().JadeScenarioSeries, (s.id, "yf:ABC")) is not None
        for s in config.SCENARIOS
    )
    h.ticker = "NEW"
    db_session.flush()
    history.refresh_jade_price_history(db_session, E + timedelta(days=1))
    assert all(
        db_session.get(models().JadeScenarioSeries, (s.id, "yf:NEW")) is not None
        for s in config.SCENARIOS
    )


def test_a4_fx_before_earliest(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:

    first = date(2021, 9, 9)
    db_session.add(FxRate(pair="USDEUR", rate_date=first, rate=Decimal(".8")))
    db_session.add(FxRate(pair="USDJPY", rate_date=first + timedelta(days=1), rate=Decimal("110")))
    db_session.flush()
    fake = Mock(
        return_value={
            "USDEUR": [(first - timedelta(days=1), Decimal(".7")), (first, Decimal(".9"))],
            "USDJPY": [(first, Decimal("100"))],
            "USDGBP": [(first, Decimal(".6"))],
        }
    )
    monkeypatch.setattr(fx_fetcher, "_fetch_rate_history", fake)
    assert fx_fetcher.backfill_fx_rates(db_session, years=8, before_earliest=True) == 3
    assert db_session.scalar(
        select(FxRate.rate).where(FxRate.pair == "USDEUR", FxRate.rate_date == first)
    ) == Decimal(".8")
    assert (
        db_session.scalar(
            select(FxRate.rate).where(
                FxRate.pair == "USDJPY", FxRate.rate_date == first + timedelta(days=1)
            )
        )
        == 110
    )
    assert db_session.scalar(select(FxRate.rate).where(FxRate.pair == "USDCNH")) is None
    assert fake.call_args.kwargs["period"] == "8y"
    assert fx_fetcher.backfill_fx_rates(db_session, years=8) == 4
    assert db_session.scalar(
        select(FxRate.rate).where(FxRate.pair == "USDEUR", FxRate.rate_date == first)
    ) == Decimal(".9")


def test_a5_beta(db_session: Session) -> None:

    st = stress()
    days = [E - timedelta(days=60 - i) for i in range(61)]
    calendar(db_session, days)
    p = [100.0]
    own = [50.0]
    for i in range(60):
        r = 0.01 if i % 2 else -0.01
        p.append(p[-1] * (1 + r))
        own.append(own[-1] * (1 + 2 * r))
    series(db_session, "yf:SPY", days, p)
    series(db_session, "yf:AAPL", days, own)
    h = holding(db_session, auto=True)
    snap = st.compute_portfolio(db_session, TEST_USER_ID, "USD")
    assert st.holding_beta(db_session, snap.holdings[0], config.SPY) == pytest.approx((2.0, 60))
    from app.models.jade_price import JadePricePoint

    point = db_session.get(JadePricePoint, ("yf:AAPL", days[0]))
    assert point is not None
    db_session.delete(point)
    db_session.flush()
    assert st.holding_beta(db_session, snap.holdings[0], config.SPY) == (1.0, 59)
    h.ticker = "MISSING"
    db_session.flush()
    assert st.holding_beta(
        db_session, st.compute_portfolio(db_session, TEST_USER_ID, "USD").holdings[0], config.SPY
    ) == (1.0, 0)


def test_a6_forward_chain() -> None:

    st = stress()
    days = [P + timedelta(days=i) for i in range(4)]
    assert list(
        st.forward_chain(
            days,
            dict(zip(days, [100.0, 90.0, 99.0, 99.0], strict=True)),
            {days[2]: 50.0, days[3]: 55.0},
            days[2],
            1.5,
        ).values()
    ) == pytest.approx([1, 0.85, 0.9775, 1.07525])


def shock_fixture(session: Session) -> None:

    path(session)
    cached(session, "yf:AAPL", DAYS, [100.0] + [110.0] * 5 + [77.0] * 6)
    cached(session, "yf:AGG", DAYS, [100.0] + [101.0] * 5 + [103.0] * 6)
    holding(session, value="60000", auto=True)
    holding(session, value="40000", auto=True, ticker="AGG", asset_class="BOND_FUND")


def test_a7_shock_contributions(db_session: Session) -> None:

    stress()
    shock_fixture(db_session)
    out = compute(db_session)
    assert (out.shock_return, out.shock_amount, out.portfolio_value) == (
        "-0.178571",
        "-17857.10",
        "100000.00",
    )
    assert [(c.asset_class, c.contribution) for c in out.contributions] == [
        ("STOCK", "-0.186090"),
        ("BOND_FUND", "0.007519"),
    ]


@pytest.mark.parametrize(
    "kind,method,reason",
    [
        ("later", "head_proxy", None),
        ("BOXX", "head_proxy", None),
        ("fund", "head_proxy", None),
        ("bond", "cash_assumed", None),
        ("calendar", "excluded", "pending"),
        ("unvalued", "excluded", "unvalued"),
        ("cash", "cash", None),
        ("wmf", "cash_assumed", None),
        ("manual", "proxy", None),
        ("proxy_pending", "excluded", "pending"),
        ("trough", "excluded", "data_unavailable"),
    ],
)
def test_a8_classification(db_session: Session, kind: str, method: str, reason: str | None) -> None:

    stress()
    if kind == "BOXX":
        s = config.SCENARIOS[1]
        ds = [s.peak, s.trough, date(2022, 12, 28)]
        cached(db_session, "yf:SPY", ds, [100.0] * 3, s.id)
        cached(db_session, "yf:BOXX", [ds[-1]], [50.0], s.id)
        holding(db_session, auto=True, ticker="BOXX")
        out = stress().compute_stress(db_session, TEST_USER_ID, "USD", "sp500").scenarios[1]
    else:
        if kind != "proxy_pending":
            cached(db_session, "yf:SPY", DAYS, [100.0] * len(DAYS), attempted=kind != "calendar")
        if kind in ("later", "bond"):
            cached(db_session, "yf:AAPL", [], [])
        if kind == "fund":
            cached(db_session, "nav:008142", [P, T], [50.0, 55.0])
        if kind == "trough":
            cached(db_session, "yf:AAPL", [START, P], [100.0, 100.0])
        holding(
            db_session,
            value="0" if kind == "unvalued" else "100",
            auto=kind in ("later", "fund", "bond", "trough"),
            ticker=None if kind == "fund" else "AAPL",
            fund_code="008142" if kind == "fund" else None,
            asset_type="cash" if kind == "cash" else "wmf" if kind == "wmf" else "stock",
            asset_class="BOND_FUND" if kind == "bond" else "STOCK",
            currency="CNY" if kind == "bond" else "USD",
        )
        if kind == "bond":
            db_session.add_all([FxRate(pair="USDCNY", rate_date=d, rate=Decimal(7)) for d in DAYS])
            db_session.flush()
        out = compute(db_session)
    row = out.holdings[0]
    assert (row.method, row.excluded_reason) == (method, reason)
    if kind == "later":
        assert row.own_first_date is None and row.beta == "1.0000" and row.beta_samples == 0
    if kind == "BOXX":
        assert row.own_first_date == date(2022, 12, 28)
    if kind == "fund":
        assert row.own_first_date == P
    if kind == "manual":
        assert row.beta is None


def test_a9_today_weights(db_session: Session) -> None:

    stress()
    shock_fixture(db_session)
    out = compute(db_session)
    assert sorted(cast(str, h.weight) for h in out.holdings) == ["0.400000", "0.600000"]
    curves = [
        (dict(zip(DAYS, map(Decimal, [100] + [110] * 5 + [77] * 6), strict=True)), Decimal(".6")),
        (dict(zip(DAYS, map(Decimal, [100] + [101] * 5 + [103] * 6), strict=True)), Decimal(".4")),
    ]
    assert stress().weighted_path(DAYS, curves)[0] == (START, 1.0)
    assert out.points[0].portfolio == "-0.060150"


def test_a10_drawdown_points(db_session: Session) -> None:

    path(db_session)
    cached(
        db_session,
        "yf:AAPL",
        DAYS,
        [100.0, 200.0, 110.0, 110.0, 110.0, 110.0, 77.0, 77.0, 77.0, 77.0, 77.0, 77.0],
    )
    holding(db_session, auto=True)
    out = compute(db_session)
    assert (out.max_drawdown, out.max_drawdown_peak, out.max_drawdown_trough) == (
        "-0.615000",
        DAYS[1],
        T,
    )
    assert next(p for p in out.points if p.date == P).portfolio == "0.000000"


@pytest.mark.parametrize(
    "kind,expected",
    [("ok", "ok"), ("pending", "pending"), ("stale", "unavailable"), ("carried", "ok")],
)
def test_a11_benchmark_carry(db_session: Session, kind: str, expected: str) -> None:

    path(db_session)
    holding(db_session, asset_type="cash")
    before = compute(db_session)
    if kind != "pending":
        ds = (
            DAYS
            if kind == "ok"
            else [d for d in DAYS if d != P]
            if kind == "carried"
            else [d for d in DAYS if d > P]
        )
        cached(db_session, "yf:DIA", ds, [100.0] * len(ds))
    out = compute(db_session, "dow30")
    assert out.benchmark_status == expected
    assert (out.shock_return, out.max_drawdown) == (before.shock_return, before.max_drawdown)
    assert out.benchmark_shock_return == ("0.000000" if expected == "ok" else None)
    assert all(p.benchmark == ("0.000000" if expected == "ok" else None) for p in out.points)


@pytest.mark.parametrize("kind", ["no_holdings", "pending", "insufficient"])
def test_a12_statuses(db_session: Session, kind: str) -> None:

    ds = [P, T] if kind == "insufficient" else DAYS
    cached(db_session, "yf:SPY", ds, [100.0] * len(ds))
    if kind != "no_holdings":
        holding(
            db_session, auto=kind == "pending", asset_type="stock" if kind == "pending" else "cash"
        )
    out = compute(db_session)
    assert out.status == kind
    assert out.points == []
    assert out.contributions == []
    assert all(
        getattr(out, k) is None
        for k in (
            "portfolio_value",
            "shock_return",
            "shock_amount",
            "max_drawdown",
            "max_drawdown_peak",
            "max_drawdown_trough",
            "benchmark_shock_return",
            "benchmark_max_drawdown",
        )
    )
    assert out.coverage.pending_share == ("1.000000" if kind == "pending" else None)


@pytest.mark.parametrize("share,notice", [("0.66", True), ("0.65", False), ("0", False)])
def test_a13_notices(db_session: Session, share: str, notice: bool) -> None:

    path(db_session)
    if Decimal(share):
        holding(db_session, value=str(Decimal(share) * 100))
    holding(db_session, value=str((1 - Decimal(share)) * 100), asset_type="cash")
    out = compute(db_session)
    assert out.coverage.data_quality == notice
    assert out.coverage.approx_share_at_start == f"{Decimal(share):.6f}"
    assert out.proxy_understates == (Decimal(share) > 0)


def test_a14_api(app_client: TestClient, db_session: Session) -> None:

    u = db_session.get(User, TEST_USER_ID)
    assert u is not None
    u.subscription_status = "active"
    u.subscription_type = "jade"
    u.base_currency = "EUR"
    db_session.flush()
    response = app_client.get("/jade/stress")
    assert response.status_code == 200, response.text
    out = importlib.import_module("app.schemas.jade").JadeStressOut.model_validate(response.json())
    assert (out.base_currency, out.benchmark) == ("EUR", "sp500")
    assert [s.id for s in out.scenarios] == ["covid_2020", "rates_2022", "tariffs_2025"]
    assert app_client.get("/jade/stress?benchmark=invalid").status_code == 422
    u.subscription_type = "daily"
    db_session.flush()
    denied = app_client.get("/jade/stress")
    assert denied.status_code == 403 and denied.json()["detail"] == "subscription_required"
    assert not db_session.new and not db_session.dirty and not db_session.deleted


def test_a15_unchanged_tools(db_session: Session) -> None:

    days = [S, *[E - timedelta(days=i) for i in reversed(range(61))]]
    calendar(db_session, days)
    series(db_session, "yf:SPY", days, [100.0 + i for i in range(len(days))])
    series(db_session, "yf:AAPL", days[1:], [50.0 + i for i in range(61)])
    holding(db_session, auto=True)
    from app.services.jade_tail_risk import compute_tail_risk

    before = replay.compute_replay(db_session, TEST_USER_ID, "USD", "sp500", "5Y").model_dump_json()
    tail = compute_tail_risk(db_session, TEST_USER_ID, "USD", "sp500").model_dump_json()
    stress().compute_stress(db_session, TEST_USER_ID, "USD", "sp500")
    assert (
        replay.compute_replay(db_session, TEST_USER_ID, "USD", "sp500", "5Y").model_dump_json()
        == before
    )
    assert compute_tail_risk(db_session, TEST_USER_ID, "USD", "sp500").model_dump_json() == tail
