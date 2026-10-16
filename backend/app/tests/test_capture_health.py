"""Capture-health probe (issue #372 slice B; weekend split issue #487).

Alert rule: 21:30 ET every calendar day. Market-data expected date = last
Mon-Fri; portfolio expected date = that calendar day. Stale = no success
evidence dated on the relevant expected day. Not a 36h wall clock (Monday
vs Friday would false-positive).
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import date, timedelta
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
    CaptureHealthReport,
    _is_repeat,
    evaluate_capture_health,
    expected_capture_date,
    expected_capture_date_portfolio,
    is_stale,
    maybe_alert_capture_health,
    should_alert_portfolio,
)
from app.tasks import celery_app
from app.tests.conftest import seed_user

_TUE = date(2026, 9, 8)
_FRI = date(2026, 9, 4)
_SAT = date(2026, 9, 5)
_UID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")


def test_expected_capture_date_is_weekday() -> None:
    assert expected_capture_date(_TUE) == _TUE
    assert expected_capture_date(date(2026, 9, 6)) == _FRI  # Sunday -> Friday


def test_expected_capture_date_portfolio_is_calendar_day() -> None:
    """Issue #487: portfolio evidence is expected every calendar day."""
    assert expected_capture_date_portfolio(_TUE) == _TUE
    assert expected_capture_date_portfolio(_SAT) == _SAT
    assert expected_capture_date_portfolio(date(2026, 9, 6)) == date(2026, 9, 6)


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


def test_evaluate_reports_last_success_dates(db_session: Session) -> None:
    """issue #426: the report must carry each pipeline's last-success date,
    not just the fact that it's stale, so the alert body can say how stale
    without a human having to SSH in and query the DB."""
    _seed(db_session, _FRI)
    report = evaluate_capture_health(db_session, as_of=_TUE)
    assert report.price_last == _FRI
    assert report.fx_last == _FRI
    assert report.bench_last == _FRI
    assert report.complete_last == _FRI


def test_evaluate_last_success_dates_none_when_never_captured(db_session: Session) -> None:
    seed_user(db_session, _UID, email="health-empty@example.com")
    report = evaluate_capture_health(db_session, as_of=_TUE)
    assert report.price_last is None
    assert report.fx_last is None
    assert report.bench_last is None
    assert report.complete_last is None


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
        subject = mock_alert.call_args.kwargs["subject"]
        # issue #426: human-readable labels and per-pipeline last-success
        # dates in the body a person reads, not raw field names as the
        # only content.
        assert "Market closing prices" in body
        assert "FX rates" in body
        assert "Benchmark index prices" in body
        assert "Portfolio value snapshots" in body
        assert _FRI.isoformat() in body  # last confirmed date, not just "stale"
        assert "No action needed unless this repeats" in body
        assert "capture health" not in subject.lower() or "Capture alert" in subject
        # the raw codes/values still live in a technical footer for debugging
        assert "raw issue codes: price,fx,portfolio,benchmark" in body
        assert "skipped_deps=0 pending=0" in body


def test_alert_body_matches_issue_426_literal_wording(
    db_session: Session, production_env: None
) -> None:
    """blacktomb42 review on PR #428: the first pass paraphrased issue
    #426's normative template ("FX exchange rates" / generic "no success
    dated" for every pipeline) instead of using its literal per-pipeline
    wording. Lock the exact phrasing in so it can't drift back."""
    _seed(db_session, _FRI)
    report = evaluate_capture_health(db_session, as_of=_TUE)
    with patch("app.services.capture_health.send_ops_alert", return_value=True) as mock_alert:
        maybe_alert_capture_health(report)
        body = mock_alert.call_args.kwargs["body"]
        assert "- FX rates: no exchange rate dated 2026-09-08 yet." in body
        assert "- Market closing prices: no closing price dated 2026-09-08 yet." in body
        assert "- Benchmark index prices: no price dated 2026-09-08 yet." in body
        assert "- Portfolio value snapshots: 0 user-day(s) waiting on a missing dependency" in body


def test_alert_body_names_the_fx_evening_followup(
    db_session: Session, production_env: None
) -> None:
    """issue #519: the fx block specifically should point at the 20:00 ET
    evening attempt rather than leaving the reader to guess what happens
    next (was the 00:05 ET next-day catch-up, issue #426)."""
    _seed(db_session, _FRI)
    report = evaluate_capture_health(db_session, as_of=_TUE)
    with patch("app.services.capture_health.send_ops_alert", return_value=True) as mock_alert:
        maybe_alert_capture_health(report)
        body = mock_alert.call_args.kwargs["body"]
        assert "20:00 ET" in body
        assert "#519" in body


# ---------------------------------------------------------------------------
# Severity taxonomy (issue #479)
# ---------------------------------------------------------------------------

