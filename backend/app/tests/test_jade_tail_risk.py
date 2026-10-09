"""Issue #718 acceptance tests with real Postgres and cached data."""

from collections.abc import Generator, Sequence
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.user_investment_context import UserInvestmentContext
from app.schemas.jade import JadeTailRiskOut
from app.services import jade_replay as replay
from app.services import jade_tail_risk as tail
from app.services import portfolio_risk as risk
from app.tests.conftest import TEST_USER_ID
from app.tests.test_jade_replay import E, S, calendar, holding, series, setup  # noqa: F401


@pytest.fixture(autouse=True)
def no_provider(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    guard = Mock(side_effect=AssertionError("request-time provider call"))
    monkeypatch.setattr("app.services._yfinance.fetch_ohlcv_range_bounded", guard)
    monkeypatch.setattr("app.services.fund_nav_fetcher.fetch_nav_history_pages", guard)
    yield
    guard.assert_not_called()


def cached(session: Session, values: Sequence[float], benchmark_n: int | None = None) -> list[date]:
    days = [E - timedelta(days=len(values) - 1 - i) for i in range(len(values))]
    calendar(session, days)
    series(session, "yf:AAPL", [S, *days], [values[0], *values])
    holding(session, value="100000", auto=True)
    if benchmark_n is not None:
        ds = days[-benchmark_n:] if benchmark_n else []
        series(session, "yf:SPY", ds, [100 + i for i in range(len(ds))])
    session.flush()
    return days


def compute(session: Session) -> JadeTailRiskOut:
    return tail.compute_tail_risk(session, TEST_USER_ID, "USD", "sp500")


def test_a1_tail_count() -> None:
    for n, level, expected in [
        (40, 95, 2),
        (500, 99, 5),
        (1255, 95, 63),
        (1255, 99, 13),
        (5, 95, 1),
    ]:
        assert tail.tail_count(n, level) == expected


def test_a2_historical(db_session: Session) -> None:
    values = [100.0]
    for r in [-0.05, -0.03, -0.02] + [0.001] * 37:
        values.append(values[-1] * (1 + r))
    cached(db_session, values)
    out = compute(db_session)
    assert out.portfolio_value == "100000.00"
    cell = out.levels[0].daily
    assert cell is not None
    assert (cell.var, cell.cvar, cell.var_amount, cell.cvar_amount) == (
        "0.030000",
        "0.040000",
        "3000.00",
        "4000.00",
    )
    level = out.levels[1]
    assert not level.available
    assert all(
        getattr(level, k) is None
        for k in ("daily", "monthly", "normal_daily", "normal_monthly", "tail_days", "tail_windows")
    )


def test_a3_monthly(db_session: Session) -> None:
    days = cached(db_session, list(range(100, 126)))
    monthly = tail.month_returns(list(zip(days, map(float, range(100, 126)), strict=True)))
    assert list(map(replay.ratio, monthly)) == [
        "0.210000",
        "0.207921",
        "0.205882",
        "0.203883",
        "0.201923",
    ]
    out = compute(db_session)
    assert (out.month_windows, out.month_independent) == (5, 1)
    assert out.levels[0].monthly is not None
    assert out.levels[0].monthly.var == "-0.201923"


@pytest.mark.parametrize("n", [11, 21, 22])
def test_a3_short_monthly_api(app_client: TestClient, db_session: Session, n: int) -> None:
    cached(db_session, list(range(100, 100 + n)), n)
    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    user.subscription_status = "active"
    user.subscription_type = "jade"
    db_session.flush()
    response = app_client.get("/jade/tail-risk")
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["status"] == "ok"
    level = out["levels"][0]
    assert level["normal_monthly"] is None
    assert (level["monthly"] is None) == (n <= 21)
    assert (level["benchmark_monthly"] is None) == (n <= 21)
    assert level["tail_windows"] == (1 if n == 22 else None)
    if n == 22:
        assert level["monthly"]["var"] == "-0.210000"


def test_a4_normal() -> None:
    for level, expected in [(95, ("0.030031", "0.037660")), (99, ("0.042473", "0.048660"))]:
        assert tuple(map(replay.ratio, tail.normal([0.01, -0.01, 0.02, -0.02], level))) == expected


@pytest.mark.parametrize(
    "answers,expected",
    [
        (
            {"risk_appetite": "BALANCED", "horizon": "SHORT", "objective": "GROWTH"},
            [("0.025904", "0.118707"), ("0.036637", "0.167890")],
        ),
        (
            {"risk_appetite": "CONSERVATIVE", "horizon": "MEDIUM", "objective": "GROWTH"},
            [("0.020723", "0.094966"), ("0.029309", "0.134312")],
        ),
        (None, None),
        ({"risk_appetite": "BALANCED", "objective": "GROWTH"}, None),
    ],
)
def test_a5_reference(
    db_session: Session,
    answers: dict[str, object] | None,
    expected: list[tuple[str | None, str | None]] | None,
) -> None:
    cached(db_session, list(range(100, 126)))
    if answers is not None:
        from sqlalchemy.orm.attributes import set_committed_value

        valid = {
            "asset_scale": "100K_500K",
            "sectors_of_interest": [],
            "intel_focus": "MACRO",
            "style": "GROWTH",
            "markets": ["US"],
            "horizon": "LONG",
            **answers,
        }
        context = UserInvestmentContext(
            user_id=TEST_USER_ID, questionnaire=valid, questionnaire_version="v1"
        )
        db_session.add(context)
        db_session.flush()
        # Simulate a malformed legacy loaded value without weakening DB constraints.
        set_committed_value(context, "questionnaire", answers)
    out = compute(db_session)
    assert out.tolerance_status == ("ok" if expected else "no_questionnaire")
    assert [(item.reference_daily, item.reference_monthly) for item in out.levels] == (
        expected or [(None, None)] * 2
    )
    assert not out.levels[1].available


def test_a6_histogram() -> None:
    bins = tail.histogram([-0.012, -0.0049, 0.0, 0.0051])
    assert [(b.lower, b.upper, b.count) for b in bins] == [
        ("-0.015000", "-0.010000", 1),
        ("-0.010000", "-0.005000", 0),
        ("-0.005000", "0.000000", 1),
        ("0.000000", "0.005000", 1),
        ("0.005000", "0.010000", 1),
    ]


def test_a7_fixed_sample(db_session: Session) -> None:
    cached(db_session, list(range(100, 601)))
    out = compute(db_session)
    five = replay.compute_replay(db_session, TEST_USER_ID, "USD", "sp500", "5Y")
    assert out.window_start == S and out.window_end == E
    assert out.sample_count == five.sample_count == 500
    assert (
        replay.compute_replay(db_session, TEST_USER_ID, "USD", "sp500", "1M").sample_count
        < out.sample_count
    )


@pytest.mark.parametrize("n", [499, 500, 501])
def test_a8_gate(db_session: Session, n: int) -> None:
    cached(db_session, list(range(100, 101 + n)))
    out = compute(db_session)
    assert out.levels[1].available == (n >= 500)
    assert out.levels[1].tail_days == (tail.tail_count(n, 99) if n >= 500 else None)


@pytest.mark.parametrize("benchmark_n", [None, 0, 11, 22, 499, 501])
def test_a9_benchmark(db_session: Session, benchmark_n: int | None) -> None:
    cached(db_session, list(range(100, 601)), benchmark_n)
    out = compute(db_session)
    assert out.benchmark_status == (
        "pending" if benchmark_n is None else "unavailable" if benchmark_n == 0 else "ok"
    )
    assert (out.levels[0].benchmark_daily is not None) == (
        benchmark_n is not None and benchmark_n > 0
    )
    assert (out.levels[0].benchmark_monthly is not None) == (
        benchmark_n is not None and benchmark_n > 21
    )
    assert (out.levels[1].benchmark_daily is not None) == (benchmark_n == 501)
    assert (out.levels[1].benchmark_monthly is not None) == (benchmark_n == 501)
    for level in out.levels:
        for cell in (level.benchmark_daily, level.benchmark_monthly):
            if cell:
                assert cell.var_amount is None and cell.cvar_amount is None


@pytest.mark.parametrize("kind", ["no_holdings", "pending", "insufficient"])
def test_a10_statuses(db_session: Session, kind: str) -> None:
    calendar(db_session, [E - timedelta(days=i) for i in reversed(range(10))])
    if kind != "no_holdings":
        holding(
            db_session,
            asset_type="cash" if kind == "insufficient" else "stock",
            auto=kind == "pending",
        )
    out = compute(db_session)
    assert out.status == kind
    assert out.levels == [] and out.histogram == [] and out.portfolio_value is None
    assert out.month_windows == out.month_independent == 0
    assert (
        out.coverage
        == replay.compute_replay(db_session, TEST_USER_ID, "USD", "sp500", "5Y").coverage
    )


@pytest.mark.parametrize("method", ["own", "cash", "proxy", "head_proxy"])
def test_a11_proxy_notice(db_session: Session, method: str) -> None:
    days = [S, *[E - timedelta(days=i) for i in reversed(range(26))]]
    calendar(db_session, days)
    series(db_session, "yf:SPY", days, list(range(100, 100 + len(days))))
    if method in ("own", "head_proxy"):
        ds = days if method == "own" else days[1:]
        series(db_session, "yf:AAPL", ds, list(range(100, 100 + len(ds))))
    holding(
        db_session,
        asset_type="cash" if method == "cash" else "stock",
        auto=method in ("own", "head_proxy"),
    )
    out = compute(db_session)
    assert out.proxy_understates == (method in ("proxy", "head_proxy"))


def test_a12_api(app_client: TestClient, db_session: Session) -> None:
    calendar(db_session, [E])
    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    user.subscription_status = "active"
    user.subscription_type = "jade"
    user.base_currency = "EUR"
    db_session.flush()
    response = app_client.get("/jade/tail-risk")
    assert response.status_code == 200, response.text
    out = JadeTailRiskOut.model_validate(response.json())
    assert out.base_currency == "EUR" and out.benchmark == "sp500"
    assert app_client.get("/jade/tail-risk?benchmark=invalid").status_code == 422
    user.subscription_type = "daily"
    db_session.flush()
    forbidden = app_client.get("/jade/tail-risk")
    assert forbidden.status_code == 403 and forbidden.json()["detail"] == "subscription_required"
    assert not db_session.new and not db_session.dirty and not db_session.deleted


def test_a13_refactors(db_session: Session) -> None:
    for appetite, plain, shift in [
        ("CONSERVATIVE", (".10", ".20"), (".10", ".20")),
        ("BALANCED", (".20", ".30"), (".15", ".25")),
        ("AGGRESSIVE", (".30", ".40"), (".25", ".35")),
    ]:
        assert risk.risk_thresholds(appetite, "LONG", "GROWTH") == tuple(map(Decimal, plain))
        assert risk.risk_thresholds(appetite, "SHORT", "GROWTH") == tuple(map(Decimal, shift))
        assert risk.risk_thresholds(appetite, "LONG", "PRESERVATION") == tuple(map(Decimal, shift))
    cached(db_session, list(range(100, 126)))
    build = replay.build_replay(db_session, TEST_USER_ID, "USD", "sp500", "5Y")
    assert (
        build.out.model_dump_json()
        == replay.compute_replay(db_session, TEST_USER_ID, "USD", "sp500", "5Y").model_dump_json()
    )
