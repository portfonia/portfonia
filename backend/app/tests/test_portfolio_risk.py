"""Issue #551: risk arithmetic and read-only portfolio endpoint."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.benchmark_price import BenchmarkPrice
from app.models.holding import Holding
from app.models.portfolio_snapshot_batch import PortfolioSnapshotBatch
from app.models.portfolio_value_snapshot import PortfolioValueSnapshot
from app.models.user_investment_context import UserInvestmentContext
from app.services.portfolio_risk import (
    _beta,
    _deviation,
    _risk_label,
    _rolling_vol,
    _tier,
    compute_portfolio_risk,
)
from app.tests.conftest import TEST_USER_ID, seed_user


def test_volatility_sample_count_annualization_and_tiers() -> None:
    start = date(2026, 8, 3)
    days = [start + timedelta(days=i) for i in range(22)]
    returns = {d: Decimal("0.01") if i % 2 == 0 else Decimal("-0.01") for i, d in enumerate(days)}
    short = _rolling_vol(days[:21], returns, days[:21])
    assert short.status == "insufficient_sample"
    assert short.current is None
    assert short.points == []

    full = _rolling_vol(days, returns, days)
    assert full.status == "ok"
    assert full.current is not None
    assert full.current.quantize(Decimal("0.0001")) == Decimal("0.1625")
    assert len(full.points) == 1
    assert full.points[0].date == days[-1]
    assert [_tier(Decimal(v)) for v in ("0.0999", "0.1000", "0.1999", "0.2000")] == [
        "low",
        "medium",
        "medium",
        "high",
    ]


def test_beta_pairs_and_zero_variance() -> None:
    start = date(2026, 8, 3)
    days = [start + timedelta(days=i) for i in range(22)]
    benchmark = {d: Decimal("0.01") if i % 2 else Decimal("-0.01") for i, d in enumerate(days)}
    portfolio = {d: v * Decimal("1.5") for d, v in benchmark.items()}
    value = _beta(days, portfolio, benchmark).value
    assert value is not None
    assert value.quantize(Decimal("0.0001")) == Decimal("1.5000")
    assert _beta(days[:21], portfolio, benchmark).status == "insufficient_sample"
    assert _beta(days, portfolio, {d: Decimal("0.01") for d in days}).value is None


@pytest.mark.parametrize(
    ("appetite", "horizon", "objective", "vol", "expected"),
    [
        ("BALANCED", "LONG", "GROWTH", "0.186", "within"),
        ("AGGRESSIVE", "SHORT", "GROWTH", "0.37", "exceeds"),
        ("AGGRESSIVE", "LONG", "GROWTH", "0.37", "caution"),
        ("CONSERVATIVE", "LONG", "PRESERVATION", "0.12", "caution"),
        ("BALANCED", "LONG", "GROWTH", "0.20", "caution"),
        ("BALANCED", "LONG", "GROWTH", "0.30", "exceeds"),
    ],
)
def test_risk_thresholds(
    appetite: str, horizon: str, objective: str, vol: str, expected: str
) -> None:
    assert _risk_label(Decimal(vol), appetite, horizon, objective) == expected


def test_deviation_examples() -> None:
    from app.services.portfolio_calculator import HoldingValue

    def holding(value: str, asset_class: str, market: str) -> HoldingValue:
        import uuid

        return HoldingValue(
            holding_id=uuid.uuid4(),
            name="Example",
            ticker=None,
            fund_code=None,
            currency="USD",
            asset_type="stock",
            asset_class=asset_class,
            market=market,
            market_value=Decimal(value),
            market_value_base=Decimal(value),
            price_as_of=None,
        )

    assert (
        _deviation(
            [holding("85", "STOCK", "US"), holding("15", "CASH_EQUIV", "Other")],
            {"risk_appetite": "BALANCED", "style": "GROWTH", "markets": ["US"]},
        ).delta
        == 1
    )
    assert (
        _deviation(
            [holding("30", "STOCK", "US"), holding("70", "CASH_EQUIV", "Other")],
            {"risk_appetite": "AGGRESSIVE", "style": "GROWTH", "markets": []},
        ).delta
        == -2
    )
    assert (
        _deviation(
            [
                holding("35", "STOCK", "HK"),
                holding("15", "OTHER", "US"),
                holding("50", "BOND_FUND", "US"),
            ],
            {"risk_appetite": "CONSERVATIVE", "style": "INDEX", "markets": ["US"]},
        ).delta
        == 2
    )
    assert (
        _deviation(
            [holding("100", "ETF", "US")],
            {"risk_appetite": "CONSERVATIVE", "style": "INDEX", "markets": ["US"]},
        ).delta
        == 2
    )


def test_endpoint_auth_validation_and_empty_holdings(
    app_client: TestClient, db_session: Session
) -> None:
    seed_user(db_session, TEST_USER_ID)
    response = app_client.get("/portfolio/risk")
    assert response.status_code == 200
    data = response.json()
    assert data["portfolio_vol"]["status"] == "insufficient_sample"
    assert data["beta"]["status"] == "insufficient_sample"
    assert data["risk"]["status"] == "no_questionnaire"
    assert data["deviation"]["status"] == "no_questionnaire"
    assert data["manual_valuation_share"] is None
    assert app_client.get("/portfolio/risk", params={"benchmark": "bad"}).status_code == 422
    assert app_client.get("/portfolio/risk", params={"base_currency": "BAD"}).status_code == 422


def test_endpoint_requires_principal(app_client: TestClient) -> None:
    from app.core.deps import current_principal
    from app.main import app

    override = app.dependency_overrides.pop(current_principal)
    try:
        assert app_client.get("/portfolio/risk").status_code == 401
    finally:
        app.dependency_overrides[current_principal] = override


def test_real_postgres_nyse_sampling_carried_and_benchmark_independence(
    db_session: Session,
) -> None:
    import uuid

    seed_user(db_session, TEST_USER_ID)
    holding_id = uuid.uuid4()
    first = date(2026, 8, 3)
    calendar = [first + timedelta(days=i) for i in range(45)]
    nyse = [day for day in calendar if day.weekday() < 5 and day != date(2026, 9, 7)]
    value = Decimal("100")
    sp_price = Decimal("100")
    nyse_index = 0
    for day in calendar:
        if day in nyse:
            nyse_index += 1
            change = Decimal("0.01") if nyse_index % 2 else Decimal("-0.01")
            value *= 1 + change
            sp_price *= 1 + change / Decimal("1.5")
            db_session.add(BenchmarkPrice(index_code="sp500", price_date=day, close_price=sp_price))
        if day.weekday() < 5:
            db_session.add(
                BenchmarkPrice(
                    index_code="csi300",
                    price_date=day,
                    close_price=Decimal("100") + Decimal(day.day) / 10,
                    currency="USD",
                )
            )
        db_session.add(
            PortfolioSnapshotBatch(user_id=TEST_USER_ID, snapshot_date=day, status="complete")
        )
        db_session.add(
            PortfolioValueSnapshot(
                user_id=TEST_USER_ID,
                snapshot_date=day,
                holding_id=holding_id,
                currency="USD",
                base_currency="USD",
                shares=Decimal("1"),
                market_value=value,
                market_value_base=value,
                data_quality="approx_carried" if day == nyse[10] else "ok",
            )
        )
    db_session.flush()

    sp = compute_portfolio_risk(db_session, TEST_USER_ID, "sp500", "USD", today=calendar[-1])
    csi = compute_portfolio_risk(db_session, TEST_USER_ID, "csi300", "USD", today=calendar[-1])
    assert sp.portfolio_vol == csi.portfolio_vol
    assert sp.beta == csi.beta
    assert sp.risk == csi.risk
    assert sp.benchmark_vol != csi.benchmark_vol
    assert sp.portfolio_vol.status == "ok"
    assert sp.portfolio_vol.sample_count == len(nyse) - 2
    assert sp.beta.sample_count == len(nyse) - 2
    assert sp.portfolio_vol.window_end == nyse[-1]
    assert csi.benchmark_vol.window_end == max(day for day in calendar if day.weekday() < 5)

    manual = Holding(
        user_id=TEST_USER_ID,
        name="Manual",
        pricing_mode="manual",
        currency="USD",
        current_value=Decimal("66.00"),
        asset_type="other",
        asset_class="STOCK",
        market="US",
    )
    cash = Holding(
        user_id=TEST_USER_ID,
        name="Cash",
        pricing_mode="manual",
        currency="USD",
        current_value=Decimal("34.00"),
        asset_type="cash",
        asset_class="CASH_EQUIV",
        market="US",
    )
    db_session.add_all(
        [
            manual,
            cash,
            UserInvestmentContext(
                user_id=TEST_USER_ID,
                questionnaire={
                    "asset_scale": "100K_500K",
                    "sectors_of_interest": [],
                    "intel_focus": "MACRO",
                    "risk_appetite": "BALANCED",
                    "horizon": "LONG",
                    "objective": "GROWTH",
                    "style": "GROWTH",
                    "markets": ["US"],
                },
                questionnaire_version="v1",
            ),
        ]
    )
    db_session.flush()
    at_limit = compute_portfolio_risk(db_session, TEST_USER_ID, "sp500", "USD", today=calendar[-1])
    assert at_limit.manual_valuation_share == Decimal("0.66")
    assert at_limit.risk.status == "data_quality"
    assert at_limit.portfolio_vol.status == "ok"
    assert at_limit.beta.status == "ok"
    assert at_limit.deviation.status == "ok"
    manual.current_value = Decimal("65.99")
    cash.current_value = Decimal("34.01")
    db_session.flush()
    below_limit = compute_portfolio_risk(
        db_session, TEST_USER_ID, "sp500", "USD", today=calendar[-1]
    )
    assert below_limit.manual_valuation_share == Decimal("0.6599")
    assert below_limit.risk.status == "ok"


def test_real_postgres_benchmark_remains_visible_without_holdings(db_session: Session) -> None:
    seed_user(db_session, TEST_USER_ID)
    first = date(2026, 8, 3)
    days = [
        first + timedelta(days=i) for i in range(34) if (first + timedelta(days=i)).weekday() < 5
    ]
    for i, day in enumerate(days):
        db_session.add(
            BenchmarkPrice(
                index_code="sp500",
                price_date=day,
                close_price=Decimal("100") + (Decimal("1") if i % 2 else Decimal("0")),
            )
        )
    db_session.add(
        UserInvestmentContext(
            user_id=TEST_USER_ID,
            questionnaire={
                "asset_scale": "100K_500K",
                "sectors_of_interest": [],
                "intel_focus": "MACRO",
                "risk_appetite": "BALANCED",
                "horizon": "LONG",
                "objective": "GROWTH",
                "style": "INDEX",
                "markets": ["US"],
            },
            questionnaire_version="1",
        )
    )
    db_session.flush()
    result = compute_portfolio_risk(db_session, TEST_USER_ID, "sp500", "USD", today=days[-1])
    assert result.benchmark_vol.status == "ok"
    assert result.portfolio_vol.status == "insufficient_sample"
    assert result.beta.status == "insufficient_sample"
    assert result.risk.status == "insufficient_sample"
    assert result.deviation.status == "no_valued_holdings"
    assert result.manual_valuation_share is None
