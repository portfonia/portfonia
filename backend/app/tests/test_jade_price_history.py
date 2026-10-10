"""Issue #714 cache fill, paged NAV, Beat and migration contract."""

import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import cast
from unittest.mock import Mock

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from alembic import command
from app.core.config import get_settings
from app.models.jade_price import JadePricePoint, JadePriceSeries
from app.services.fund_nav_fetcher import fetch_nav_history_pages
from app.services.jade_price_history import refresh_jade_price_history
from app.services.jade_replay_config import FIXED_ETF_SYMBOLS
from app.tests.conftest import TEST_USER_ID, U1_USER_ID, seed_user
from app.tests.test_jade_replay import calendar, compute, holding

HTTP_CLIENT = httpx.Client
TODAY = date(2026, 10, 8)


@pytest.fixture(autouse=True)
def replay_fill_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep replay provider fixtures scoped to replay; #720 tests the combined run."""
    from app.services.jade_price_history import FillSummary

    monkeypatch.setattr(
        "app.services.jade_price_history.fill_scenarios", Mock(return_value=FillSummary())
    )


def response(rows: list[dict[str, str]], code: int = 0) -> httpx.Response:
    return httpx.Response(200, json={"ErrCode": code, "Data": {"LSJZList": rows}})


def row(day: str, nav: str, fhsp: str = "") -> dict[str, str]:
    return dict(FSRQ=day, DWJZ=nav, FHSP=fhsp)


