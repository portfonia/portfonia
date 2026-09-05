"""Issue #311: new capture nodes, unsupported-market parse, GBp scale, skip."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.models.price_snapshot import PriceSnapshot
from app.services._yfinance import _safe_scaled_price, _scale_price, fetch_last_close
from app.services.holding_parser import _postprocess
from app.services.markets import CAPTURE_MARKET_ORDER
from app.services.price_capture import capture_prices
from app.services.report_prompts import _build_pass2_prompt
from app.services.report_sections import _build_section1
from app.tasks import celery_app
from app.tests.conftest import seed_user

_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")
_AS_OF = datetime(2026, 9, 1, 16, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _seed_user(db_session: Session) -> None:
    seed_user(db_session, _USER)


def _holding(name: str, ticker: str, market: str | None = None, **kw: object) -> Holding:
    data: dict[str, object] = dict(
        user_id=_USER,
        name=name,
        ticker=ticker,
        pricing_mode="auto",
        currency="USD",
        market=market,
        shares=Decimal("10"),
        asset_class="STOCK",
    )
    data.update(kw)
    return Holding(**data)


def _make_hist(ticker: str, price: float) -> pd.DataFrame:
    idx = pd.DatetimeIndex([_AS_OF], name="Date")
    close = pd.DataFrame({ticker: [price]}, index=idx)
    return pd.concat({"Close": close}, axis=1)


class _FakeTicker:
    def __init__(self, symbol: str, *, currency: str, last: float | None = None) -> None:
        self.fast_info = {"currency": currency, "lastPrice": last}


# ---------------------------------------------------------------------------
# 2. independently-scheduled capture nodes
# ---------------------------------------------------------------------------


def test_new_markets_each_have_own_open_and_close_beat_entries() -> None:
    sched = celery_app.conf.beat_schedule
    expected_close = {
        "UK": (16, 30),
        "Europe": (17, 30),
        "Japan": (15, 0),
        "Korea": (15, 30),
    }
    for market, (hour, minute) in expected_close.items():
        assert f"capture-prices-{market}-open" in sched
        assert f"capture-prices-{market}-close" in sched
        assert f"capture-prices-{market}-after_close" not in sched
        close = sched[f"capture-prices-{market}-close"]
        assert close["args"] == (market, "close")
        cron = close["schedule"]
        assert hour in cron.hour and minute in cron.minute


def test_backfill_ohlcv_mirrors_all_seven_capture_markets() -> None:
    from app.tasks.capture_tasks import backfill_ohlcv_task

    with (
        patch("app.core.database.SessionLocal") as mock_session_cls,
        patch("app.services.price_capture.capture_prices", return_value=1) as mock_cap,
    ):
        mock_session_cls.return_value = MagicMock()
        result = backfill_ohlcv_task.run(["VOD.L"])
    assert result == {"written": 7}
    assert [c.args[1] for c in mock_cap.call_args_list] == list(CAPTURE_MARKET_ORDER)


@pytest.mark.parametrize(
    "ticker,market,currency",
    [
        ("VOD.L", "UK", "GBP"),
        ("ASML.AS", "Europe", "EUR"),
        ("7203.T", "Japan", "JPY"),
        ("005930.KS", "Korea", "KRW"),
    ],
)
def test_capture_close_includes_holding_in_that_market_universe(
    db_session: Session, ticker: str, market: str, currency: str
) -> None:
    db_session.add(
        _holding(ticker, ticker, market=market, currency=currency, capture_supported=True)
    )
    db_session.flush()
    ohlcv = {ticker: [(date(2026, 9, 1), 1.0, 1.0, 1.0, 10.0, 1.0)]}
    with patch("app.services.price_capture.fetch_ohlcv_range", return_value=ohlcv) as mock_fetch:
        n = capture_prices(db_session, market=market, session_node="close")
    assert n == 1
    mock_fetch.assert_called_once()
    assert ticker in mock_fetch.call_args.args[0]
    row = db_session.execute(
        select(PriceSnapshot).where(PriceSnapshot.ticker == ticker)
    ).scalar_one()
    assert row.market == market
    assert row.close == Decimal("10.0")


def test_capture_skips_capture_supported_false_no_yfinance(db_session: Session) -> None:
    db_session.add(
        _holding("BHP", "BHP.AX", market="Other", capture_supported=False, currency="AUD")
    )
    db_session.flush()
    with patch("app.services.price_capture.fetch_ohlcv_range") as mock_fetch:
        n = capture_prices(db_session, market="US", session_node="close")
        n_other = capture_prices(db_session, market="Other", session_node="close")
    assert n == 0
    assert n_other == 0
    mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# 3. unresolvable upload row survives
# ---------------------------------------------------------------------------


def test_postprocess_keeps_unresolvable_ticker_as_other_not_processed() -> None:
    rows = _postprocess(
        [
            {
                "name": "BHP Group",
                "ticker": "BHP.AX",
                "currency": "AUD",
                "shares": 10,
                "pricing_mode": "auto",
            }
        ]
    )
    assert len(rows) == 1
    assert rows[0].market == "Other"
    assert rows[0].capture_supported is False
    assert rows[0].ticker == "BHP.AX"


def test_postprocess_maps_lse_ticker_to_uk() -> None:
    rows = _postprocess(
        [
            {
                "name": "Vodafone",
                "ticker": "VOD.L",
                "currency": "GBP",
                "shares": 10,
                "pricing_mode": "auto",
            }
        ]
    )
    assert rows[0].market == "UK"
    assert rows[0].capture_supported is True


# ---------------------------------------------------------------------------
# 6/7. GBp generic scale; EUR/JPY/KRW not scaled
# ---------------------------------------------------------------------------


def test_scale_price_is_generic_gbpence_check() -> None:
    assert _scale_price(7050.0, "GBp") == pytest.approx(70.50)
    assert _scale_price(5894.0, "GBp") == pytest.approx(58.94)
    assert _scale_price(7050.0, "GBP") == pytest.approx(7050.0)
    assert _scale_price(700.0, "EUR") == pytest.approx(700.0)
    assert _scale_price(2500.0, "JPY") == pytest.approx(2500.0)
    assert _scale_price(70000.0, "KRW") == pytest.approx(70000.0)
    assert _scale_price(300.0, "USD") == pytest.approx(300.0)


@pytest.mark.parametrize(
    "ticker,raw,scaled",
    [
        ("VOD.L", 7050.0, 70.50),
        ("BARC.L", 185.4, 1.854),
        ("TSCO.L", 358.0, 3.58),
        ("PSH.L", 5894.0, 58.94),
    ],
)
def test_fetch_last_close_scales_lse_via_generic_gbpence(
    monkeypatch: pytest.MonkeyPatch, ticker: str, raw: float, scaled: float
) -> None:
    def fake_download(**kwargs: object) -> pd.DataFrame:
        return _make_hist(ticker, raw)

    monkeypatch.setattr(
        "app.services._yfinance.yf.Ticker",
        lambda symbol: _FakeTicker(symbol, currency="GBp"),
    )
    with (
        patch("app.services._yfinance.yf.download", side_effect=fake_download),
        patch("app.services._yfinance.time.sleep"),
    ):
        result = fetch_last_close([ticker])
    price, _ = result[ticker]
    assert price == pytest.approx(scaled)


def test_fetch_last_close_omits_lse_bar_when_currency_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #312 B1: unknown GBp currency must not store pence as pounds."""

    def fake_download(**kwargs: object) -> pd.DataFrame:
        return _make_hist("VOD.L", 7050.0)

    monkeypatch.setattr(
        "app.services._yfinance.yf.Ticker",
        lambda symbol: _FakeTicker(symbol, currency=""),
    )
    monkeypatch.setattr(
        "app.services._yfinance._fetched_currency",
        lambda ticker: None,
    )
    with (
        patch("app.services._yfinance.yf.download", side_effect=fake_download),
        patch("app.services._yfinance.time.sleep"),
    ):
        result = fetch_last_close(["VOD.L"])
    assert "VOD.L" not in result
    assert result == {}


