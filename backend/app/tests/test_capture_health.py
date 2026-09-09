"""Capture-health probe (issue #372 slice B).

Alert rule: after the Mon-Fri 21:30 ET probe, expected date = that ET
weekday. Stale = no success evidence dated on that day. Not a 36h wall
clock (Monday vs Friday would false-positive).
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.benchmark_price import BenchmarkPrice
from app.models.fx_rate import FxRate
from app.models.portfolio_snapshot_batch import PortfolioSnapshotBatch
from app.models.price_snapshot import PriceSnapshot
from app.services.capture_health import (
    evaluate_capture_health,
    expected_capture_date,
    is_stale,
    maybe_alert_capture_health,
    should_alert_portfolio,
)
from app.tasks import celery_app
from app.tests.conftest import seed_user

_TUE = date(2026, 9, 8)
_FRI = date(2026, 9, 4)
_UID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")


def test_expected_capture_date_is_weekday() -> None:
    assert expected_capture_date(_TUE) == _TUE
    assert expected_capture_date(date(2026, 9, 6)) == _FRI  # Sunday -> Friday


def test_is_stale_frozen_clock() -> None:
    assert is_stale(None, _TUE) is True
    assert is_stale(_FRI, _TUE) is True
    assert is_stale(_TUE, _TUE) is False
    assert is_stale(date(2026, 9, 9), _TUE) is False


def test_should_alert_portfolio_frozen_clock() -> None:
    assert (
        should_alert_portfolio(last_complete=_TUE, expected=_TUE, skipped_deps=0, pending=0)
        is False
    )
    assert (
        should_alert_portfolio(last_complete=_FRI, expected=_TUE, skipped_deps=0, pending=0) is True
    )
    assert (
        should_alert_portfolio(last_complete=_TUE, expected=_TUE, skipped_deps=2, pending=0) is True
    )
    assert (
        should_alert_portfolio(last_complete=_TUE, expected=_TUE, skipped_deps=0, pending=1) is True
    )


def _seed(session: Session, d: date, *, batch_status: str = "complete") -> None:
    seed_user(session, _UID, email="health@example.com")
    session.add(
        PriceSnapshot(
            ticker="AAPL", market="US", session_node="close", trade_date=d, close=Decimal("100")
        )
    )
    session.add(FxRate(pair="USDCNY", rate=Decimal("7"), rate_date=d))
    session.add(PortfolioSnapshotBatch(user_id=_UID, snapshot_date=d, status=batch_status))
    session.add(
        BenchmarkPrice(index_code="sp500", price_date=d, close_price=Decimal("1"), currency="USD")
    )
    session.flush()


def test_evaluate_fresh_has_no_issues(db_session: Session) -> None:
    _seed(db_session, _TUE)
    report = evaluate_capture_health(db_session, as_of=_TUE)
    assert report.expected_date == _TUE
    assert report.issues == ()
    assert report.should_alert() is False


def test_evaluate_stale_price(db_session: Session) -> None:
    _seed(db_session, _FRI)
    report = evaluate_capture_health(db_session, as_of=_TUE)
    assert "price" in report.issues
    assert "fx" in report.issues
    assert "benchmark" in report.issues
    assert "portfolio" in report.issues
    assert report.should_alert() is True


def test_evaluate_skipped_deps_on_expected_day(db_session: Session) -> None:
    _seed(db_session, _TUE, batch_status="skipped_deps")
    report = evaluate_capture_health(db_session, as_of=_TUE)
    assert "portfolio" in report.issues
    assert report.skipped_deps == 1
    assert report.should_alert() is True


@pytest.fixture
def production_env() -> Generator[None, None, None]:
    get_settings.cache_clear()
    with patch.dict("os.environ", {"APP_ENV": "production"}):
        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


def test_no_alert_outside_production(db_session: Session) -> None:
    _seed(db_session, _FRI)
    report = evaluate_capture_health(db_session, as_of=_TUE)
    with patch("app.services.capture_health.send_ops_alert") as mock_alert:
        maybe_alert_capture_health(report)
        mock_alert.assert_not_called()


def test_one_aggregated_alert_in_production(db_session: Session, production_env: None) -> None:
    _seed(db_session, _FRI)
    report = evaluate_capture_health(db_session, as_of=_TUE)
    with patch("app.services.capture_health.send_ops_alert", return_value=True) as mock_alert:
        maybe_alert_capture_health(report)
        mock_alert.assert_called_once()
        body = mock_alert.call_args.kwargs["body"]
        assert "price" in body
        assert "portfolio" in body


def test_beat_probe_is_after_snapshot_window() -> None:
    entry = celery_app.conf.beat_schedule["check-capture-health-daily"]
    assert entry["task"] == "app.tasks.capture_tasks.check_capture_health_task"
    assert 21 in entry["schedule"].hour
    assert 30 in entry["schedule"].minute


@patch("app.core.database.SessionLocal")
@patch("app.services.capture_health.evaluate_capture_health")
@patch("app.services.capture_health.maybe_alert_capture_health")
def test_probe_task(
    mock_alert: MagicMock, mock_eval: MagicMock, mock_session_cls: MagicMock
) -> None:
    from app.tasks.capture_tasks import check_capture_health_task

    session = MagicMock()
    mock_session_cls.return_value = session
    report = MagicMock()
    report.as_dict.return_value = {"issues": ("price",)}
    mock_eval.return_value = report
    assert check_capture_health_task.run() == {"issues": ("price",)}
    mock_alert.assert_called_once_with(report)
    session.close.assert_called_once()