def client_with(pages: list[httpx.Response]) -> tuple[httpx.Client, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return pages[len(calls) - 1]

    return HTTP_CLIENT(transport=httpx.MockTransport(handle)), calls


def test_a9_paging_sorted_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    pause = Mock()
    monkeypatch.setattr("app.services.fund_nav_fetcher.time.sleep", pause)
    client, calls = client_with(
        [
            response([row("2026-10-08", "1.2"), row("2026-10-07", "1.1")]),
            response([row("2026-10-07", "1.1"), row("2026-10-06", "1")]),
            response([]),
        ]
    )
    with client:
        rows = fetch_nav_history_pages("161725", client, date(2021, 10, 8), TODAY, None)
    assert rows is not None and [r.nav_date for r in rows] == [
        date(2026, 10, 6),
        date(2026, 10, 7),
        TODAY,
    ]
    assert [r.url.params["pageIndex"] for r in calls] == ["1", "2", "3"]
    assert all(
        r.url.params["pageSize"] == "20"
        and r.url.params["startDate"] == "2021-10-08"
        and r.url.params["endDate"] == "2026-10-08"
        for r in calls
    )
    assert pause.call_count == 2


@pytest.mark.parametrize("failure", ["provider", "transport", "json", "row"])
def test_a8_nav_failure(failure: str) -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        if failure == "transport":
            raise httpx.ConnectError("offline", request=req)
        if failure == "json":
            return httpx.Response(200, text="not json")
        if failure == "row":
            return response([row("invalid", "1")])
        return response([], 1)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert fetch_nav_history_pages("161725", client, date(2021, 10, 8), TODAY, None) is None


def mocks(
    monkeypatch: pytest.MonkeyPatch, pages: list[httpx.Response]
) -> tuple[Mock, list[httpx.Request]]:
    client, calls = client_with(pages)
    monkeypatch.setattr("app.services.jade_price_history.httpx.Client", lambda: client)
    yf = Mock(return_value={})
    monkeypatch.setattr("app.services.jade_price_history.fetch_ohlcv_range_bounded", yf)
    monkeypatch.setattr("app.services.fund_nav_fetcher.time.sleep", Mock())
    return yf, calls


def jade(session: Session) -> None:
    u = seed_user(session, TEST_USER_ID)
    u.subscription_status = "active"
    u.subscription_type = "jade"
    session.flush()


def test_d7_5_a8_fund_index_incremental(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    jade(db_session)
    holding(db_session, auto=True, ticker=None, fund_code="161725", asset_type="fund")
    yf, calls = mocks(
        monkeypatch,
        [
            response(
                [row("2026-10-07", "1.4500", "每10份派现金0.5000元"), row("2026-10-06", "1.5000")]
            ),
            response([]),
        ],
    )
    refresh_jade_price_history(db_session, TODAY)
    pts = list(
        db_session.scalars(
            select(JadePricePoint)
            .where(JadePricePoint.series_key == "nav:161725")
            .order_by(JadePricePoint.trade_date)
        )
    )
    assert [p.close for p in pts] == [Decimal("1.5000"), Decimal("1.5000")]
    assert len(calls) == 2
    _, calls = mocks(
        monkeypatch,
        [
            response(
                [row("2026-10-08", "1.4790"), row("2026-10-07", "1.4500", "每10份派现金0.5000元")]
            )
        ],
    )
    refresh_jade_price_history(db_session, TODAY)
    p = db_session.get(JadePricePoint, ("nav:161725", TODAY))
    assert p and p.close == Decimal("1.530000") and p.raw_close == Decimal("1.4790")
    assert len(calls) == 1
    s = db_session.get(JadePriceSeries, "nav:161725")
    assert s and s.last_attempt_on == s.last_success_on == TODAY
    assert yf.call_args.args[1] == date(2021, 9, 23)


def test_d7_6_unparsed_distribution_no_recovery(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    jade(db_session)
    holding(db_session, auto=True, ticker=None, fund_code="161725", asset_type="fund")
    yf, _ = mocks(
        monkeypatch,
        [
            response([row("2026-10-08", "1.4", "unknown distribution"), row("2026-10-07", "1.5")]),
            response([]),
        ],
    )
    calendar(db_session, [TODAY])
    monkeypatch.setattr("app.services.jade_replay.today_et", lambda: TODAY)
    yf.return_value = {"SPY": [(TODAY, 100, 100, 100, 100, 1)]}
    logging.getLogger("app.services.jade_price_history").disabled = False
    with caplog.at_level(logging.WARNING):
        refresh_jade_price_history(db_session, TODAY)
    s = db_session.get(JadePriceSeries, "nav:161725")
    assert s and s.unusable_reason == "unparsed_distribution"
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(JadePricePoint)
            .where(JadePricePoint.series_key == "nav:161725")
        )
        == 0
    )
    assert "161725" in caplog.text and "unknown distribution" in caplog.text
    replay_row = compute(db_session).holdings[0]
    assert replay_row.method == "proxy" and replay_row.own_history_unavailable
    assert replay_row.proxy_symbol == "SPY"
    _, calls = mocks(monkeypatch, [response([row("2026-10-08", "1.4")]), response([])])
    refresh_jade_price_history(db_session, TODAY)
    assert calls == [] and s.unusable_reason == "unparsed_distribution"


def test_a8_failed_nav_keeps_existing(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    jade(db_session)
    holding(db_session, auto=True, ticker=None, fund_code="161725", asset_type="fund")
    db_session.add(JadePriceSeries(series_key="nav:161725"))
    db_session.flush()
    db_session.add(
        JadePricePoint(
            series_key="nav:161725",
            trade_date=TODAY - timedelta(days=1),
            close=Decimal("1.5"),
            raw_close=Decimal("1.5"),
        )
    )
    db_session.flush()
    mocks(monkeypatch, [response([], 1)])
    refresh_jade_price_history(db_session, TODAY)
    point = db_session.get(JadePricePoint, ("nav:161725", TODAY - timedelta(days=1)))
    assert point is not None and point.close == Decimal("1.5")
    assert (
        cast(JadePriceSeries, db_session.get(JadePriceSeries, "nav:161725")).last_success_on is None
    )


def test_a8_yf_upsert_needed_series(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    jade(db_session)
    holding(db_session, auto=True, ticker="00700.HK")
    manual = holding(db_session, ticker="MANUAL")
    unsupported = holding(db_session, auto=True, ticker="UNSUPPORTED")
    unsupported.capture_supported = False
    other = seed_user(db_session, U1_USER_ID)
    other.subscription_status = "active"
    other.subscription_type = "daily"
    h = holding(db_session, auto=True, ticker="OTHERUSER")
    h.user_id = U1_USER_ID
    db_session.add(JadePriceSeries(series_key="yf:SPY", last_success_on=TODAY - timedelta(days=1)))
    db_session.flush()
    db_session.add(JadePricePoint(series_key="yf:SPY", trade_date=TODAY, close=Decimal(90)))
    db_session.flush()
    yf, _ = mocks(monkeypatch, [])
    yf.return_value = {"0700.HK": [(TODAY, 100, 100, 100, 100, 1)]}
    result = refresh_jade_price_history(db_session, TODAY)
    requested = set(yf.call_args.args[0])
    assert requested == FIXED_ETF_SYMBOLS | {"0700.HK"}
    assert yf.call_args.args[2] == TODAY + timedelta(days=1)
    assert cast(JadePricePoint, db_session.get(JadePricePoint, ("yf:0700.HK", TODAY))).close == 100
    assert cast(JadePricePoint, db_session.get(JadePricePoint, ("yf:SPY", TODAY))).close == 90
    s = cast(JadePriceSeries, db_session.get(JadePriceSeries, "yf:SPY"))
    assert s.last_attempt_on == TODAY and s.last_success_on == TODAY - timedelta(days=1)
    yf.return_value = {"0700.HK": [(TODAY, 110, 110, 110, 110, 1)]}
    refresh_jade_price_history(db_session, TODAY)
    assert cast(JadePricePoint, db_session.get(JadePricePoint, ("yf:0700.HK", TODAY))).close == 110
    assert result.written == 1 and manual.current_value == 100


def test_d7_10_no_jade(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    yf, calls = mocks(monkeypatch, [])
    assert refresh_jade_price_history(db_session, TODAY).attempted == 0
    yf.assert_not_called()
    assert not calls
    assert db_session.scalar(select(func.count()).select_from(JadePriceSeries)) == 0


def test_a10_migration(alembic_cfg: Config) -> None:
    command.upgrade(alembic_cfg, "d71400000001")
    engine = create_engine(get_settings().database_url)
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "d71400000001"
        assert conn.scalar(text("SELECT to_regclass('jade_price_points')")) == "jade_price_points"
    command.downgrade(alembic_cfg, "d71000000001")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT to_regclass('jade_price_points')")) is None
    command.upgrade(alembic_cfg, "head")
    engine.dispose()


def test_beat_and_leap_window() -> None:
    from app.services.jade_replay_config import years_before
    from app.tasks import API_QUIET_BEAT_ENTRIES, celery_app

    entry = celery_app.conf.beat_schedule["refresh-jade-price-history-daily"]
    assert entry["task"] == "app.tasks.jade_tasks.refresh_jade_price_history_task"
    assert entry["schedule"].hour == {22} and entry["schedule"].minute == {15}
    assert API_QUIET_BEAT_ENTRIES["refresh-jade-price-history-daily"] is True
    assert "app.tasks.jade_tasks" in celery_app.conf.include
    assert years_before(date(2024, 2, 29), 5) == date(2019, 2, 28)


def test_a8_download_adjusted_major_units(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the existing yfinance adapter, mocking only its outbound calls."""
    import pandas as pd

    from app.services import _yfinance

    jade(db_session)
    holding(db_session, auto=True, ticker="VOD.L", market="UK")

    def download(**kwargs: object) -> pd.DataFrame:
        assert db_session.get(JadePriceSeries, "yf:VOD.L") is not None
        assert kwargs["auto_adjust"] is True
        assert kwargs["start"] == "2021-09-23" and kwargs["end"] == "2026-10-09"
        tickers = str(kwargs["tickers"]).split()
        columns = pd.MultiIndex.from_product([["Open", "High", "Low", "Close", "Volume"], tickers])
        return pd.DataFrame(
            [[100.0] * len(columns)], index=pd.to_datetime([TODAY]), columns=columns
        )

    mock = Mock(side_effect=download)
    monkeypatch.setattr("app.services._yfinance.yf.download", mock)
    ticker = Mock()
    ticker.fast_info = {"currency": "GBp"}
    monkeypatch.setattr("app.services._yfinance.yf.Ticker", Mock(return_value=ticker))
    monkeypatch.setattr(_yfinance, "_inter_batch_sleep", Mock())
    monkeypatch.setattr("app.services._yfinance.time.sleep", Mock())
    refresh_jade_price_history(db_session, TODAY)
    point = db_session.get(JadePricePoint, ("yf:VOD.L", TODAY))
    assert point is not None and point.close == Decimal("1") and point.raw_close is None
    assert mock.call_count > 0