def test_safe_scaled_price_treats_empty_string_currency_as_unknown_on_lse() -> None:
    """issue #313 item 2: `_safe_scaled_price`'s fail-closed LSE check only
    tested `currency is None`. `_fetched_currency`'s return type (`str |
    None`) doesn't rule out an actual empty string coming back from
    yfinance, and that value would fall through to the final `return value`
    — identity-scaling pence as pounds, the exact 100x bug class #204/#311
    exist to kill. `""` must omit the bar the same way `None` does."""
    assert _safe_scaled_price("VOD.L", 7050.0, "") is None
    assert _safe_scaled_price("VOD.L", 7050.0, None) is None


def test_fetch_last_close_omits_lse_bar_when_fetched_currency_is_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """issue #313 item 2: unlike the sibling test above, this does NOT
    monkeypatch `_fetched_currency` — it lets the real function run against a
    yfinance response whose `currency` field is `""`, the actual shape #313
    says can reach `_safe_scaled_price` unpatched. `""` must omit the LSE bar
    the same way `None` does, not identity-scale pence as pounds."""

    def fake_download(**kwargs: object) -> pd.DataFrame:
        return _make_hist("VOD.L", 7050.0)

    monkeypatch.setattr(
        "app.services._yfinance.yf.Ticker",
        lambda symbol: _FakeTicker(symbol, currency=""),
    )
    with (
        patch("app.services._yfinance.yf.download", side_effect=fake_download),
        patch("app.services._yfinance.time.sleep"),
    ):
        result = fetch_last_close(["VOD.L"])
    assert result == {}


