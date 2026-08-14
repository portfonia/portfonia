"""Integration tests for /portfolio endpoints."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.fx_rate import FxRate
from app.models.holding import Holding
from app.services.fund_nav_fetcher import FundNavFetchResult
from app.services.price_fetcher import PriceFetchResult

_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")
_FX_DATE = date(2026, 1, 2)


def _seed(db_session: Session) -> None:
    db_session.add_all(
        [
            FxRate(pair="USDCNY", rate=Decimal("7.0"), rate_date=_FX_DATE, source="test"),
            FxRate(pair="USDHKD", rate=Decimal("8.0"), rate_date=_FX_DATE, source="test"),
            Holding(
                user_id=_USER,
                name="Apple",
                pricing_mode="auto",
                ticker="AAPL",
                currency="USD",
                shares=Decimal("10"),
                market_price=Decimal("300"),
                asset_type="stock",
                sector="Technology",
            ),
        ]
    )
    db_session.flush()


def test_summary_returns_distributions(app_client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    resp = app_client.get("/portfolio/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["base_currency"] == "USD"
    assert body["fx_date"] == "2026-01-02"
    assert body["total_base"] == "3000.00"
    assert body["by_sector"] == {"Technology": "3000.00"}
    assert body["concentration"]["top_holding_name"] == "Apple"
    assert body["concentration"]["single_holding_high"] is True


def test_summary_base_currency_cny(app_client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    resp = app_client.get("/portfolio/summary?base_currency=CNY")
    assert resp.status_code == 200
    assert resp.json()["total_base"] == "21000.00"


def test_summary_rejects_invalid_currency(app_client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    resp = app_client.get("/portfolio/summary?base_currency=EUR")
    assert resp.status_code == 422


def test_refresh_orchestrates_all_fetchers(app_client: TestClient, db_session: Session) -> None:
    _seed(db_session)

    from app.services import fx_fetcher
    from app.services.fx_fetcher import FxFetchResult

    with (
        patch(
            "app.routers.portfolio.price_fetcher.update_holding_prices",
            return_value=PriceFetchResult(updated=1, failed=[]),
        ),
        patch("app.routers.portfolio.price_fetcher.backfill_sectors", return_value=2),
        patch(
            "app.routers.portfolio.update_fund_navs",
            return_value=FundNavFetchResult(updated=1, failed=["999"]),
        ),
        patch.object(
            fx_fetcher, "update_fx_rates", return_value=FxFetchResult(upserted=2, failed=[])
        ),
    ):
        resp = app_client.post("/portfolio/refresh")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "prices_updated": 1,
        "prices_failed": [],
        "sectors_backfilled": 2,
        "funds_updated": 1,
        "funds_failed": ["999"],
        "fx_upserted": 2,
        "fx_failed": [],
    }