_MON = date(2026, 9, 14)
_THU_BEFORE_MON = date(2026, 9, 10)
_FRI_BEFORE_MON = date(2026, 9, 11)
_MON_BEFORE_TUE = date(2026, 9, 7)


def test_is_repeat_uses_prior_trading_day_not_calendar_yesterday() -> None:
    """issue #479: a Monday probe must check Friday, not calendar-Sunday.

    `expected - timedelta(days=1)` on Monday is Sunday; last=Friday would
    then look like a repeat (Friday < Sunday). The helper must go through
    expected_capture_date so Friday success is NOT a repeat on Monday.
    """
    prior_via_helper = expected_capture_date(_MON - timedelta(days=1))
    calendar_yesterday = _MON - timedelta(days=1)
    assert prior_via_helper == _FRI_BEFORE_MON
    assert calendar_yesterday != _FRI_BEFORE_MON
    assert calendar_yesterday > _FRI_BEFORE_MON

    assert _is_repeat(_FRI_BEFORE_MON, _MON) is False
    assert _is_repeat(_THU_BEFORE_MON, _MON) is True
    assert _is_repeat(None, _MON) is True
    assert _is_repeat(_MON_BEFORE_TUE, _TUE) is False
    assert _is_repeat(_FRI, _TUE) is True


def _fx_report(*, expected: date, fx_last: date | None) -> CaptureHealthReport:
    return CaptureHealthReport(
        expected_date=expected,
        issues=("fx",),
        skipped_deps=0,
        pending=0,
        fx_last=fx_last,
    )


def test_capture_health_single_day_stale_is_info(production_env: None) -> None:
    """issue #479: stale only on expected_date → INFO."""
    report = _fx_report(expected=_TUE, fx_last=_MON_BEFORE_TUE)
    with patch("app.services.capture_health.send_ops_alert", return_value=True) as mock_alert:
        maybe_alert_capture_health(report)
        mock_alert.assert_called_once()
        assert mock_alert.call_args.kwargs["severity"] == "INFO"


def test_capture_health_repeat_stale_is_warning(production_env: None) -> None:
    """issue #479: stale on expected_date AND the prior trading day → WARNING."""
    report = _fx_report(expected=_TUE, fx_last=_FRI)
    with patch("app.services.capture_health.send_ops_alert", return_value=True) as mock_alert:
        maybe_alert_capture_health(report)
        mock_alert.assert_called_once()
        assert mock_alert.call_args.kwargs["severity"] == "WARNING"


def test_capture_health_monday_after_stale_friday_is_warning(production_env: None) -> None:
    """issue #479: Monday probe after a stale Friday is a repeat, via
    expected_capture_date, not calendar-Sunday."""
    report = _fx_report(expected=_MON, fx_last=_THU_BEFORE_MON)
    with patch("app.services.capture_health.send_ops_alert", return_value=True) as mock_alert:
        maybe_alert_capture_health(report)
        mock_alert.assert_called_once()
        assert mock_alert.call_args.kwargs["severity"] == "WARNING"


def test_capture_health_monday_after_healthy_friday_is_info(production_env: None) -> None:
    """issue #479: Friday success then Monday lag is a single-day INFO.

    A calendar-yesterday implementation would mis-label this WARNING.
    """
    report = _fx_report(expected=_MON, fx_last=_FRI_BEFORE_MON)
    with patch("app.services.capture_health.send_ops_alert", return_value=True) as mock_alert:
        maybe_alert_capture_health(report)
        mock_alert.assert_called_once()
        assert mock_alert.call_args.kwargs["severity"] == "INFO"


def test_capture_health_portfolio_only_is_info(production_env: None) -> None:
    """issue #479: portfolio has no single _last date and does not participate
    in repeat detection — a portfolio-only report is INFO."""
    report = CaptureHealthReport(
        expected_date=_TUE,
        issues=("portfolio",),
        skipped_deps=1,
        pending=0,
        complete_last=_FRI,
    )
    with patch("app.services.capture_health.send_ops_alert", return_value=True) as mock_alert:
        maybe_alert_capture_health(report)
        mock_alert.assert_called_once()
        assert mock_alert.call_args.kwargs["severity"] == "INFO"


def test_saturday_probe_healthy_when_portfolio_complete_and_friday_market_data(
    db_session: Session,
) -> None:
    """Acceptance 5: Saturday probe with a complete Saturday portfolio
    batch and Friday FX/price/benchmark evidence does not alert."""
    seed_user(db_session, _UID, email="health-sat@example.com")
    session_d = _SAT
    db_session.add(
        PriceSnapshot(
            ticker="AAPL",
            market="US",
            session_node="close",
            trade_date=_FRI,
            close=Decimal("100"),
        )
    )
    db_session.add(FxRate(pair="USDCNY", rate=Decimal("7"), rate_date=_FRI))
    db_session.add(
        BenchmarkPrice(
            index_code="sp500", price_date=_FRI, close_price=Decimal("1"), currency="USD"
        )
    )
    db_session.add(PortfolioSnapshotBatch(user_id=_UID, snapshot_date=session_d, status="complete"))
    db_session.flush()

    report = evaluate_capture_health(db_session, as_of=_SAT)
    assert report.issues == ()
    assert report.should_alert() is False
    assert expected_capture_date(_SAT) == _FRI
    assert expected_capture_date_portfolio(_SAT) == _SAT