@pytest.mark.parametrize(
    "ticker,raw,currency",
    [
        ("ASML.AS", 700.0, "EUR"),
        ("7203.T", 2500.0, "JPY"),
        ("005930.KS", 70000.0, "KRW"),
    ],
)
def test_fetch_last_close_does_not_scale_eur_jpy_krw(
    monkeypatch: pytest.MonkeyPatch, ticker: str, raw: float, currency: str
) -> None:
    def fake_download(**kwargs: object) -> pd.DataFrame:
        return _make_hist(ticker, raw)

    monkeypatch.setattr(
        "app.services._yfinance.yf.Ticker",
        lambda symbol: _FakeTicker(symbol, currency=currency),
    )
    with (
        patch("app.services._yfinance.yf.download", side_effect=fake_download),
        patch("app.services._yfinance.time.sleep"),
    ):
        result = fetch_last_close([ticker])
    price, _ = result[ticker]
    assert price == pytest.approx(raw)


def test_ticker_price_scale_table_is_gone() -> None:
    import app.services._yfinance as yf_mod

    assert not hasattr(yf_mod, "_TICKER_PRICE_SCALE")


# ---------------------------------------------------------------------------
# 5. section 1 marker + Pass 2 omit
# ---------------------------------------------------------------------------


def test_section1_uses_market_not_supported_distinct_from_price_unavailable() -> None:
    portfolio = {
        "base_currency": "USD",
        "fx_rates_as_of": {"CNY": "2026-09-01"},
        "total_base": 100.0,
        "by_market": {"US": 100.0},
        "by_currency": {},
        "by_asset_type": {},
        "holdings": [
            {
                "name": "Priced",
                "broker": "IBKR",
                "currency": "USD",
                "market_value": 100,
                "market_value_base": 100.0,
                "position": 0,
                "asset_class": "STOCK",
                "capture_supported": True,
            },
            {
                "name": "BHP Group",
                "ticker": "BHP.AX",
                "broker": "IBKR",
                "currency": "AUD",
                "market_value": None,
                "market_value_base": None,
                "position": 1,
                "asset_class": "STOCK",
                "capture_supported": False,
            },
            {
                "name": "Unpriced US",
                "ticker": "GHOST",
                "broker": "IBKR",
                "currency": "USD",
                "market_value": None,
                "market_value_base": None,
                "position": 2,
                "asset_class": "STOCK",
                "capture_supported": True,
            },
        ],
    }
    md = _build_section1(portfolio)
    bhp_row = next(line for line in md.splitlines() if "BHP Group" in line)
    ghost_row = next(line for line in md.splitlines() if "Unpriced US" in line)
    assert "[market not supported]" in bhp_row
    assert "[price unavailable]" not in bhp_row
    assert "[price unavailable]" in ghost_row
    assert "[market not supported]" not in ghost_row
    assert "**IBKR subtotal** | USD | **100**" in md


def test_pass2_prompt_omits_not_processed_holdings() -> None:
    portfolio = {
        "base_currency": "USD",
        "fx_rates_as_of": {"CNY": "2026-09-01"},
        "total_base": 100.0,
        "by_market": {"US": 100.0},
        "by_currency": {},
        "by_asset_type": {},
        "holdings": [
            {
                "name": "Apple",
                "ticker": "AAPL",
                "currency": "USD",
                "market_value": 100.0,
                "market_value_base": 100.0,
                "asset_class": "STOCK",
                "capture_supported": True,
            },
            {
                "name": "BHP Group",
                "ticker": "BHP.AX",
                "currency": "AUD",
                "market_value": None,
                "market_value_base": None,
                "asset_class": "STOCK",
                "capture_supported": False,
            },
        ],
    }
    prompt = _build_pass2_prompt(portfolio, {}, [], [])
    assert "Apple" in prompt
    assert "BHP.AX" not in prompt
    assert "BHP Group" not in prompt


def test_compute_portfolio_excludes_not_processed_from_aggregates(
    db_session: Session,
) -> None:
    from app.services.portfolio_calculator import compute_portfolio

    db_session.add(
        _holding(
            "Apple",
            "AAPL",
            market="US",
            capture_supported=True,
            currency="USD",
            shares=Decimal("1"),
            market_price=Decimal("100"),
        )
    )
    db_session.add(
        _holding(
            "BHP Group",
            "BHP.AX",
            market="Other",
            capture_supported=False,
            currency="AUD",
            shares=Decimal("10"),
            market_price=Decimal("40"),
        )
    )
    db_session.flush()
    snap = compute_portfolio(db_session, _USER, base_currency="USD")
    assert snap.total_base == Decimal("100.00")
    assert "AUD" not in snap.by_currency
    assert "Other" not in snap.by_market
    names = {h.name for h in snap.holdings}
    assert names == {"Apple", "BHP Group"}
    bhp = next(h for h in snap.holdings if h.name == "BHP Group")
    assert bhp.market_value is None
    assert bhp.capture_supported is False
    assert "BHP.AX" not in snap.stale_tickers
