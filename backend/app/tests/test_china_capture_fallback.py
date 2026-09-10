"""Bounded China NAV/ETF fallback orchestration and guarded upsert (#389)."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import CST
from app.models.holding import Holding
from app.models.price_snapshot import PriceSnapshot
from app.services._tencent import OhlcBar
from app.services.capture_results import CaptureTarget, NavHistoryOutcome, NavPoint
from app.services.china_session_calendar import ChinaSessionWindow, freeze_capture_window
from app.services.price_capture import (
    _guarded_fallback_upsert,
    capture_fund_navs,
    capture_fund_navs_attempt,
    capture_prices,
    capture_prices_attempt,
    emit_nav_terminal_diagnostics,
)
from app.tests.conftest import seed_user

_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")
SEP4 = date(2026, 9, 4)
SEP7 = date(2026, 9, 7)
SEP8 = date(2026, 9, 8)


@pytest.fixture(autouse=True)
def _seed_user(db_session: Session) -> None:
    seed_user(db_session, _USER)


def _fund(code: str) -> Holding:
    return Holding(
        user_id=_USER,
        name=f"fund-{code}",
        fund_code=code,
        pricing_mode="auto",
        currency="CNY",
        market="A-Share",
        asset_class="EQUITY_CN",
        asset_type="fund",
    )


def _etf(ticker: str = "513500.SS") -> Holding:
    return Holding(
        user_id=_USER,
        name="CSI 500 ETF",
        ticker=ticker,
        pricing_mode="auto",
        currency="CNY",
        market="A-Share",
        asset_class="EQUITY_US_BROAD",
        asset_type="etf",
        capture_supported=True,
    )


def _window(at: datetime) -> ChinaSessionWindow:
    return freeze_capture_window(at, lookback_days=30, max_lag_sessions=2)


def _eastmoney(points: list[tuple[date, Decimal]], error: str | None = None) -> NavHistoryOutcome:
    return NavHistoryOutcome(
        points=tuple(NavPoint(day, nav, "eastmoney") for day, nav in points),
        error=error,  # type: ignore[arg-type]
    )


def test_partial_batch_commits_successes_and_retries_only_empty_fund(
    db_session: Session,
) -> None:
    db_session.add_all([_fund("008142"), _fund("110011"), _fund("019547")])
    db_session.flush()
    histories = {
        "008142": _eastmoney([(SEP8, Decimal("2.1766"))]),
        "110011": _eastmoney([(SEP8, Decimal("4.1454"))]),
        "019547": _eastmoney([]),
    }
    window = _window(datetime(2026, 9, 8, 20, 0, tzinfo=CST))
    with patch(
        "app.services.price_capture.fetch_nav_history_outcome",
        side_effect=lambda code, client, **kwargs: histories[code],
    ):
        outcome = capture_fund_navs_attempt(
            db_session, window=window, lookback_days=30, allow_fallback=False
        )
    tickers = {row.ticker for row in db_session.execute(select(PriceSnapshot)).scalars()}
    assert "008142" in tickers
    assert "110011" in tickers
    assert "019547" not in tickers
    assert outcome.written >= 2
    assert [t.key for t in outcome.unresolved] == ["019547"]


def test_wrapper_positive_writes_do_not_alert_unresolved(
    db_session: Session,
) -> None:
    db_session.add_all([_fund("008142"), _fund("019547")])
    db_session.flush()
    histories = {
        "008142": _eastmoney([(SEP8, Decimal("2.1766"))]),
        "019547": _eastmoney([]),
    }
    with (
        patch(
            "app.services.price_capture.fetch_nav_history_outcome",
            side_effect=lambda code, client, **kwargs: histories[code],
        ),
        patch(
            "app.services.price_capture.freeze_capture_window",
            return_value=_window(datetime(2026, 9, 8, 20, 0, tzinfo=CST)),
        ),
        patch("app.services.price_capture.send_ops_alert") as alert,
    ):
        written = capture_fund_navs(db_session)
    assert written >= 1
    alert.assert_not_called()


def test_lag_boundaries_default_two_sessions(db_session: Session) -> None:
    db_session.add_all([_fund("019547"), _fund("110011")])
    db_session.flush()
    morning = _window(datetime(2026, 9, 8, 8, 0, tzinfo=CST))
    evening = _window(datetime(2026, 9, 8, 20, 0, tzinfo=CST))
    with patch(
        "app.services.price_capture.fetch_nav_history_outcome",
        return_value=_eastmoney([(date(2026, 9, 4), Decimal("1.57"))]),
    ):
        am = capture_fund_navs_attempt(
            db_session, window=morning, fund_codes=["019547"], allow_fallback=False
        )
        pm = capture_fund_navs_attempt(
            db_session, window=evening, fund_codes=["019547"], allow_fallback=False
        )
    assert am.unresolved == ()
    assert pm.unresolved == ()
    with patch(
        "app.services.price_capture.fetch_nav_history_outcome",
        return_value=_eastmoney([(date(2026, 9, 3), Decimal("1.57"))]),
    ):
        am_old = capture_fund_navs_attempt(
            db_session, window=morning, fund_codes=["110011"], allow_fallback=False
        )
        pm_old = capture_fund_navs_attempt(
            db_session, window=evening, fund_codes=["110011"], allow_fallback=False
        )
    assert am_old.unresolved == ()
    assert [t.reason for t in pm_old.unresolved] == ["lag_exceeded"]


def test_sina_fallback_after_primary_exhaustion_is_latest_only(
    db_session: Session,
) -> None:
    db_session.add(_fund("019547"))
    db_session.flush()
    window = _window(datetime(2026, 9, 8, 20, 0, tzinfo=CST))
    sina = NavPoint(date(2026, 9, 7), Decimal("1.5765"), "sina")
    with (
        patch(
            "app.services.price_capture.fetch_nav_history_outcome",
            return_value=_eastmoney([], error="transport"),
        ),
        patch("app.services.price_capture.sina_latest_nav_point", return_value=sina) as sina_fetch,
        patch("app.services.price_capture.send_ops_alert") as alert,
    ):
        outcome = capture_fund_navs_attempt(db_session, window=window, allow_fallback=True)
    sina_fetch.assert_called_once()
    assert outcome.unresolved == ()
    assert outcome.history_coverage["019547"] == "latest_only"
    row = db_session.execute(
        select(PriceSnapshot).where(
            PriceSnapshot.ticker == "019547", PriceSnapshot.trade_date == date(2026, 9, 7)
        )
    ).scalar_one()
    assert row.close == Decimal("1.5765")
    assert row.source == "sina"
    alert.assert_not_called()


def test_guarded_upsert_does_not_overwrite_valid_close(db_session: Session) -> None:
    now = datetime.now(tz=UTC)
    db_session.add(
        PriceSnapshot(
            ticker="513500.SS",
            market="A-Share",
            session_node="close",
            trade_date=SEP7,
            open=Decimal("2.1"),
            high=Decimal("2.3"),
            low=Decimal("2.0"),
            close=Decimal("2.2"),
            volume=Decimal("100"),
            source="yfinance",
            captured_at=now,
        )
    )
    db_session.flush()
    written = _guarded_fallback_upsert(
        db_session,
        [
            {
                "ticker": "513500.SS",
                "market": "A-Share",
                "session_node": "close",
                "trade_date": SEP7,
                "open": Decimal("9"),
                "high": Decimal("9"),
                "low": Decimal("9"),
                "close": Decimal("9"),
                "volume": None,
                "last": None,
                "source": "tencent",
                "captured_at": now,
            }
        ],
    )
    assert written == 0
    row = db_session.execute(
        select(PriceSnapshot).where(PriceSnapshot.ticker == "513500.SS")
    ).scalar_one()
    assert row.close == Decimal("2.2")
    assert row.volume == Decimal("100")
    assert row.source == "yfinance"


def test_guarded_upsert_fills_unusable_close_and_keeps_volume(
    db_session: Session,
) -> None:
    now = datetime.now(tz=UTC)
    db_session.add(
        PriceSnapshot(
            ticker="513500.SS",
            market="A-Share",
            session_node="close",
            trade_date=SEP7,
            open=Decimal("2.1"),
            high=Decimal("2.3"),
            low=Decimal("2.0"),
            close=None,
            volume=Decimal("100"),
            source="yfinance",
            captured_at=now,
        )
    )
    db_session.flush()
    written = _guarded_fallback_upsert(
        db_session,
        [
            {
                "ticker": "513500.SS",
                "market": "A-Share",
                "session_node": "close",
                "trade_date": SEP7,
                "open": Decimal("2.1"),
                "high": Decimal("2.3"),
                "low": Decimal("2.0"),
                "close": Decimal("2.2"),
                "volume": None,
                "last": None,
                "source": "tencent",
                "captured_at": now,
            }
        ],
    )
    assert written == 1
    row = db_session.execute(
        select(PriceSnapshot).where(PriceSnapshot.ticker == "513500.SS")
    ).scalar_one()
    assert row.close == Decimal("2.2")
    assert row.volume == Decimal("100")
    assert row.source == "tencent"


def test_etf_window_inserts_only_missing_tencent_date(db_session: Session) -> None:
    db_session.add(_etf())
    db_session.flush()
    now = datetime.now(tz=UTC)
    for day, close in ((SEP4, "2.0"), (SEP8, "2.2")):
        db_session.add(
            PriceSnapshot(
                ticker="513500.SS",
                market="A-Share",
                session_node="close",
                trade_date=day,
                open=Decimal(close),
                high=Decimal(close),
                low=Decimal(close),
                close=Decimal(close),
                source="yfinance",
                captured_at=now,
            )
        )
    db_session.flush()
    yahoo = {
        "513500.SS": [
            (SEP4, 2.0, 2.0, 2.0, 2.0, 1.0),
            (SEP8, 2.2, 2.2, 2.2, 2.2, 1.0),
        ]
    }
    raw = {
        SEP4: OhlcBar(SEP4, Decimal("2.0"), Decimal("2.0"), Decimal("2.0"), Decimal("2.0")),
        SEP7: OhlcBar(SEP7, Decimal("2.1"), Decimal("2.1"), Decimal("2.1"), Decimal("2.1")),
        SEP8: OhlcBar(SEP8, Decimal("2.2"), Decimal("2.2"), Decimal("2.2"), Decimal("2.2")),
    }
    window = freeze_capture_window(
        datetime(2026, 9, 8, 20, 0, tzinfo=CST), lookback_days=7, max_lag_sessions=2
    )
    with (
        patch("app.services.price_capture.fetch_ohlcv_range_bounded", return_value=yahoo),
        patch("app.services.price_capture.fetch_tencent_daily_bars", side_effect=[raw, raw]),
    ):
        outcome, _anchors = capture_prices_attempt(
            db_session,
            "A-Share",
            "close",
            window=window,
            lookback_days=7,
            allow_fallback=True,
        )
    dates = {
        row.trade_date: row
        for row in db_session.execute(
            select(PriceSnapshot).where(PriceSnapshot.ticker == "513500.SS")
        ).scalars()
    }
    assert set(dates) == {SEP4, SEP7, SEP8}
    assert dates[SEP7].source == "tencent"
    assert dates[SEP4].source == "yfinance"
    assert dates[SEP8].source == "yfinance"
    assert outcome.unresolved == ()


def test_noneligible_market_does_not_call_tencent(db_session: Session) -> None:
    db_session.add(
        Holding(
            user_id=_USER,
            name="Apple",
            ticker="AAPL",
            pricing_mode="auto",
            currency="USD",
            market="US",
            asset_class="STOCK",
            asset_type="stock",
        )
    )
    db_session.flush()
    with (
        patch("app.services.price_capture.fetch_ohlcv_range", return_value={}),
        patch("app.services.price_capture.fetch_tencent_daily_bars") as tencent,
    ):
        capture_prices(db_session, "US", "close")
    tencent.assert_not_called()


def test_midnight_retry_keeps_frozen_window(db_session: Session) -> None:
    db_session.add(_fund("019547"))
    db_session.flush()
    frozen = _window(datetime(2026, 9, 8, 20, 0, tzinfo=CST))
    seen: list[date] = []

    def fake_fetch(code: str, client: object, **kwargs: object) -> NavHistoryOutcome:
        seen.append(kwargs["end_date"])  # type: ignore[arg-type]
        return _eastmoney([])

    with patch("app.services.price_capture.fetch_nav_history_outcome", side_effect=fake_fetch):
        capture_fund_navs_attempt(
            db_session, window=frozen, only_keys=("019547",), allow_fallback=False
        )
    assert seen == [date(2026, 9, 8)]


def test_removed_holding_is_not_resurrected_on_retry(db_session: Session) -> None:
    outcome = capture_fund_navs_attempt(
        db_session,
        window=_window(datetime(2026, 9, 8, 20, 0, tzinfo=CST)),
        only_keys=("019547",),
        allow_fallback=False,
    )
    assert outcome.written == 0
    assert outcome.unresolved == ()


def test_emit_nav_terminal_uses_existing_dedup_not_capture_failed(
    db_session: Session,
) -> None:
    logging.getLogger("app.services.price_capture").disabled = False
    target = CaptureTarget(
        key="019547",
        market="A-Share",
        kind="nav",
        reason="missing",
        cutoff=date(2026, 9, 4),
        window_start=date(2026, 8, 9),
        window_end=date(2026, 9, 8),
    )
    with patch("app.services.price_capture.send_ops_alert", return_value=True) as alert:
        emit_nav_terminal_diagnostics((target,), as_of_date=date(2026, 9, 8), max_lag_sessions=2)
        emit_nav_terminal_diagnostics((target,), as_of_date=date(2026, 9, 8), max_lag_sessions=2)
    alert.assert_called_once()
    assert alert.call_args.kwargs["idempotency_key"] == "ops-fund-nav-empty-019547-2026-09-08"