def test_saturday_probe_alerts_when_portfolio_batch_missing(db_session: Session) -> None:
    """Acceptance 6: Saturday probe with no complete portfolio batch that
    day alerts on portfolio, even though Friday market data is current."""
    seed_user(db_session, _UID, email="health-sat-miss@example.com")
    db_session.add(
        PriceSnapshot(
            ticker="AAPL",
            market="US",
            session_node="close",
            trade_date=_FRI,
            close=Decimal("100"),
        )
    )
    db_session.add(FxRate(pair="USDCNY", rate=Decimal("7"), rate_date=_FRI))
    db_session.add(
        BenchmarkPrice(
            index_code="sp500", price_date=_FRI, close_price=Decimal("1"), currency="USD"
        )
    )
    db_session.flush()

    report = evaluate_capture_health(db_session, as_of=_SAT)
    assert "portfolio" in report.issues
    assert "price" not in report.issues
    assert "fx" not in report.issues
    assert "benchmark" not in report.issues
    assert report.should_alert() is True
    assert report.expected_date == _SAT


def test_weekend_portfolio_alerts_use_probe_date_for_dedup(
    db_session: Session, production_env: None
) -> None:
    """Review P2: Saturday then Sunday portfolio failures must send two
    emails, keyed by the probe date, not Friday's market expected date.
    A second Saturday probe is deduped."""
    seed_user(db_session, _UID, email="health-weekend-dedup@example.com")
    db_session.add(
        PriceSnapshot(
            ticker="AAPL",
            market="US",
            session_node="close",
            trade_date=_FRI,
            close=Decimal("100"),
        )
    )
    db_session.add(FxRate(pair="USDCNY", rate=Decimal("7"), rate_date=_FRI))
    db_session.add(
        BenchmarkPrice(
            index_code="sp500", price_date=_FRI, close_price=Decimal("1"), currency="USD"
        )
    )
    db_session.flush()

    sat = evaluate_capture_health(db_session, as_of=_SAT)
    sun = evaluate_capture_health(db_session, as_of=date(2026, 9, 6))
    assert sat.issues == ("portfolio",)
    assert sun.issues == ("portfolio",)
    assert sat.expected_date == _SAT
    assert sun.expected_date == date(2026, 9, 6)

    with patch("app.services.capture_health.send_ops_alert", return_value=True) as mock_alert:
        maybe_alert_capture_health(sat)
        maybe_alert_capture_health(sun)
        maybe_alert_capture_health(sat)
        assert mock_alert.call_count == 2
        subjects = [c.kwargs["subject"] for c in mock_alert.call_args_list]
        assert _SAT.isoformat() in subjects[0]
        assert date(2026, 9, 6).isoformat() in subjects[1]
        assert _FRI.isoformat() not in subjects[0]
        assert _FRI.isoformat() not in subjects[1]


def test_beat_probe_is_after_snapshot_window() -> None:
    entry = celery_app.conf.beat_schedule["check-capture-health-daily"]
    assert entry["task"] == "app.tasks.capture_tasks.check_capture_health_task"
    assert 21 in entry["schedule"].hour
    assert 30 in entry["schedule"].minute
    assert entry["schedule"].day_of_week == set(range(7))


def test_fx_evening_beat_schedule() -> None:
    """issue #519: 20:00 ET (US equity after-hours close), same calendar
    day, 30 minutes before the 20:30 ET portfolio snapshot — every day
    including weekends (the #487 separate weekend-backfill script is
    retired in favor of this single schedule)."""
    entry = celery_app.conf.beat_schedule["capture-fx-evening-daily"]
    assert entry["task"] == "app.tasks.capture_tasks.capture_fx_evening_task"
    assert 20 in entry["schedule"].hour
    assert 0 in entry["schedule"].minute
    assert entry["schedule"].day_of_week == set(range(7))


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


