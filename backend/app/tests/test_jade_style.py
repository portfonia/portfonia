"""Issue #727 acceptance tests; cached fixtures on real Postgres, no providers."""

import importlib
from datetime import date, timedelta
from decimal import Decimal
from itertools import combinations
from types import ModuleType
from unittest.mock import Mock

import numpy as np
import pytest
from fastapi.testclient import TestClient
from numpy.typing import NDArray
from sqlalchemy.orm import Session

from app.models.fx_rate import FxRate
from app.models.jade_price import JadePriceSeries
from app.models.user import User
from app.schemas.jade import JadeStyleOut
from app.services import (
    jade_price_history as history,
)
from app.services import (
    jade_replay as replay,
)
from app.services import (
    jade_replay_config as config,
)
from app.services.jade_substitutes import substitute_keys
from app.tests.conftest import TEST_USER_ID, seed_user
from app.tests.test_jade_replay import E, calendar, holding, series

SYMBOLS = [
    "IWF",
    "IWD",
    "IWM",
    "EFA",
    "EEM",
    "2800.HK",
    "510300.SS",
    "AGG",
    "TLT",
    "GLD",
    "DBC",
    "VNQ",
    "BIL",
]
NEW = {"IWF", "IWD", "IWM", "TLT", "BIL"}
X = np.array(
    [
        [0.010, 0.002, -0.004],
        [-0.020, 0.001, 0.006],
        [0.015, -0.001, 0.002],
        [-0.005, 0.003, -0.003],
        [0.012, 0, 0.001],
        [-0.008, 0.002, 0.004],
    ]
)
Y = np.array([0.007, -0.012, 0.010, -0.002, 0.008, -0.004])
CORNER = np.array([0.013, -0.025, 0.019, -0.007, 0.015, -0.011])


def module() -> ModuleType:
    return importlib.import_module("app.services.jade_style")


