"""Issue #692 acceptance: freshness verification outside XSHG coverage."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import CST
from app.models.price_snapshot import PriceSnapshot
from app.services._tencent import OhlcBar
from app.services.capture_results import NavFetchError, NavHistoryOutcome, NavPoint
from app.services.china_session_calendar import ChinaSessionWindow
from app.services.price_capture import (
    capture_fund_navs_attempt,
    capture_prices_attempt,
    emit_etf_terminal_diagnostics,
    emit_nav_terminal_diagnostics,
)
from app.tests.conftest import seed_user
from app.tests.test_china_capture_fallback import _USER, _etf, _fund

JAN4 = date(2027, 1, 4)
JAN5 = date(2027, 1, 5)
JAN6 = date(2027, 1, 6)
CODE = "019547"
TICKER = "513500.SS"
WINDOW = ChinaSessionWindow(
    datetime(2027, 1, 6, 20, tzinfo=CST).astimezone(UTC),
    date(2026, 12, 30),
    JAN6,
    None,
    None,
    "calendar_unknown",
)


@pytest.fixture(autouse=True)
def isolated_providers(db_session: Session) -> Iterator[None]:
    seed_user(db_session, _USER)
    sent: set[str] = set()
    with (
        patch(
            "app.services.price_capture.fetch_nav_history_outcome",
            return_value=NavHistoryOutcome(()),
        ),
        patch("app.services.price_capture.fetch_ohlcv_range_bounded", return_value={}),
        patch("app.services.price_capture.sina_latest_nav_point", return_value=None),
        patch("app.services.price_capture.fetch_tencent_daily_bars", return_value={}),
        patch("app.services.price_capture.completed_sessions", return_value=None),
        patch("app.services.price_capture.already_alerted", side_effect=lambda key: key in sent),
        patch(
            "app.services.price_capture.mark_alerted", side_effect=lambda key, ttl: sent.add(key)
        ),
    ):
        yield


def store(session: Session, key: str, day: date) -> None:
    session.add(
        PriceSnapshot(
            ticker=key,
            market="A-Share",
            session_node="close",
            trade_date=day,
            open=Decimal("2"),
            high=Decimal("2"),
            low=Decimal("2"),
            close=Decimal("2"),
            source="yfinance",
            captured_at=datetime.now(tz=UTC),
        )
    )
    session.flush()


def prices(session: Session, key: str) -> dict[date, PriceSnapshot]:
    return {
        row.trade_date: row
        for row in session.scalars(select(PriceSnapshot).where(PriceSnapshot.ticker == key))
    }


def bar(day: date) -> OhlcBar:
    return OhlcBar(day, Decimal("2"), Decimal("2"), Decimal("2"), Decimal("2"))


def anchors() -> dict[str, dict[date, OhlcBar]]:
    return {TICKER: {JAN4: bar(JAN4)}}


def assert_unverified(alert: MagicMock, key: str, source: str) -> None:
    alert.assert_called_once()
    kwargs = alert.call_args.kwargs
    assert kwargs["subject"] == f"[Portfonia] price freshness unverified — {key}"
    assert kwargs["idempotency_key"] == f"ops-price-unverified-{key}-2027-01-06"
    assert kwargs["severity"] == "WARNING"
    body = kwargs["body"]
    assert "stale" not in body.lower() and "missing" not in body.lower()
    assert "fetch failed" not in body.lower()
    assert "XSHG" in body and source in body and "2027-01-04" in body
    assert "fallback did not change stored rows" in body
    assert "worker.log" in body


@pytest.mark.parametrize("allow_fallback", [False, True])
def test_nav_newer_point_stored_same_attempt(db_session: Session, allow_fallback: bool) -> None:
    db_session.add(_fund(CODE))
    store(db_session, CODE, JAN4)
    with (
        patch(
            "app.services.price_capture.sina_latest_nav_point",
            return_value=NavPoint(JAN5, Decimal("2.1"), "sina"),
        ) as sina,
        patch("app.services.price_capture.send_ops_alert") as alert,
    ):
        outcome = capture_fund_navs_attempt(
            db_session, window=WINDOW, allow_fallback=allow_fallback
        )
    assert JAN5 in prices(db_session, CODE)
    assert prices(db_session, CODE)[JAN5].source == "sina"
    assert outcome.unresolved == ()
    assert outcome.recovered == (CODE,)
    assert outcome.history_coverage == {CODE: "latest_only"}
    sina.assert_called_once()
    alert.assert_not_called()


def test_nav_same_date_is_verified_without_write(db_session: Session) -> None:
    db_session.add(_fund(CODE))
    store(db_session, CODE, JAN4)
    with (
        patch(
            "app.services.price_capture.sina_latest_nav_point",
            return_value=NavPoint(JAN4, Decimal("9"), "sina"),
        ) as sina,
        patch("app.services.price_capture.send_ops_alert") as alert,
    ):
        outcome = capture_fund_navs_attempt(db_session, window=WINDOW)
    sina.assert_called_once()
    assert outcome.written == 0 and outcome.unresolved == ()
    assert prices(db_session, CODE)[JAN4].close == Decimal("2")
    assert outcome.recovered == ()
    alert.assert_not_called()


def test_nav_unverified_retries_then_deduped_alert(db_session: Session) -> None:
    db_session.add(_fund(CODE))
    store(db_session, CODE, JAN4)
    with (
        patch("app.services.price_capture.sina_latest_nav_point", return_value=None) as sina,
        patch("app.services.price_capture.send_ops_alert", return_value=True) as alert,
    ):
        for attempt in range(3):
            outcome = capture_fund_navs_attempt(
                db_session, window=WINDOW, only_keys=(CODE,), allow_fallback=attempt == 2
            )
            assert [t.reason for t in outcome.unresolved] == ["calendar_unknown_unverified"]
            assert outcome.unresolved[0].latest_date == JAN4
        assert sina.call_count == 3
        alert.assert_not_called()
        for _ in range(2):
            emit_nav_terminal_diagnostics(outcome.unresolved, WINDOW.window_end, 2)
        assert_unverified(alert, CODE, "Sina")
    assert set(prices(db_session, CODE)) == {JAN4}


@pytest.mark.parametrize("allow_fallback", [False, True])
def test_etf_newer_bar_stored_same_attempt(db_session: Session, allow_fallback: bool) -> None:
    db_session.add(_etf())
    store(db_session, TICKER, JAN4)
    raw = {d: bar(d) for d in (JAN4, JAN5)}
    with (
        patch(
            "app.services.price_capture.fetch_tencent_daily_bars", side_effect=[raw, raw]
        ) as tencent,
        patch("app.services.price_capture.send_ops_alert") as alert,
    ):
        outcome, _ = capture_prices_attempt(
            db_session,
            "A-Share",
            "close",
            window=WINDOW,
            allow_fallback=allow_fallback,
            yahoo_anchors=anchors(),
        )
    assert JAN5 in prices(db_session, TICKER)
    assert prices(db_session, TICKER)[JAN5].source == "tencent"
    assert outcome.unresolved == () and outcome.recovered == (TICKER,)
    assert [call.args for call in tencent.call_args_list] == [
        ("sh513500", WINDOW.window_start, JAN6, "raw"),
        ("sh513500", WINDOW.window_start, JAN6, "qfq"),
    ]
    alert.assert_not_called()


def test_etf_both_empty_retries_then_deduped_unverified_alert(db_session: Session) -> None:
    db_session.add(_etf())
    store(db_session, TICKER, JAN4)
    with (
        patch("app.services.price_capture.fetch_tencent_daily_bars", return_value={}) as tencent,
        patch("app.services.price_capture.send_ops_alert", return_value=True) as alert,
    ):
        for attempt in range(3):
            outcome, _ = capture_prices_attempt(
                db_session,
                "A-Share",
                "close",
                window=WINDOW,
                only_tickers=(TICKER,),
                allow_fallback=attempt == 2,
            )
            assert [t.reason for t in outcome.unresolved] == ["calendar_unknown_unverified"]
            assert outcome.unresolved[0].missing_dates == ()
            assert outcome.written == 0
        assert tencent.call_count == 6
        alert.assert_not_called()
        for _ in range(2):
            emit_etf_terminal_diagnostics(outcome.unresolved)
        assert_unverified(alert, TICKER, "Tencent")
    assert set(prices(db_session, TICKER)) == {JAN4}


@pytest.mark.parametrize(
    "raw_days,qfq_days",
    [
        ((JAN4, JAN5), (JAN4,)),
        ((JAN4,), (JAN4, JAN5)),
        ((JAN4, JAN5, JAN6), (JAN4, JAN6)),
        ((JAN4, JAN5), ()),
    ],
)
def test_etf_asymmetric_dates_reject_admission(
    db_session: Session, raw_days: tuple[date, ...], qfq_days: tuple[date, ...]
) -> None:
    db_session.add(_etf())
    store(db_session, TICKER, JAN4)
    # An already-stored asymmetric date must still participate in interval validation.
    if JAN6 in raw_days:
        store(db_session, TICKER, JAN5)
    with patch(
        "app.services.price_capture.fetch_tencent_daily_bars",
        side_effect=[{d: bar(d) for d in raw_days}, {d: bar(d) for d in qfq_days}],
    ):
        outcome, _ = capture_prices_attempt(
            db_session, "A-Share", "close", window=WINDOW, yahoo_anchors=anchors()
        )
    assert [t.reason for t in outcome.unresolved] == ["unsupported_adjustment"]
    assert outcome.written == 0
    assert all(row.source != "tencent" for row in prices(db_session, TICKER).values())


def test_etf_all_provider_dates_stored_is_verified(db_session: Session) -> None:
    db_session.add(_etf())
    store(db_session, TICKER, JAN4)
    with (
        patch(
            "app.services.price_capture.fetch_tencent_daily_bars", return_value={JAN4: bar(JAN4)}
        ) as tencent,
        patch("app.services.price_capture.send_ops_alert") as alert,
    ):
        outcome, _ = capture_prices_attempt(db_session, "A-Share", "close", window=WINDOW)
    assert tencent.call_count == 2
    assert outcome.written == 0 and outcome.unresolved == ()
    alert.assert_not_called()


def test_covered_current_data_calls_neither_fallback(db_session: Session) -> None:
    db_session.add_all([_fund(CODE), _etf()])
    store(db_session, TICKER, JAN4)
    covered = replace(WINDOW, calendar_status="ok", latest_session=JAN4, cutoff=JAN4)
    with (
        patch(
            "app.services.price_capture.fetch_nav_history_outcome",
            return_value=NavHistoryOutcome((NavPoint(JAN4, Decimal("2"), "eastmoney"),)),
        ),
        patch("app.services.price_capture.completed_sessions", return_value=(JAN4,)),
        patch("app.services.price_capture.sina_latest_nav_point") as sina,
        patch("app.services.price_capture.fetch_tencent_daily_bars") as tencent,
    ):
        nav = capture_fund_navs_attempt(db_session, window=covered, allow_fallback=True)
        etf, _ = capture_prices_attempt(
            db_session, "A-Share", "close", window=covered, allow_fallback=True
        )
    assert nav.unresolved == () and etf.unresolved == ()
    sina.assert_not_called()
    tencent.assert_not_called()


def test_unknown_unsupported_currency_keeps_no_target(db_session: Session) -> None:
    fund = _fund(CODE)
    fund.currency = "USD"
    db_session.add(fund)
    store(db_session, CODE, JAN4)
    with patch("app.services.price_capture.sina_latest_nav_point") as sina:
        outcome = capture_fund_navs_attempt(db_session, window=WINDOW, allow_fallback=True)
    assert outcome.unresolved == ()
    sina.assert_not_called()


@pytest.mark.parametrize("error,reason", [(None, "missing"), ("transport", "transport")])
def test_no_nav_preserves_primary_reason(
    db_session: Session, error: NavFetchError | None, reason: str
) -> None:
    db_session.add(_fund(CODE))
    db_session.flush()
    with (
        patch(
            "app.services.price_capture.fetch_nav_history_outcome",
            return_value=NavHistoryOutcome((), error),
        ),
        patch("app.services.price_capture.sina_latest_nav_point") as sina,
    ):
        outcome = capture_fund_navs_attempt(db_session, window=WINDOW)
    assert [t.reason for t in outcome.unresolved] == [reason]
    sina.assert_not_called()


def test_no_observed_etf_keeps_existing_unknown_target(db_session: Session) -> None:
    db_session.add(_etf())
    db_session.flush()
    with patch("app.services.price_capture.fetch_tencent_daily_bars") as tencent:
        outcome, _ = capture_prices_attempt(
            db_session, "A-Share", "close", window=WINDOW, allow_fallback=True
        )
    assert [t.reason for t in outcome.unresolved] == ["calendar_unknown"]
    tencent.assert_not_called()
