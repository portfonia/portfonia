"""Ops-triggered snapshot recovery + the daily task's catch-up pass (issue #373)."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.models.portfolio_value_snapshot import PortfolioValueSnapshot
from app.models.price_snapshot import PriceSnapshot
from app.services.portfolio_history import stage_user_snapshot
from app.services.snapshot_recovery import (
    CATCHUP_LOOKBACK_DAYS,
    MAX_RECOVERY_WINDOW_DAYS,
    SnapshotRecoveryReport,
)
from app.tasks.capture_tasks import capture_portfolio_value_snapshot_task
from app.tests.conftest import seed_user
from app.tests.test_admin_router import _headers

TODAY = date(2026, 9, 9)


def _seed_holding(session: Session, user_id: uuid.UUID, ticker: str, shares: Decimal) -> None:
    session.add(
        Holding(
            user_id=user_id,
            name=ticker,
            ticker=ticker,
            currency="USD",
            pricing_mode="auto",
            shares=shares,
        )
    )
    session.add(
        PriceSnapshot(
            ticker=ticker,
            market="US",
            session_node="close",
            trade_date=TODAY,
            close=Decimal("10"),
        )
    )


def test_recover_endpoint_requires_ops_token(app_client: TestClient) -> None:
    resp = app_client.post("/admin/portfolio/snapshots/recover")

    assert resp.status_code == 401


def test_recover_endpoint_replays_a_frozen_day(app_client: TestClient, db_session: Session) -> None:
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    _seed_holding(db_session, user_id, "AAPL", Decimal("2"))
    db_session.flush()
    assert stage_user_snapshot(db_session, user_id, TODAY) == (1, "computed")
    db_session.commit()

    resp = app_client.post(
        "/admin/portfolio/snapshots/recover",
        headers=_headers(),
        params={"start_date": TODAY.isoformat(), "end_date": TODAY.isoformat()},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["replayed"] == 1
    assert body["failed"] == 0
    assert body["dates"] == [TODAY.isoformat()]
    rows = (
        db_session.execute(
            select(PortfolioValueSnapshot).where(PortfolioValueSnapshot.user_id == user_id)
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


def test_recover_endpoint_rejects_an_over_long_window(
    app_client: TestClient, db_session: Session
) -> None:
    start = date(2026, 1, 1)
    end = start + timedelta(days=MAX_RECOVERY_WINDOW_DAYS + 1)

    resp = app_client.post(
        "/admin/portfolio/snapshots/recover",
        headers=_headers(),
        params={"start_date": start.isoformat(), "end_date": end.isoformat()},
    )

    assert resp.status_code == 400


@patch("app.services.snapshot_recovery.recover_portfolio_snapshots")
@patch("app.services.portfolio_history.capture_portfolio_value_snapshot")
@patch("app.core.database.SessionLocal")
def test_daily_task_runs_a_bounded_catchup_after_the_capture(
    mock_session_local: MagicMock,
    mock_capture: MagicMock,
    mock_recover: MagicMock,
) -> None:
    """The daily run now covers a recent missed day instead of only capturing
    its own date — the catch-up window is bounded by `CATCHUP_LOOKBACK_DAYS`."""
    mock_session_local.return_value = MagicMock()
    mock_capture.return_value = {"users": 1, "written": 2, "complete": 1, "skipped_deps": 0}
    mock_recover.return_value = SnapshotRecoveryReport(replayed=2, recomputed=1)

    result = capture_portfolio_value_snapshot_task.run()

    assert result["recovered_replayed"] == 2
    assert result["recovered_recomputed"] == 1
    assert result["complete"] == 1
    window = mock_recover.call_args.kwargs
    assert (window["end_date"] - window["start_date"]).days == CATCHUP_LOOKBACK_DAYS
    assert window["today"] == window["end_date"]
