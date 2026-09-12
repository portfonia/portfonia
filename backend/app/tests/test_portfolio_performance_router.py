"""Smoke test for GET /portfolio/performance (issue #360 Phase 1)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.portfolio_snapshot_batch import PortfolioSnapshotBatch
from app.models.portfolio_value_snapshot import PortfolioValueSnapshot
from app.tests.conftest import TEST_USER_ID, seed_user

D1 = date(2026, 8, 1)


def test_get_portfolio_performance_returns_expected_shape(
    app_client: TestClient, db_session: Session
) -> None:
    seed_user(db_session, TEST_USER_ID)
    holding_id = uuid.uuid4()
    db_session.add(
        PortfolioSnapshotBatch(user_id=TEST_USER_ID, snapshot_date=D1, status="complete")
    )
    db_session.add(
        PortfolioValueSnapshot(
            user_id=TEST_USER_ID,
            snapshot_date=D1,
            holding_id=holding_id,
            currency="USD",
            base_currency="USD",
            shares=Decimal("1"),
            market_value_base=Decimal("100"),
        )
    )
    db_session.flush()

    resp = app_client.get("/portfolio/performance", params={"range": "ALL"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["portfolio"]["empty"] is False
    assert body["header"]["label"] == "market_value_change"
    assert body["meta"]["range"] == "ALL"
    assert body["benchmarks"] == []


def test_get_portfolio_performance_serializes_nullable_benchmark_points(
    app_client: TestClient, db_session: Session
) -> None:
    from app.models.benchmark_price import BenchmarkPrice

    seed_user(db_session, TEST_USER_ID)
    holding_id = uuid.uuid4()
    db_session.add(
        PortfolioSnapshotBatch(user_id=TEST_USER_ID, snapshot_date=D1, status="complete")
    )
    db_session.add(
        PortfolioValueSnapshot(
            user_id=TEST_USER_ID,
            snapshot_date=D1,
            holding_id=holding_id,
            currency="USD",
            base_currency="USD",
            shares=Decimal("1"),
            market_value_base=Decimal("100"),
        )
    )
    db_session.add(
        BenchmarkPrice(
            index_code="sp500",
            price_date=date(2026, 7, 31),
            close_price=Decimal("100"),
        )
    )
    db_session.flush()

    resp = app_client.get("/portfolio/performance", params={"range": "ALL", "benchmarks": "sp500"})
    assert resp.status_code == 200
    series = resp.json()["benchmarks"][0]
    assert series["displayable"] is True
    assert series["normalization"] == "portfolio_start"
    assert series["comparison_status"] == "baseline_only"
    assert series["comparison_return_pct"] == "0.0000"
    point = next(p for p in series["points"] if p["date"] == "2026-08-01")
    assert point["return_pct_cumulative"] == "0.0000"
    assert point["price_as_of"] == "2026-07-31"
    assert point["carried"] is True
    assert point["fx_as_of"] == {}
    assert point["unavailable_reason"] is None


def test_get_portfolio_performance_accepts_csi300(
    app_client: TestClient, db_session: Session
) -> None:
    from app.models.benchmark_price import BenchmarkPrice
    from app.models.fx_rate import FxRate

    seed_user(db_session, TEST_USER_ID)
    holding_id = uuid.uuid4()
    db_session.add(
        PortfolioSnapshotBatch(user_id=TEST_USER_ID, snapshot_date=D1, status="complete")
    )
    db_session.add(
        PortfolioValueSnapshot(
            user_id=TEST_USER_ID,
            snapshot_date=D1,
            holding_id=holding_id,
            currency="USD",
            base_currency="USD",
            shares=Decimal("1"),
            market_value_base=Decimal("100"),
        )
    )
    db_session.add(
        BenchmarkPrice(
            index_code="csi300",
            price_date=date(2026, 7, 31),
            close_price=Decimal("4500"),
            currency="CNY",
        )
    )
    db_session.add(FxRate(pair="USDCNY", rate_date=date(2026, 7, 31), rate=Decimal("7.2")))
    db_session.flush()

    resp = app_client.get("/portfolio/performance", params={"range": "ALL", "benchmarks": "csi300"})
    assert resp.status_code == 200
    series = resp.json()["benchmarks"][0]
    assert series["index_code"] == "csi300"
    assert series["displayable"] is True


def test_get_portfolio_performance_empty_book_returns_empty_flag(
    app_client: TestClient, db_session: Session
) -> None:
    seed_user(db_session, TEST_USER_ID)
    resp = app_client.get("/portfolio/performance", params={"range": "1Y"})
    assert resp.status_code == 200
    assert resp.json()["portfolio"]["empty"] is True


def test_get_portfolio_performance_serializes_allocation_and_monthly(
    app_client: TestClient, db_session: Session
) -> None:
    """Issue #433 contract serialization: `allocation`/`monthly_performance`
    are always present, additive fields; default `monthly_benchmark` is
    `sp500` and is independent of the (here, empty) `benchmarks` multi-select."""
    seed_user(db_session, TEST_USER_ID)
    holding_id = uuid.uuid4()
    db_session.add(
        PortfolioSnapshotBatch(user_id=TEST_USER_ID, snapshot_date=D1, status="complete")
    )
    db_session.add(
        PortfolioValueSnapshot(
            user_id=TEST_USER_ID,
            snapshot_date=D1,
            holding_id=holding_id,
            currency="USD",
            base_currency="USD",
            shares=Decimal("1"),
            market_value_base=Decimal("100"),
            asset_class="STOCK",
        )
    )
    db_session.flush()

    resp = app_client.get("/portfolio/performance", params={"range": "ALL"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["allocation"]["asset_classes"] == ["STOCK"]
    allocation_point = body["allocation"]["points"][0]
    assert allocation_point["date"] == "2026-08-01"
    assert allocation_point["weights"] == {"STOCK": "1.0000"}
    assert allocation_point["is_incomplete"] is False
    assert allocation_point["excluded_holding_count"] == 0

    assert body["monthly_performance"]["method"] == "approx_eod_twr"
    assert body["monthly_performance"]["benchmark_code"] == "sp500"
    monthly_point = body["monthly_performance"]["points"][0]
    assert monthly_point["month"] == "2026-08"
    assert monthly_point["portfolio_return_pct"] == "0.0000"
    assert monthly_point["partial_reason"] in ("tracking_start", "month_to_date")


def test_get_portfolio_performance_monthly_benchmark_out_of_catalog_is_422(
    app_client: TestClient, db_session: Session
) -> None:
    seed_user(db_session, TEST_USER_ID)
    resp = app_client.get(
        "/portfolio/performance",
        params={"range": "1M", "monthly_benchmark": "a50"},
    )
    assert resp.status_code == 422