@pytest.fixture(autouse=True)
def setup(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_user(db_session, TEST_USER_ID)
    monkeypatch.setattr(replay, "today_et", lambda: E)
    monkeypatch.setattr(
        history, "fetch_ohlcv_range_bounded", Mock(side_effect=AssertionError("provider call"))
    )
    monkeypatch.setattr(
        history, "fetch_nav_history_pages", Mock(side_effect=AssertionError("provider call"))
    )


def fixture(
    session: Session, n: int = 42, kind: str = "own", benchmark: str = "ok", basis: str = "ok"
) -> tuple[list[date], NDArray[np.float64]]:
    days = [E - timedelta(days=n + 2 - i) for i in range(n + 3)]
    calendar(session, days)
    rng = np.random.default_rng(727)
    values = 100 * np.vstack(
        [np.ones(13), np.cumprod(1 + rng.normal(0.0002, 0.01, (len(days) - 1, 13)), axis=0)]
    )
    for j, s in enumerate(SYMBOLS):
        if s == "TLT" and basis == "missing":
            continue
        series(
            session,
            "yf:" + s,
            days,
            values[:, j].tolist(),
            attempted=not (s == "TLT" and basis == "unattempted"),
        )
    for pair, rate in [("USDHKD", 7.8), ("USDCNY", 7.0)]:
        session.add_all([FxRate(pair=pair, rate_date=d, rate=Decimal(str(rate))) for d in days])
    if benchmark != "pending":
        bd = days[-44:] if benchmark == "short" else days
        if benchmark == "unavailable":
            series(session, "yf:SPY", [], [])
        else:
            series(session, "yf:SPY", bd, values[-len(bd) :, 0].tolist())
    if kind == "own":
        series(
            session, "yf:AAPL", [E - timedelta(days=100), *days], [100.0, *values[:, 0].tolist()]
        )
        holding(session, auto=True)
    elif kind == "proxy":
        holding(session)
    elif kind == "head":
        series(session, "yf:AAPL", days[12:], values[12:, 0].tolist())
        holding(session, auto=True)
    elif kind == "cash":
        holding(session, asset_type="cash", asset_class="CASH_EQUIV")
    elif kind == "pending":
        holding(session, auto=True)
    session.flush()
    return days, values


def compute(session: Session) -> JadeStyleOut:
    result: JadeStyleOut = module().compute_style(session, TEST_USER_ID, "USD", "sp500")
    return result


def test_b1_constants() -> None:
    assert [e.symbol for e in config.STYLE_BASIS] == SYMBOLS
    assert frozenset("yf:" + s for s in SYMBOLS) == config.STYLE_KEYS
    assert (
        config.STYLE_RANGE,
        config.STYLE_HORIZON_DAYS,
        config.STYLE_MIN_RETURNS,
        config.STYLE_LOW_FIT,
        config.STYLE_SOLVER_TOL,
    ) == ("3M", 3, 42, 0.6, 1e-12)
    assert not NEW & config.FIXED_ETF_SYMBOLS


def test_b2_fill(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    u = db_session.get(User, TEST_USER_ID)
    assert u
    u.subscription_status = "active"
    u.subscription_type = "jade"
    holding(db_session, auto=True, ticker="0700.HK", market="HK")
    batch = Mock(
        side_effect=lambda symbols, start, end: {
            s: [(E, 100.0, 100.0, 100.0, 100.0, 1)] for s in symbols
        }
    )
    fill = Mock()
    monkeypatch.setattr(history, "fetch_ohlcv_range_bounded", batch)
    monkeypatch.setattr(history, "fill_scenarios", fill)
    history.refresh_jade_price_history(db_session, E)
    assert set(batch.call_args.args[0]) == config.FIXED_ETF_SYMBOLS | NEW | {"0700.HK"}
    for s in NEW:
        row = db_session.get(JadePriceSeries, "yf:" + s)
        assert row and row.last_success_on == E
    fill.assert_called_once_with(
        db_session,
        E,
        {"yf:" + s for s in config.FIXED_ETF_SYMBOLS} | {"yf:0700.HK"} | substitute_keys(),
    )


@pytest.mark.parametrize("skipped", [False, True], ids=["consecutive", "skipped-date"])
def test_b3_overlapping(skipped: bool) -> None:
    dates = [E + timedelta(days=i + (2 if skipped and i >= 3 else 0)) for i in range(6)]
    assert [
        replay.ratio(r)
        for r in module().overlapping_returns(
            list(zip(dates, [100.0, 101.0, 99.0, 102.0, 103.0, 101.0], strict=True))
        )
    ] == ["0.020000", "0.019802", "0.020202"]


@pytest.mark.parametrize("corner", [False, True], ids=["interior", "corner"])
def test_b4_worked_weights(corner: bool) -> None:
    y = CORNER if corner else Y
    w = module().constrained_weights(X, y)
    assert [replay.ratio(float(v)) for v in w] == (
        ["1.000000", "0.000000", "0.000000"] if corner else ["0.643996", "0.303214", "0.052790"]
    )
    if corner:
        g = X.T @ X @ w - X.T @ y
        assert np.all((g - g[0])[1:] > 0)
        assert (g - g[0])[1:] == pytest.approx([0.000275, 0.000297], abs=1e-12)


def oracle(x: NDArray[np.float64], y: NDArray[np.float64]) -> NDArray[np.float64]:
    q = x.T @ x
    c = x.T @ y
    k = x.shape[1]
    best = None
    loss = float("inf")
    for size in range(1, k + 1):
        for support in combinations(range(k), size):
            ix = list(support)
            a = np.block(
                [[q[np.ix_(ix, ix)], np.ones((size, 1))], [np.ones((1, size)), np.zeros((1, 1))]]
            )
            z = np.linalg.solve(a, np.r_[c[ix], 1])[:size]
            if min(z) < 0:
                continue
            w = np.zeros(k)
            w[ix] = z
            candidate = float(np.sum((y - x @ w) ** 2))
            if candidate < loss:
                loss = candidate
                best = w
    assert best is not None
    return best


@pytest.mark.parametrize("k", [3, 8, 13])
@pytest.mark.parametrize("boundary", [False, True], ids=["interior", "several-zeros"])
def test_b5_oracle(k: int, boundary: bool) -> None:
    rng = np.random.default_rng(727 + k)
    x = rng.normal(0, 0.01, (60, k))
    w0 = np.ones(k) / k if not boundary else np.r_[1.2, -0.2, np.zeros(k - 2)]
    y = x @ w0 + (rng.normal(0, 0.00001, 60) if not boundary else 0)
    w = module().constrained_weights(x, y)
    np.testing.assert_allclose(w, oracle(x, y), atol=1e-9, rtol=0)
    assert abs(sum(w) - 1) <= 1e-10 and np.all(w >= 0)
    g = x.T @ x @ w - x.T @ y
    support = w > 1e-10
    nu = -g[support][0]
    assert np.all(np.abs((g + nu)[support]) <= 1e-10)
    assert np.all((g + nu)[~support] >= -1e-10)
    if boundary:
        assert np.count_nonzero(w <= 1e-10) >= k - 1


@pytest.mark.parametrize(
    "y,r2,vol",
    [(Y, "0.999710", "0.001340"), (CORNER, "0.953468", "0.034779")],
    ids=["interior", "corner"],
)
def test_b6_worked_fit(y: NDArray[np.float64], r2: str, vol: str) -> None:
    result = module().fit(X, y)
    assert result.r_squared == r2 and result.residual_vol == vol


def test_b6_zero_variance() -> None:
    result = module().fit(X, np.zeros(6))
    assert result.r_squared is None and not result.low_fit
    assert result.residual_vol is not None and len(result.weights) == 3


def test_b6_negative_r_squared() -> None:
    result = module().fit(X, -X[:, 1] / 100)
    assert float(result.r_squared) < 0


@pytest.mark.parametrize("r2,expected", [(0.59, True), (0.60, False), (None, False)])
def test_b8_low_fit(r2: float | None, expected: bool) -> None:
    assert module().low_fit(r2) is expected


@pytest.mark.parametrize(
    "kind,basis,n,expected",
    [
        ("none", "ok", 42, "no_holdings"),
        ("pending", "ok", 42, "pending"),
        ("own", "missing", 42, "pending"),
        ("own", "unattempted", 42, "pending"),
        ("own", "ok", 41, "insufficient"),
        ("own", "ok", 42, "ok"),
        ("own", "ok", 5, "insufficient"),
    ],
    ids=[
        "no-holdings",
        "replay-pending",
        "basis-missing",
        "basis-unattempted",
        "41-samples",
        "42-samples",
        "replay-insufficient",
    ],
)
def test_b7_status(db_session: Session, kind: str, basis: str, n: int, expected: str) -> None:
    fixture(db_session, n, kind, basis=basis)
    out = compute(db_session)
    assert out.status == expected
    assert out.sample_count == (n if expected not in ("no_holdings", "pending") else 0)
    if expected != "ok":
        assert out.portfolio is None and out.benchmark_fit is None and out.points == []


@pytest.mark.parametrize(
    "bench,expected",
    [
        ("pending", "pending"),
        ("unavailable", "unavailable"),
        ("ok", "ok"),
        ("short", "unavailable"),
    ],
)
def test_b9_benchmark(db_session: Session, bench: str, expected: str) -> None:
    fixture(db_session, benchmark=bench)
    out = compute(db_session)
    assert out.status == "ok" and out.benchmark_status == expected
    assert (out.benchmark_fit is not None) == (expected == "ok")


@pytest.mark.parametrize("bench", ["pending", "unavailable", "ok"])
def test_b9_non_ok_copies_benchmark(db_session: Session, bench: str) -> None:
    fixture(db_session, 41, benchmark=bench)
    b = replay.build_replay(db_session, TEST_USER_ID, "USD", "sp500", "3M")
    out = compute(db_session)
    assert out.status == "insufficient" and out.benchmark_fit is None
    assert out.benchmark_status == b.out.benchmark_status


@pytest.mark.parametrize("kind,flag", [("own", False), ("proxy", True), ("head", True)])
def test_b10_response(db_session: Session, kind: str, flag: bool) -> None:
    days, values = fixture(db_session, kind=kind)
    out = compute(db_session)
    assert out.status == "ok" and out.portfolio is not None
    assert [w.symbol for w in out.portfolio.weights] == SYMBOLS
    assert [p.date for p in out.points] == days
    assert out.points[0].portfolio == out.points[0].style_mix == "0.000000"
    b = replay.build_replay(db_session, TEST_USER_ID, "USD", "sp500", "3M")
    assert out.coverage == b.out.coverage and out.proxy_inflates_fit is flag
    assert (out.horizon_days, out.min_samples, out.first_valid_date) == (3, 42, days[0])
    # Recompute using unrounded solved weights; HK/CNY prices converted at constant FX.
    normalized = values / values[0, :]
    p = np.array([v for _, v in b.portfolio])
    h = 3
    x = normalized[h:] / normalized[:-h] - 1
    y = p[h:] / p[:-h] - 1
    w = module().constrained_weights(x, y)
    for i, point in enumerate(out.points):
        assert point.style_mix == replay.ratio(float(normalized[i] @ w - 1))
        assert point.portfolio == replay.ratio(float(p[i] / p[0] - 1))


@pytest.mark.parametrize("plan,code", [("jade", 200), ("daily", 403), ("weekly", 403)])
def test_b11_access(app_client: TestClient, db_session: Session, plan: str, code: int) -> None:
    fixture(db_session)
    u = db_session.get(User, TEST_USER_ID)
    assert u
    u.subscription_status = "active"
    u.subscription_type = plan
    u.base_currency = "EUR"
    db_session.add_all(
        [
            FxRate(pair="USDEUR", rate_date=d, rate=Decimal("0.9"))
            for d, _ in replay.build_replay(
                db_session, TEST_USER_ID, "USD", "sp500", "3M"
            ).portfolio
        ]
    )
    db_session.flush()
    out = app_client.get("/jade/style")
    assert out.status_code == code, out.text
    if code == 200:
        schema = importlib.import_module("app.schemas.jade").JadeStyleOut
        validated = schema.model_validate(out.json())
        assert (validated.base_currency, validated.benchmark) == ("EUR", "sp500")
        assert validated.status == "ok"
    else:
        assert out.json()["detail"] == "subscription_required"
    assert not db_session.new and not db_session.dirty and not db_session.deleted


def test_b11_invalid_benchmark(app_client: TestClient, db_session: Session) -> None:
    u = db_session.get(User, TEST_USER_ID)
    assert u
    u.subscription_status = "active"
    u.subscription_type = "jade"
    db_session.flush()
    assert app_client.get("/jade/style?benchmark=invalid").status_code == 422


def test_b5_sparse_support_13() -> None:
    rng = np.random.default_rng(740)
    x = rng.normal(0, 0.01, (60, 13))
    y = x @ np.r_[0.6, 0.4, np.zeros(11)] + rng.normal(0, 0.00001, 60)
    w = module().constrained_weights(x, y)
    np.testing.assert_allclose(w, oracle(x, y), atol=1e-9, rtol=0)
    assert np.count_nonzero(w <= 1e-10) >= 2
    assert abs(sum(w) - 1) <= 1e-10
    assert np.all(w >= 0)
    gradient = x.T @ x @ w - x.T @ y
    support = w > 1e-10
    multipliers = gradient - gradient[support][0]
    assert np.all(np.abs(multipliers[support]) <= 1e-10)
    assert np.all(multipliers[~support] >= -1e-10)
