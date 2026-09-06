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


def test_get_portfolio_performance_empty_book_returns_empty_flag(
    app_client: TestClient, db_session: Session
) -> None:
    seed_user(db_session, TEST_USER_ID)
    resp = app_client.get("/portfolio/performance", params={"range": "1Y"})
    assert resp.status_code == 200
    assert resp.json()["portfolio"]["empty"] is True
