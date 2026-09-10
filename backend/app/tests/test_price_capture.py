"""Tests for the price capture service (ADR-002 step 2b)."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.models.price_snapshot import PriceSnapshot
from app.services._finnhub import FinnhubQuote
from app.services._yfinance import OhlcvPoint
from app.services.capture_results import CaptureTarget, NavHistoryOutcome, NavPoint
from app.services.price_capture import (
    _UPSERT_CHUNK_SIZE,
    _upsert,
    capture_fund_navs,
    capture_prices,
    emit_nav_terminal_diagnostics,
)
from app.tests.conftest import seed_user

_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture(autouse=True)
def _seed_user(db_session: Session) -> None:
    seed_user(db_session, _USER)


def _holding(name: str, ticker: str, market: str | None = None) -> Holding:
    return Holding(
        user_id=_USER,
        name=name,
        ticker=ticker,
        pricing_mode="auto",
        currency="USD",
        market=market,
    )


def test_capture_close_stores_ohlcv(db_session: Session) -> None:
    db_session.add_all([_holding("Apple", "AAPL"), _holding("Tencent", "0700.HK")])
    db_session.flush()

    ohlcv = {"AAPL": [(date(2026, 6, 5), 200.0, 205.0, 199.0, 203.5, 1000.0)]}
    with patch("app.services.price_capture.fetch_ohlcv_range", return_value=ohlcv):
        n = capture_prices(db_session, market="US", session_node="close")

    assert n == 1  # only AAPL is a US ticker
    row = db_session.execute(
        select(PriceSnapshot).where(PriceSnapshot.ticker == "AAPL")
    ).scalar_one()
    assert row.close == Decimal("203.5")
    assert row.open == Decimal("200.0")
    assert row.trade_date == date(2026, 6, 5)
    assert row.last is None


def test_capture_close_is_idempotent(db_session: Session) -> None:
    db_session.add(_holding("Apple", "AAPL"))
    db_session.flush()
    ohlcv = {"AAPL": [(date(2026, 6, 5), 200.0, 205.0, 199.0, 203.5, 1000.0)]}
    with patch("app.services.price_capture.fetch_ohlcv_range", return_value=ohlcv):
        capture_prices(db_session, market="US", session_node="close")
        # Re-capture with a revised close → updates the same row, no duplicate.
        ohlcv["AAPL"] = [(date(2026, 6, 5), 200.0, 206.0, 199.0, 204.0, 1100.0)]
        capture_prices(db_session, market="US", session_node="close")

    rows = (
        db_session.execute(select(PriceSnapshot).where(PriceSnapshot.ticker == "AAPL"))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].close == Decimal("204.0")


# ---------------------------------------------------------------------------
# Finnhub fallback wiring (issue #56) — non-close US-market spot capture only
# ---------------------------------------------------------------------------


def _settings_with_finnhub_key(key: str | None) -> MagicMock:
    return _settings_with_keys(finnhub=key, massive=None)


def _settings_with_keys(finnhub: str | None, massive: str | None) -> MagicMock:
    settings = MagicMock()
    if finnhub is None:
        settings.FINNHUB_API_KEY = None
    else:
        settings.FINNHUB_API_KEY.get_secret_value.return_value = finnhub
    if massive is None:
        settings.MASSIVE_API_KEY = None
    else:
        settings.MASSIVE_API_KEY.get_secret_value.return_value = massive
    return settings


def test_capture_spot_falls_back_to_finnhub_for_yfinance_miss(db_session: Session) -> None:
    db_session.add(_holding("Apple", "AAPL", market="US"))
    db_session.flush()

    with (
        patch(
            "app.services.price_capture.get_settings", return_value=_settings_with_finnhub_key("k")
        ),
        patch("app.services.price_capture.fetch_spot", return_value={}),
        patch(
            "app.services.price_capture.fetch_finnhub_quotes",
            return_value={"AAPL": FinnhubQuote(last=328.21, previous_close=324.96)},
        ) as mock_finnhub,
    ):
        n = capture_prices(db_session, market="US", session_node="pre_open")

    mock_finnhub.assert_called_once_with(["AAPL"], "k")
    assert n == 1
    row = db_session.execute(
        select(PriceSnapshot).where(PriceSnapshot.ticker == "AAPL")
    ).scalar_one()
    assert row.last == Decimal("328.21")
    assert row.source == "finnhub"


def test_capture_spot_does_not_call_finnhub_when_yfinance_succeeds(db_session: Session) -> None:
    db_session.add(_holding("Apple", "AAPL", market="US"))
    db_session.flush()

    with (
        patch(
            "app.services.price_capture.get_settings", return_value=_settings_with_finnhub_key("k")
        ),
        patch("app.services.price_capture.fetch_spot", return_value={"AAPL": 330.0}),
        patch("app.services.price_capture.fetch_finnhub_quotes") as mock_finnhub,
    ):
        capture_prices(db_session, market="US", session_node="pre_open")

    mock_finnhub.assert_not_called()


def test_capture_spot_does_not_call_finnhub_when_key_unset(db_session: Session) -> None:
    db_session.add(_holding("Apple", "AAPL", market="US"))
    db_session.flush()

    with (
        patch(
            "app.services.price_capture.get_settings", return_value=_settings_with_finnhub_key(None)
        ),
        patch("app.services.price_capture.fetch_spot", return_value={}),
        patch("app.services.price_capture.fetch_finnhub_quotes") as mock_finnhub,
    ):
        capture_prices(db_session, market="US", session_node="pre_open")

    mock_finnhub.assert_not_called()


def test_capture_spot_non_us_market_does_not_call_finnhub(db_session: Session) -> None:
    db_session.add(_holding("Tencent", "0700.HK", market="HK"))
    db_session.flush()

    with (
        patch(
            "app.services.price_capture.get_settings", return_value=_settings_with_finnhub_key("k")
        ),
        patch("app.services.price_capture.fetch_spot", return_value={}),
        patch("app.services.price_capture.fetch_finnhub_quotes") as mock_finnhub,
    ):
        capture_prices(db_session, market="HK", session_node="pre_open")

    mock_finnhub.assert_not_called()


def test_capture_close_node_does_not_call_finnhub(db_session: Session) -> None:
    """Finnhub is a spot/intraday-node fallback (near-real-time single point)
    — the close node uses fetch_ohlcv_range, not fetch_spot, and must never
    reach for Finnhub even on a yfinance miss."""
    db_session.add(_holding("Apple", "AAPL", market="US"))
    db_session.flush()

    with (
        patch(
            "app.services.price_capture.get_settings", return_value=_settings_with_finnhub_key("k")
        ),
        patch("app.services.price_capture.fetch_ohlcv_range", return_value={}),
        patch("app.services.price_capture.fetch_finnhub_quotes") as mock_finnhub,
    ):
        capture_prices(db_session, market="US", session_node="close")

    mock_finnhub.assert_not_called()


def test_capture_close_backfills_multiple_days(db_session: Session) -> None:
    """A range fetch stores one row per trading day — this is catch-up."""
    db_session.add(_holding("Apple", "AAPL"))
    db_session.flush()
    bars = {
        "AAPL": [
            (date(2026, 6, 3), 1.0, 1.0, 1.0, 100.0, 1.0),
            (date(2026, 6, 4), 1.0, 1.0, 1.0, 101.0, 1.0),
            (date(2026, 6, 5), 1.0, 1.0, 1.0, 102.0, 1.0),
        ]
    }
    with patch("app.services.price_capture.fetch_ohlcv_range", return_value=bars):
        n = capture_prices(db_session, market="US", session_node="close")
    assert n == 3
    rows = (
        db_session.execute(select(PriceSnapshot).where(PriceSnapshot.ticker == "AAPL"))
        .scalars()
        .all()
    )
    assert {r.trade_date for r in rows} == {date(2026, 6, 3), date(2026, 6, 4), date(2026, 6, 5)}


def test_capture_intraday_stores_last(db_session: Session) -> None:
    db_session.add(_holding("Apple", "AAPL"))
    db_session.flush()
    with patch("app.services.price_capture.fetch_spot", return_value={"AAPL": 201.25}):
        n = capture_prices(
            db_session, market="US", session_node="open", trade_date=date(2026, 6, 5)
        )
    assert n == 1
    row = db_session.execute(
        select(PriceSnapshot).where(PriceSnapshot.session_node == "open")
    ).scalar_one()
    assert row.last == Decimal("201.25")
    assert row.close is None


def test_capture_declared_market_routes_ticker(db_session: Session) -> None:
    # A US-listed ticker the user declared as HK must capture under HK, not US.
    db_session.add(_holding("Weird", "AAPL", market="HK"))
    db_session.flush()
    with patch("app.services.price_capture.fetch_spot", return_value={"AAPL": 50.0}) as spot:
        assert capture_prices(db_session, market="US", session_node="open") == 0
        spot.assert_not_called()  # AAPL is not in the US bucket here


def test_capture_prices_tickers_filter_restricts_fetch(db_session: Session) -> None:
    """A confirm-time backfill must not pull the whole market universe (#194)."""
    db_session.add_all([_holding("Apple", "AAPL"), _holding("Nvidia", "NVDA")])
    db_session.flush()
    ohlcv = {"AAPL": [(date(2026, 6, 5), 1.0, 1.0, 1.0, 100.0, 1.0)]}
    with patch("app.services.price_capture.fetch_ohlcv_range", return_value=ohlcv) as fetch:
        n = capture_prices(db_session, market="US", session_node="close", tickers=["AAPL"])
    fetch.assert_called_once_with(["AAPL"], lookback_days=7)
    assert n == 1
    assert (
        db_session.execute(
            select(func.count()).select_from(PriceSnapshot).where(PriceSnapshot.ticker == "NVDA")
        ).scalar_one()
        == 0
    )


def test_capture_prices_empty_tickers_filter_fetches_nothing(db_session: Session) -> None:
    db_session.add(_holding("Apple", "AAPL"))
    db_session.flush()
    with patch("app.services.price_capture.fetch_ohlcv_range") as fetch:
        n = capture_prices(db_session, market="US", session_node="close", tickers=[])
    assert n == 0
    fetch.assert_not_called()


def _fund_holding(name: str, fund_code: str) -> Holding:
    return Holding(
        user_id=_USER,
        name=name,
        fund_code=fund_code,
        pricing_mode="auto",
        currency="CNY",
        market="A-Share",
        asset_class="EQUITY_US_BROAD",
    )


def _nav_out(history: list[tuple[date, Decimal]]) -> NavHistoryOutcome:
    return NavHistoryOutcome(points=tuple(NavPoint(day, nav, "eastmoney") for day, nav in history))


def test_capture_fund_navs_stores_close_under_fund_code(db_session: Session) -> None:
    db_session.add(_fund_holding("Huaxia SSE 50 ETF", "513100"))
    db_session.flush()
    history = [(date(2026, 8, 22), Decimal("1.23"))]
    with patch(
        "app.services.price_capture.fetch_nav_history_outcome",
        return_value=_nav_out(history),
    ):
        n = capture_fund_navs(db_session)

    assert n == 1
    row = db_session.execute(
        select(PriceSnapshot).where(PriceSnapshot.ticker == "513100")
    ).scalar_one()
    assert row.close == Decimal("1.23")
    assert row.session_node == "close"
    assert row.market == "A-Share"
    assert row.trade_date == date(2026, 8, 22)
    assert row.source == "eastmoney"


def test_capture_fund_navs_fund_codes_filter_restricts_fetch(db_session: Session) -> None:
    """Confirm-time NAV capture must not rescan every fund in the system (#196)."""
    db_session.add_all(
        [
            _fund_holding("Huaxia SSE 50 ETF", "513100"),
            _fund_holding("Huatai CSI 300 ETF", "510300"),
        ]
    )
    db_session.flush()
    history = [(date(2026, 8, 22), Decimal("1.23"))]
    with patch(
        "app.services.price_capture.fetch_nav_history_outcome",
        return_value=_nav_out(history),
    ) as fetch:
        n = capture_fund_navs(db_session, fund_codes=["513100"])

    fetch.assert_called_once()
    assert fetch.call_args.args[0] == "513100"
    assert n == 1
    assert (
        db_session.execute(
            select(func.count()).select_from(PriceSnapshot).where(PriceSnapshot.ticker == "510300")
        ).scalar_one()
        == 0
    )


def test_capture_fund_navs_empty_fund_codes_filter_fetches_nothing(
    db_session: Session,
) -> None:
    db_session.add(_fund_holding("Huaxia SSE 50 ETF", "513100"))
    db_session.flush()
    with patch("app.services.price_capture.fetch_nav_history_outcome") as fetch:
        n = capture_fund_navs(db_session, fund_codes=[])
    assert n == 0
    fetch.assert_not_called()


def test_capture_fund_navs_dedupes_same_fund_code_across_holdings(
    db_session: Session,
) -> None:
    """Two users (or lots) of the same fund must not double-fetch the NAV API."""
    other = uuid.uuid4()
    seed_user(db_session, other)
    db_session.add_all(
        [
            _fund_holding("Huaxia SSE 50 ETF", "513100"),
            Holding(
                user_id=other,
                name="Same fund, other user",
                fund_code="513100",
                pricing_mode="auto",
                currency="CNY",
                market="A-Share",
                asset_class="EQUITY_US_BROAD",
            ),
        ]
    )
    db_session.flush()
    history = [(date(2026, 8, 22), Decimal("1.23"))]
    with patch(
        "app.services.price_capture.fetch_nav_history_outcome",
        return_value=_nav_out(history),
    ) as fetch:
        n = capture_fund_navs(db_session)
    fetch.assert_called_once()
    assert n == 1


def test_capture_fund_navs_prefers_declared_market_over_null_default(
    db_session: Session,
) -> None:
    """Same fund_code, mixed NULL vs declared market: declared wins, stably."""
    other = uuid.uuid4()
    seed_user(db_session, other)
    db_session.add_all(
        [
            Holding(
                user_id=_USER,
                name="Null market lot",
                fund_code="513100",
                pricing_mode="auto",
                currency="CNY",
                market=None,
                asset_class="EQUITY_US_BROAD",
            ),
            Holding(
                user_id=other,
                name="Declared HK lot",
                fund_code="513100",
                pricing_mode="auto",
                currency="CNY",
                market="HK",
                asset_class="EQUITY_US_BROAD",
            ),
        ]
    )
    db_session.flush()
    history = [(date(2026, 8, 22), Decimal("1.23"))]
    with patch(
        "app.services.price_capture.fetch_nav_history_outcome",
        return_value=_nav_out(history),
    ):
        n = capture_fund_navs(db_session)
    assert n == 1
    row = db_session.execute(
        select(PriceSnapshot).where(PriceSnapshot.ticker == "513100")
    ).scalar_one()
    assert row.market == "HK"


def _stale_target(code: str, nav_date: date) -> CaptureTarget:
    return CaptureTarget(
        key=code,
        market="A-Share",
        kind="nav",
        reason="lag_exceeded",
        latest_date=nav_date,
    )


def _missing_target(code: str) -> CaptureTarget:
    return CaptureTarget(key=code, market="A-Share", kind="nav", reason="missing")


def test_terminal_nav_stale_alert_is_keyed_by_nav_date(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    logging.getLogger("app.services.price_capture").disabled = False
    with (
        caplog.at_level(logging.WARNING, logger="app.services.price_capture"),
        patch("app.services.price_capture.send_ops_alert") as alert,
    ):
        emit_nav_terminal_diagnostics(
            (_stale_target("513500", date(2026, 8, 27)),),
            as_of_date=date(2026, 9, 8),
            max_lag_sessions=2,
        )
    alert.assert_called_once()
    assert alert.call_args.kwargs["idempotency_key"] == "ops-fund-nav-stale-513500-2026-08-27"
    assert "513500" in alert.call_args.kwargs["subject"]
    assert any("2026-08-27" in r.getMessage() for r in caplog.records)


def test_capture_fund_navs_wrapper_does_not_send_terminal_alerts(
    db_session: Session,
) -> None:
    db_session.add(_fund_holding("CSI 500 ETF", "513500"))
    db_session.flush()
    with (
        patch(
            "app.services.price_capture.fetch_nav_history_outcome",
            return_value=_nav_out([]),
        ),
        patch("app.services.price_capture.send_ops_alert") as alert,
    ):
        n = capture_fund_navs(db_session)
    assert n == 0
    alert.assert_not_called()


def test_terminal_nav_alert_dedupes_same_nav_date(db_session: Session) -> None:
    targets = (_stale_target("513500", date(2026, 8, 27)),)
    with patch("app.services.price_capture.send_ops_alert") as alert:
        emit_nav_terminal_diagnostics(targets, date(2026, 9, 8), 2)
        emit_nav_terminal_diagnostics(targets, date(2026, 9, 8), 2)
    alert.assert_called_once()


def test_terminal_nav_alerts_stale_fund_only(db_session: Session) -> None:
    with patch("app.services.price_capture.send_ops_alert") as alert:
        emit_nav_terminal_diagnostics(
            (_stale_target("513500", date(2026, 8, 28)),),
            date(2026, 9, 8),
            2,
        )
    alert.assert_called_once()
    assert alert.call_args.kwargs["idempotency_key"] == "ops-fund-nav-stale-513500-2026-08-28"


def test_terminal_empty_nav_warns_and_alerts(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    logging.getLogger("app.services.price_capture").disabled = False
    with (
        caplog.at_level(logging.WARNING, logger="app.services.price_capture"),
        patch("app.services.price_capture.send_ops_alert") as alert,
    ):
        emit_nav_terminal_diagnostics((_missing_target("513500"),), date(2026, 9, 1), 2)
    alert.assert_called_once()
    assert alert.call_args.kwargs["idempotency_key"] == "ops-fund-nav-empty-513500-2026-09-01"
    assert any(
        "513500" in r.getMessage() and "no NAV history" in r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING
    )


def test_terminal_empty_nav_dedupes_same_day(db_session: Session) -> None:
    with patch("app.services.price_capture.send_ops_alert") as alert:
        emit_nav_terminal_diagnostics((_missing_target("513500"),), date(2026, 9, 1), 2)
        emit_nav_terminal_diagnostics((_missing_target("513500"),), date(2026, 9, 1), 2)
    alert.assert_called_once()


def test_terminal_nav_does_not_dedup_failed_delivery(db_session: Session) -> None:
    with patch("app.services.price_capture.send_ops_alert", return_value=False) as alert:
        emit_nav_terminal_diagnostics(
            (_stale_target("513500", date(2026, 8, 27)),), date(2026, 9, 8), 2
        )
        emit_nav_terminal_diagnostics(
            (_stale_target("513500", date(2026, 8, 27)),), date(2026, 9, 8), 2
        )
    assert alert.call_count == 2


def test_terminal_nav_reattempts_alert_when_nav_date_changes(db_session: Session) -> None:
    with patch("app.services.price_capture.send_ops_alert") as alert:
        emit_nav_terminal_diagnostics(
            (_stale_target("513500", date(2026, 8, 27)),), date(2026, 9, 8), 2
        )
        emit_nav_terminal_diagnostics(
            (_stale_target("513500", date(2026, 8, 28)),), date(2026, 9, 8), 2
        )
    assert [c.kwargs["idempotency_key"] for c in alert.call_args_list] == [
        "ops-fund-nav-stale-513500-2026-08-27",
        "ops-fund-nav-stale-513500-2026-08-28",
    ]


def test_terminal_empty_nav_reattempts_alert_next_day(db_session: Session) -> None:
    with patch("app.services.price_capture.send_ops_alert") as alert:
        emit_nav_terminal_diagnostics((_missing_target("513500"),), date(2026, 9, 1), 2)
        emit_nav_terminal_diagnostics((_missing_target("513500"),), date(2026, 9, 2), 2)
    assert [c.kwargs["idempotency_key"] for c in alert.call_args_list] == [
        "ops-fund-nav-empty-513500-2026-09-01",
        "ops-fund-nav-empty-513500-2026-09-02",
    ]


# Close-node rows bind 10 parameters each. PostgreSQL/psycopg hard-cap a
# single query at 65535 parameters, so 6554+ rows in one INSERT overflows
# (issue #194). 7000 rows = 70000 params, past that cap with margin.
_PARAM_OVERFLOW_ROW_COUNT = 7000


def test_upsert_chunks_past_postgres_parameter_limit(db_session: Session) -> None:
    """A batch that used to exceed psycopg's 65535-param cap must still write.

    Production `backfill_ohlcv_task` hit this on 2026-08-25 when a second
    user's holdings widened the system-wide US ticker set enough that
    420 days x tickers x 10 bound params overflowed a single INSERT.
    """
    now = datetime.now(tz=UTC)
    start = date(2000, 1, 1)
    rows: list[dict[str, object]] = [
        {
            "ticker": "BULK",
            "market": "US",
            "session_node": "close",
            "trade_date": start + timedelta(days=i),
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1.0,
            "captured_at": now,
        }
        for i in range(_PARAM_OVERFLOW_ROW_COUNT)
    ]
    # Close-node rows bind one param per dict key. Lock the chunk math the
    # source comment states, derived from this row shape not a frozen 10.
    assert _UPSERT_CHUNK_SIZE * len(rows[0]) <= 65_535

    written = _upsert(db_session, rows)

    assert written == _PARAM_OVERFLOW_ROW_COUNT
    count = db_session.execute(
        select(func.count()).select_from(PriceSnapshot).where(PriceSnapshot.ticker == "BULK")
    ).scalar_one()
    assert count == _PARAM_OVERFLOW_ROW_COUNT


# ---------------------------------------------------------------------------
# Massive.com fallback wiring (issue #56) — close-node US-market only
# ---------------------------------------------------------------------------

_MASSIVE_BAR: OhlcvPoint = (date(2026, 9, 2), 115.55, 117.59, 114.13, 115.97, 1000.0)


def test_capture_close_falls_back_to_massive_for_yfinance_miss(db_session: Session) -> None:
    db_session.add(_holding("Apple", "AAPL", market="US"))
    db_session.flush()

    with (
        patch(
            "app.services.price_capture.get_settings",
            return_value=_settings_with_keys(finnhub=None, massive="k"),
        ),
        patch("app.services.price_capture.fetch_ohlcv_range", return_value={}),
        patch(
            "app.services.price_capture.fetch_massive_prev_close_ohlcv",
            return_value={"AAPL": _MASSIVE_BAR},
        ) as mock_massive,
    ):
        n = capture_prices(db_session, market="US", session_node="close")

    mock_massive.assert_called_once_with(["AAPL"], "k")
    assert n == 1
    row = db_session.execute(
        select(PriceSnapshot).where(PriceSnapshot.ticker == "AAPL")
    ).scalar_one()
    assert row.close == Decimal("115.97")
    assert row.trade_date == date(2026, 9, 2)
    assert row.source == "massive"


def test_capture_close_does_not_call_massive_when_yfinance_succeeds(db_session: Session) -> None:
    db_session.add(_holding("Apple", "AAPL", market="US"))
    db_session.flush()
    ohlcv = {"AAPL": [(date(2026, 6, 5), 200.0, 205.0, 199.0, 203.5, 1000.0)]}

    with (
        patch(
            "app.services.price_capture.get_settings",
            return_value=_settings_with_keys(finnhub=None, massive="k"),
        ),
        patch("app.services.price_capture.fetch_ohlcv_range", return_value=ohlcv),
        patch("app.services.price_capture.fetch_massive_prev_close_ohlcv") as mock_massive,
    ):
        capture_prices(db_session, market="US", session_node="close")

    mock_massive.assert_not_called()


def test_capture_close_does_not_call_massive_when_key_unset(db_session: Session) -> None:
    db_session.add(_holding("Apple", "AAPL", market="US"))
    db_session.flush()

    with (
        patch(
            "app.services.price_capture.get_settings",
            return_value=_settings_with_keys(finnhub=None, massive=None),
        ),
        patch("app.services.price_capture.fetch_ohlcv_range", return_value={}),
        patch("app.services.price_capture.fetch_massive_prev_close_ohlcv") as mock_massive,
    ):
        capture_prices(db_session, market="US", session_node="close")

    mock_massive.assert_not_called()


def test_capture_close_non_us_market_does_not_call_massive(db_session: Session) -> None:
    db_session.add(_holding("Tencent", "0700.HK", market="HK"))
    db_session.flush()

    with (
        patch(
            "app.services.price_capture.get_settings",
            return_value=_settings_with_keys(finnhub=None, massive="k"),
        ),
        patch("app.services.price_capture.fetch_ohlcv_range", return_value={}),
        patch("app.services.price_capture.fetch_massive_prev_close_ohlcv") as mock_massive,
    ):
        capture_prices(db_session, market="HK", session_node="close")

    mock_massive.assert_not_called()


def test_capture_close_missing_check_uses_normalized_ticker(db_session: Session) -> None:
    """Issue #351: a ticker fetch_ohlcv_range normalizes (an un-padded HK
    code like "700.HK" -> the canonical "0700.HK") must not be
    misclassified as missing just because the raw selected ticker doesn't
    match the dict's normalized key — that misclassification wasted a
    Massive fallback call on every capture, even on a clean yfinance hit.
    A declared `market="US"` still routes this holding into the US node's
    worklist (declared market wins in `_effective_market`), independent of
    what the ticker itself normalizes to.
    """
    db_session.add(_holding("Tencent", "700.HK", market="US"))
    db_session.flush()
    ohlcv = {"0700.HK": [(date(2026, 6, 5), 38.0, 39.0, 37.5, 38.5, 1000.0)]}

    with (
        patch(
            "app.services.price_capture.get_settings",
            return_value=_settings_with_keys(finnhub=None, massive="k"),
        ),
        patch("app.services.price_capture.fetch_ohlcv_range", return_value=ohlcv),
        patch("app.services.price_capture.fetch_massive_prev_close_ohlcv") as mock_massive,
    ):
        n = capture_prices(db_session, market="US", session_node="close")

    mock_massive.assert_not_called()
    assert n == 1
    row = db_session.execute(
        select(PriceSnapshot).where(PriceSnapshot.ticker == "0700.HK")
    ).scalar_one()
    assert row.close == Decimal("38.5")


def test_capture_spot_missing_check_uses_normalized_ticker(db_session: Session) -> None:
    """Same normalization mismatch as above, for the non-close (spot) branch
    and its Finnhub fallback (issue #351)."""
    db_session.add(_holding("Tencent", "700.HK", market="US"))
    db_session.flush()

    with (
        patch(
            "app.services.price_capture.get_settings",
            return_value=_settings_with_finnhub_key("k"),
        ),
        patch("app.services.price_capture.fetch_spot", return_value={"0700.HK": 38.9}),
        patch("app.services.price_capture.fetch_finnhub_quotes") as mock_finnhub,
    ):
        n = capture_prices(db_session, market="US", session_node="open")

    mock_finnhub.assert_not_called()
    assert n == 1


def test_capture_close_missing_fallback_never_sends_non_us_wire_symbol(
    db_session: Session,
) -> None:
    """Issue #351 review follow-up (superseded by issue #57 stage 57-2): a
    genuine miss must still be looked up under the normalized ticker
    internally, but that normalized code must never be SENT to a US-only
    fallback when it is not itself US wire syntax. Normalization can leave
    a holding's declared bucket and its lookup code's real venue disagreeing
    (a raw "700.HK" normalizes to the still-HK "0700.HK"); a holding
    declared market=US with raw ticker "700.HK" must not have "0700.HK"
    requested from Massive (frozen design section 4: "Finnhub/Massive
    reject the non-US lookup code even if the historical declared bucket
    says US" — explicitly called out for this exact shape when 57-2 was
    scoped). Before this correction, the fallback was called with the raw
    normalized ticker unconditionally; that regressed the US-only
    eligibility gate for exactly this declared/actual-venue mismatch shape.
    """
    db_session.add(_holding("Tencent", "700.HK", market="US"))
    db_session.flush()

    with (
        patch(
            "app.services.price_capture.get_settings",
            return_value=_settings_with_keys(finnhub=None, massive="k"),
        ),
        patch("app.services.price_capture.fetch_ohlcv_range", return_value={}),
        patch(
            "app.services.price_capture.fetch_massive_prev_close_ohlcv", return_value={}
        ) as mock_massive,
    ):
        capture_prices(db_session, market="US", session_node="close")

    mock_massive.assert_not_called()


def test_capture_spot_missing_fallback_never_sends_non_us_wire_symbol(
    db_session: Session,
) -> None:
    """Same as above for the spot/Finnhub branch (issue #57 stage 57-2)."""
    db_session.add(_holding("Tencent", "700.HK", market="US"))
    db_session.flush()

    with (
        patch(
            "app.services.price_capture.get_settings",
            return_value=_settings_with_finnhub_key("k"),
        ),
        patch("app.services.price_capture.fetch_spot", return_value={}),
        patch("app.services.price_capture.fetch_finnhub_quotes", return_value={}) as mock_finnhub,
    ):
        capture_prices(db_session, market="US", session_node="open")

    mock_finnhub.assert_not_called()


def test_capture_close_missing_fallback_receives_normalized_ticker(db_session: Session) -> None:
    """Issue #351 review follow-up: a genuine miss must still pass the
    fallback the normalized ticker, not the raw stored one, for a code that
    IS itself US wire syntax post-normalization (unlike the HK case
    above). The override table can map a raw ticker to a different symbol —
    querying Massive with the raw form on a real miss would ask for the
    wrong instrument, the same collision class #204 fixed for the primary
    yfinance lookup.
    """
    db_session.add(_holding("Berkshire Hathaway B", "BRK.B", market="US"))
    db_session.flush()

    with (
        patch(
            "app.services.price_capture.get_settings",
            return_value=_settings_with_keys(finnhub=None, massive="k"),
        ),
        patch("app.services.price_capture.fetch_ohlcv_range", return_value={}),
        patch(
            "app.services.price_capture.fetch_massive_prev_close_ohlcv", return_value={}
        ) as mock_massive,
    ):
        capture_prices(db_session, market="US", session_node="close")

    mock_massive.assert_called_once_with(["BRK.B"], "k")


def test_capture_spot_missing_fallback_receives_normalized_ticker(db_session: Session) -> None:
    """Same as above for the spot/Finnhub branch (issue #351 review follow-up)."""
    db_session.add(_holding("Berkshire Hathaway B", "BRK.B", market="US"))
    db_session.flush()

    with (
        patch(
            "app.services.price_capture.get_settings",
            return_value=_settings_with_finnhub_key("k"),
        ),
        patch("app.services.price_capture.fetch_spot", return_value={}),
        patch("app.services.price_capture.fetch_finnhub_quotes", return_value={}) as mock_finnhub,
    ):
        capture_prices(db_session, market="US", session_node="open")

    mock_finnhub.assert_called_once_with(["BRK.B"], "k")


@pytest.mark.parametrize("node", ["pre_open", "open", "after_close"])
def test_capture_spot_nodes_never_call_massive(db_session: Session, node: str) -> None:
    """Massive's free tier structurally cannot serve same-day data — wiring
    it into a spot/intraday node would just burn quota for nothing."""
    db_session.add(_holding("Apple", "AAPL", market="US"))
    db_session.flush()

    with (
        patch(
            "app.services.price_capture.get_settings",
            return_value=_settings_with_keys(finnhub=None, massive="k"),
        ),
        patch("app.services.price_capture.fetch_spot", return_value={}),
        patch("app.services.price_capture.fetch_massive_prev_close_ohlcv") as mock_massive,
    ):
        capture_prices(db_session, market="US", session_node=node)

    mock_massive.assert_not_called()


def test_capture_close_massive_does_not_re_request_ticker_yfinance_already_returned(
    db_session: Session,
) -> None:
    db_session.add_all(
        [_holding("Apple", "AAPL", market="US"), _holding("Microsoft", "MSFT", market="US")]
    )
    db_session.flush()
    ohlcv = {"AAPL": [(date(2026, 6, 5), 200.0, 205.0, 199.0, 203.5, 1000.0)]}

    with (
        patch(
            "app.services.price_capture.get_settings",
            return_value=_settings_with_keys(finnhub=None, massive="k"),
        ),
        patch("app.services.price_capture.fetch_ohlcv_range", return_value=ohlcv),
        patch(
            "app.services.price_capture.fetch_massive_prev_close_ohlcv",
            return_value={"MSFT": _MASSIVE_BAR},
        ) as mock_massive,
    ):
        capture_prices(db_session, market="US", session_node="close")

    mock_massive.assert_called_once_with(["MSFT"], "k")