@patch("app.core.database.SessionLocal")
@patch("app.services.fx_fetcher.capture_fx_rates")
def test_fx_evening_task_writes_operational_event(
    mock_capture: MagicMock, mock_session_cls: MagicMock
) -> None:
    """Issue #519 (was #509): same durable-evidence requirement as
    capture_fx_task, reusing the same `capture.fx` operation and attribute
    shape so both same-day attempts read as one consistent event series."""
    from app.services.fx_fetcher import FxCaptureResult
    from app.tasks.capture_tasks import capture_fx_evening_task

    session = MagicMock()
    mock_session_cls.return_value = session
    mock_capture.return_value = FxCaptureResult(
        recovered_via_yfinance=[],
        recovered_via_fallback=["USDJPY"],
    )

    with (
        patch("app.tasks.capture_tasks.oe.start_run") as mock_start,
        patch("app.tasks.capture_tasks.oe.end_run") as mock_end,
    ):
        capture_fx_evening_task.run()

    assert mock_start.call_args.args[0] == "capture.fx"
    mock_end.assert_called_once_with("ok", attributes={"pairs_upserted": 1, "pairs_failed": 0})


@patch("app.core.database.SessionLocal")
@patch("app.services.fx_fetcher.capture_fx_rates")
def test_fx_evening_task_span_reports_twelvedata_source(
    mock_capture: MagicMock, mock_session_cls: MagicMock
) -> None:
    """Issue #509 review (blacktomb42, PR #511), carried into #519's
    rewrite: Design sketched "which source resolved them" — a pair
    recovered via the Twelve Data fallback must say so, not just "some
    source, unspecified"."""
    from app.services.fx_fetcher import FxCaptureResult
    from app.tasks.capture_tasks import capture_fx_evening_task

    session = MagicMock()
    mock_session_cls.return_value = session
    mock_capture.return_value = FxCaptureResult(
        recovered_via_yfinance=[],
        recovered_via_fallback=["USDJPY"],
    )

    with (
        patch("app.tasks.capture_tasks.oe.start_run"),
        patch("app.tasks.capture_tasks.oe.end_run"),
        patch("app.tasks.capture_tasks.oe.start_span") as mock_start_span,
        patch("app.tasks.capture_tasks.oe.end_span") as mock_end_span,
    ):
        capture_fx_evening_task.run()

    assert mock_start_span.call_args.args[0] == "capture.fx.evening"
    mock_end_span.assert_called_once_with(
        mock_start_span.return_value,
        "ok",
        attributes={"source": "twelvedata", "pairs_upserted": 1, "pairs_failed": 0},
    )


@patch("app.core.database.SessionLocal")
@patch("app.services.fx_fetcher.capture_fx_rates")
def test_fx_evening_task_span_reports_yfinance_source(
    mock_capture: MagicMock, mock_session_cls: MagicMock
) -> None:
    """A pair recovered via the plain yfinance live quote (no Twelve Data
    call needed) reports "yfinance", the same source name FxRate.source
    itself uses."""
    from app.services.fx_fetcher import FxCaptureResult
    from app.tasks.capture_tasks import capture_fx_evening_task

    session = MagicMock()
    mock_session_cls.return_value = session
    mock_capture.return_value = FxCaptureResult(recovered_via_yfinance=["USDJPY"])

    with (
        patch("app.tasks.capture_tasks.oe.start_run"),
        patch("app.tasks.capture_tasks.oe.end_run"),
        patch("app.tasks.capture_tasks.oe.start_span") as mock_start_span,
        patch("app.tasks.capture_tasks.oe.end_span") as mock_end_span,
    ):
        capture_fx_evening_task.run()

    mock_end_span.assert_called_once_with(
        mock_start_span.return_value,
        "ok",
        attributes={"source": "yfinance", "pairs_upserted": 1, "pairs_failed": 0},
    )


@patch("app.core.database.SessionLocal")
@patch("app.services.fx_fetcher.capture_fx_rates")
def test_fx_evening_task_span_exists_when_all_pairs_resolved(
    mock_capture: MagicMock, mock_session_cls: MagicMock
) -> None:
    """Nothing was missing (the common case) still writes one span — the
    Contract's "at least one span" acceptance test must not depend on a
    pair actually needing a fallback that day."""
    from app.services.fx_fetcher import FxCaptureResult
    from app.tasks.capture_tasks import capture_fx_evening_task

    session = MagicMock()
    mock_session_cls.return_value = session
    mock_capture.return_value = FxCaptureResult(
        recovered_via_yfinance=[f"PAIR{i}" for i in range(14)]
    )

    with (
        patch("app.tasks.capture_tasks.oe.start_run"),
        patch("app.tasks.capture_tasks.oe.end_run"),
        patch("app.tasks.capture_tasks.oe.start_span") as mock_start_span,
        patch("app.tasks.capture_tasks.oe.end_span") as mock_end_span,
    ):
        capture_fx_evening_task.run()

    mock_start_span.assert_called_once()
    mock_end_span.assert_called_once_with(
        mock_start_span.return_value,
        "ok",
        attributes={"source": "yfinance", "pairs_upserted": 14, "pairs_failed": 0},
    )
    session.commit.assert_called_once()
    session.close.assert_called_once()
