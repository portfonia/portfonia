"""Integration tests for /portfolio endpoints."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.fx_rate import FxRate
from app.models.holding import Holding

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


# POST /refresh moved to POST /admin/portfolio/refresh (issue #128 Ring 1
# stage B, checkpoint B2, decision point 8/11) — an ordinary user must not be
# able to trigger a global market-data refresh. See test_admin_router.py for
# the orchestration test and test_old_portfolio_refresh_route_removed for the
# 404 assertion on this old path.
