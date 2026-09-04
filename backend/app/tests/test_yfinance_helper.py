"""Unit tests for the shared yfinance helper (_yfinance.py).

No database required — all tests mock yf.download and time.sleep.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import patch

import pandas as pd
import pytest

from app.services._yfinance import (
    _MAX_BATCH_SIZE,
    _chunk,
    _download_batch,
    _market_key_for_ticker,
    _normalize_ticker,
    _quiet_yfinance_logs,
    _raw_download,
    _scale_price,
    fetch_last_close,
    fetch_ohlcv_range,
    fetch_spot,
)

_AS_OF = datetime(2026, 6, 4, 20, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _stub_yf_currency(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not hit live yfinance for the generic GBp currency lookup."""

    class _Ticker:
        def __init__(self, symbol: str) -> None:
            self.fast_info = {"currency": "USD"}

    monkeypatch.setattr("app.services._yfinance.yf.Ticker", _Ticker)


# ---------------------------------------------------------------------------
# _market_key_for_ticker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ticker,expected",
    [
        ("AAPL", "us"),
        ("VOO", "us"),
        ("USDCNY=X", "us"),  # FX tickers have no market suffix
        ("0700.HK", "hk"),
        ("9988.HK", "hk"),
        ("600519.SS", "cn"),
        ("000858.SZ", "cn"),
        ("513650.SS", "cn"),
        # Case-insensitive
        ("0700.hk", "hk"),
        ("600519.ss", "cn"),
        ("VOD.L", "uk"),
        ("ASML.AS", "europe"),
        ("MC.PA", "europe"),
        ("SAP.DE", "europe"),
        ("7203.T", "japan"),
        ("005930.KS", "korea"),
        ("035420.KQ", "korea"),
        ("BHP.AX", "other"),
        ("SHOP.TO", "other"),
    ],
)
def test_market_key_for_ticker(ticker: str, expected: str) -> None:
    assert _market_key_for_ticker(ticker) == expected


# ---------------------------------------------------------------------------
# _chunk
# ---------------------------------------------------------------------------


def test_chunk_exact_multiple() -> None:
    assert _chunk(["a", "b", "c", "d"], 2) == [["a", "b"], ["c", "d"]]


def test_chunk_with_remainder() -> None:
    result = _chunk(["a", "b", "c", "d", "e"], 2)
    assert result == [["a", "b"], ["c", "d"], ["e"]]


def test_chunk_smaller_than_size() -> None:
    assert _chunk(["a", "b"], 8) == [["a", "b"]]


def test_chunk_empty() -> None:
    assert _chunk([], 8) == []


# ---------------------------------------------------------------------------
# fetch_last_close — market splitting and batch chunking
# ---------------------------------------------------------------------------


def _make_hist(ticker: str, price: float) -> pd.DataFrame:
    """Build a minimal yfinance-style DataFrame for one ticker."""
    idx = pd.DatetimeIndex([_AS_OF], name="Date")
    close = pd.DataFrame({ticker: [price]}, index=idx)
    return pd.concat({"Close": close}, axis=1)


# ---------------------------------------------------------------------------
# _normalize_ticker (issue #204: known bare-ticker collisions on yfinance)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ticker,expected",
    [
        # PSH: bare ticker collides with an unrelated US-listed ETF on
        # yfinance; the real Pershing Square Holdings trades on the LSE.
        ("PSH", "PSH.L"),
        ("psh", "PSH.L"),
        # Non-overridden tickers pass through unchanged.
        ("AAPL", "AAPL"),
        # HK normalization still composes through this function.
        ("02333.HK", "2333.HK"),
    ],
)
def test_normalize_ticker(ticker: str, expected: str) -> None:
    assert _normalize_ticker(ticker) == expected


def test_fetch_last_close_empty_input() -> None:
    assert fetch_last_close([]) == {}


def test_fetch_last_close_normalizes_known_collision_ticker() -> None:
    """A bare 'PSH' request must be resolved as PSH.L, not the collision ticker."""
    call_record: list[list[str]] = []

    def fake_download(**kwargs: object) -> pd.DataFrame:
        tickers_arg = str(kwargs["tickers"]).split()
        call_record.append(tickers_arg)
        return _make_hist(tickers_arg[0], 5900.0)

    with (
        patch("app.services._yfinance.yf.download", side_effect=fake_download),
        patch("app.services._yfinance.time.sleep"),
    ):
        result = fetch_last_close(["PSH"])

    assert call_record == [["PSH.L"]]
    assert set(result.keys()) == {"PSH.L"}


def test_scale_price_converts_gbx_tickers_to_gbp() -> None:
    """issue #204/#311: yfinance quotes LSE names in GBX (currency == GBp).
    Scale is generic — not a per-ticker table."""
    assert _scale_price(5894.0, "GBp") == pytest.approx(58.94)
    assert _scale_price(300.0, "USD") == 300.0
    assert _scale_price(700.0, "EUR") == 700.0


def test_fetch_last_close_scales_psh_l_from_pence_to_pounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raw PSH.L close of 5894 (GBX) must come back as 58.94 (GBP)."""

    def fake_download(**kwargs: object) -> pd.DataFrame:
        return _make_hist("PSH.L", 5894.0)

    class _Ticker:
        def __init__(self, symbol: str) -> None:
            self.fast_info = {"currency": "GBp"}

    monkeypatch.setattr("app.services._yfinance.yf.Ticker", _Ticker)
    with (
        patch("app.services._yfinance.yf.download", side_effect=fake_download),
        patch("app.services._yfinance.time.sleep"),
    ):
        result = fetch_last_close(["PSH"])

    price, _ = result["PSH.L"]
    assert price == pytest.approx(58.94)


def test_fetch_spot_normalizes_and_scales_known_collision_ticker() -> None:
    """fetch_spot previously queried yfinance with the raw, un-normalized
    ticker — a bare 'PSH' would hit the wrong instrument here even after the
    close-node path was fixed. Must normalize to PSH.L and scale GBX→GBP,
    same as fetch_last_close/fetch_ohlcv_range."""

    class _FakeTicker:
        def __init__(self, symbol: str) -> None:
            assert symbol == "PSH.L", f"fetch_spot queried un-normalized ticker {symbol!r}"
            self.fast_info = {"lastPrice": 3930.0, "currency": "GBp"}

    with patch("app.services._yfinance.yf.Ticker", side_effect=_FakeTicker):
        result = fetch_spot(["PSH"])

    assert set(result.keys()) == {"PSH.L"}
    assert result["PSH.L"] == pytest.approx(39.30)


def test_fetch_last_close_splits_into_market_batches() -> None:
    """US / HK / A-share tickers must each go into a separate yf.download call."""
    us_ticker = "AAPL"
    hk_ticker = "0700.HK"
    cn_ticker = "600519.SS"

    call_record: list[list[str]] = []

    def fake_download(**kwargs: object) -> pd.DataFrame:
        tickers_arg = str(kwargs["tickers"]).split()
        call_record.append(tickers_arg)
        # Return a valid single-ticker DataFrame for whichever ticker was requested.
        ticker = tickers_arg[0]
        return _make_hist(ticker, 100.0)

    with (
        patch("app.services._yfinance.yf.download", side_effect=fake_download),
        patch("app.services._yfinance.time.sleep"),
    ):
        result = fetch_last_close([us_ticker, hk_ticker, cn_ticker])

    # Three separate calls, one per market.
    assert len(call_record) == 3
    assert [us_ticker] in call_record
    assert [hk_ticker] in call_record
    assert [cn_ticker] in call_record
    # All three tickers returned.
    assert set(result.keys()) == {us_ticker, hk_ticker, cn_ticker}


def test_fetch_last_close_respects_max_batch_size() -> None:
    """A single-market list longer than _MAX_BATCH_SIZE must be split."""
    # Build a list of US tickers one larger than the limit.
    tickers = [f"T{i:02d}" for i in range(_MAX_BATCH_SIZE + 1)]

    call_record: list[list[str]] = []

    def fake_download(**kwargs: object) -> pd.DataFrame:
        batch = str(kwargs["tickers"]).split()
        call_record.append(batch)
        return _make_hist(batch[0], 50.0)

    with (
        patch("app.services._yfinance.yf.download", side_effect=fake_download),
        patch("app.services._yfinance.time.sleep"),
    ):
        fetch_last_close(tickers)

    # Must be split into 2 batches: [_MAX_BATCH_SIZE] + [1].
    assert len(call_record) == 2
    assert len(call_record[0]) == _MAX_BATCH_SIZE
    assert len(call_record[1]) == 1


def test_fetch_last_close_inter_batch_delay_called() -> None:
    """time.sleep must be called between batches (not before the first)."""
    tickers = ["AAPL", "0700.HK"]  # two different markets → two batches

    def fake_download(**kwargs: object) -> pd.DataFrame:
        t = str(kwargs["tickers"]).split()[0]
        return _make_hist(t, 1.0)

    with (
        patch("app.services._yfinance.yf.download", side_effect=fake_download),
        patch("app.services._yfinance.time.sleep") as mock_sleep,
    ):
        fetch_last_close(tickers)

    # One inter-batch pause for two batches (no pause before the first).
    mock_sleep.assert_called_once()


def test_fetch_last_close_single_batch_no_delay() -> None:
    """No inter-batch delay when all tickers fit in one batch."""
    tickers = ["AAPL", "MSFT"]  # both US, fits in one batch

    def fake_download(**kwargs: object) -> pd.DataFrame:
        idx = pd.DatetimeIndex([_AS_OF])
        close = pd.DataFrame({"AAPL": [310.0], "MSFT": [420.0]}, index=idx)
        return pd.concat({"Close": close}, axis=1)

    with (
        patch("app.services._yfinance.yf.download", side_effect=fake_download),
        patch("app.services._yfinance.time.sleep") as mock_sleep,
    ):
        fetch_last_close(tickers)

    mock_sleep.assert_not_called()


def test_fetch_last_close_failed_batch_omitted() -> None:
    """Tickers from a batch where yf.download raises are silently omitted."""
    tickers = ["AAPL", "0700.HK"]

    def fake_download(**kwargs: object) -> pd.DataFrame:
        if "0700.HK" in str(kwargs["tickers"]):
            raise OSError("network error")
        return _make_hist("AAPL", 310.0)

    with (
        patch("app.services._yfinance.yf.download", side_effect=fake_download),
        patch("app.services._yfinance.time.sleep"),
    ):
        result = fetch_last_close(tickers)

    assert "AAPL" in result
    assert "0700.HK" not in result


# ---------------------------------------------------------------------------
# _download_batch — edge cases
# ---------------------------------------------------------------------------


def test_download_batch_empty_input() -> None:
    assert _download_batch([]) == {}


def test_download_batch_empty_hist_returns_empty() -> None:
    with patch("app.services._yfinance.yf.download", return_value=pd.DataFrame()):
        assert _download_batch(["AAPL"]) == {}


def test_download_batch_all_nan_series_omitted() -> None:
    import numpy as np

    idx = pd.DatetimeIndex([_AS_OF])
    close = pd.DataFrame({"AAPL": [np.nan]}, index=idx)
    hist = pd.concat({"Close": close}, axis=1)

    with patch("app.services._yfinance.yf.download", return_value=hist):
        result = _download_batch(["AAPL"])

    assert result == {}


# ---------------------------------------------------------------------------
# _quiet_yfinance_logs — log-noise suppression (issue #56)
# ---------------------------------------------------------------------------

_YF_LOGGER = logging.getLogger("yfinance")


@pytest.fixture(autouse=True)
def _restore_yf_logger_level() -> Iterator[None]:
    """Every test in this module must leave the real `yfinance` logger as it
    found it, regardless of what the test under test does to it."""
    original = _YF_LOGGER.level
    yield
    _YF_LOGGER.setLevel(original)


@pytest.fixture(autouse=True)
def _reenable_module_logger_for_caplog() -> None:
    """A db_session-using test elsewhere in the suite runs `alembic upgrade`,
    whose fileConfig() defaults disable_existing_loggers=True and silently
    disables this already-imported module's logger regardless of test file
    or run order — re-enable so caplog can see telemetry records (same
    mechanism as test_fund_nav_fetcher.py)."""
    logging.getLogger("app.services._yfinance").disabled = False


def test_quiet_yfinance_logs_demotes_to_critical_during_the_block() -> None:
    _YF_LOGGER.setLevel(logging.WARNING)
    with _quiet_yfinance_logs():
        assert _YF_LOGGER.level == logging.CRITICAL
    assert _YF_LOGGER.level == logging.WARNING


def test_quiet_yfinance_logs_restores_level_on_exception() -> None:
    _YF_LOGGER.setLevel(logging.WARNING)
    with pytest.raises(RuntimeError), _quiet_yfinance_logs():
        assert _YF_LOGGER.level == logging.CRITICAL
        raise RuntimeError("boom")
    assert _YF_LOGGER.level == logging.WARNING


def test_raw_download_suppresses_yfinance_logger_during_call() -> None:
    _YF_LOGGER.setLevel(logging.WARNING)
    seen_levels: list[int] = []

    def fake_download(**kwargs: object) -> pd.DataFrame:
        seen_levels.append(_YF_LOGGER.level)
        return _make_hist("AAPL", 100.0)

    with patch("app.services._yfinance.yf.download", side_effect=fake_download):
        _raw_download(["AAPL"])

    assert seen_levels == [logging.CRITICAL]
    assert _YF_LOGGER.level == logging.WARNING


def test_fetch_ohlcv_range_suppresses_yfinance_logger_during_call() -> None:
    _YF_LOGGER.setLevel(logging.WARNING)
    seen_levels: list[int] = []

    def fake_download(**kwargs: object) -> pd.DataFrame:
        seen_levels.append(_YF_LOGGER.level)
        return _make_hist("AAPL", 100.0)

    with patch("app.services._yfinance.yf.download", side_effect=fake_download):
        fetch_ohlcv_range(["AAPL"])

    assert seen_levels == [logging.CRITICAL]
    assert _YF_LOGGER.level == logging.WARNING


def test_fetch_spot_suppresses_yfinance_logger_during_call() -> None:
    _YF_LOGGER.setLevel(logging.WARNING)
    seen_levels: list[int] = []

    class _Ticker:
        def __init__(self, symbol: str) -> None:
            seen_levels.append(_YF_LOGGER.level)
            self.fast_info = {"lastPrice": 100.0, "currency": "USD"}

    with patch("app.services._yfinance.yf.Ticker", side_effect=_Ticker):
        fetch_spot(["AAPL"])

    assert seen_levels == [logging.CRITICAL]
    assert _YF_LOGGER.level == logging.WARNING


# ---------------------------------------------------------------------------
# Provider telemetry (issue #56) — structured logger.info at each fetch site
# ---------------------------------------------------------------------------


def test_raw_download_logs_telemetry_on_success(caplog: pytest.LogCaptureFixture) -> None:
    with (
        patch("app.services._yfinance.yf.download", return_value=_make_hist("AAPL", 100.0)),
        caplog.at_level(logging.INFO, logger="app.services._yfinance"),
    ):
        _raw_download(["AAPL"])

    telemetry = [r for r in caplog.records if "source=yfinance" in r.getMessage()]
    assert len(telemetry) == 1
    msg = telemetry[0].getMessage()
    assert "ticker_count=1" in msg
    assert "latency_ms=" in msg


def test_raw_download_logs_telemetry_with_error_type_on_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fake_download(**kwargs: object) -> pd.DataFrame:
        raise OSError("network unreachable")

    with (
        patch("app.services._yfinance.yf.download", side_effect=fake_download),
        caplog.at_level(logging.INFO, logger="app.services._yfinance"),
    ):
        _raw_download(["AAPL"])

    telemetry = [r for r in caplog.records if "source=yfinance" in r.getMessage()]
    assert len(telemetry) == 1
    assert "error_type=connection" in telemetry[0].getMessage()


def test_fetch_ohlcv_range_logs_telemetry(caplog: pytest.LogCaptureFixture) -> None:
    with (
        patch("app.services._yfinance.yf.download", return_value=_make_hist("AAPL", 100.0)),
        caplog.at_level(logging.INFO, logger="app.services._yfinance"),
    ):
        fetch_ohlcv_range(["AAPL"])

    telemetry = [r for r in caplog.records if "source=yfinance" in r.getMessage()]
    assert len(telemetry) == 1
    assert "ticker_count=1" in telemetry[0].getMessage()


def test_fetch_spot_logs_telemetry(caplog: pytest.LogCaptureFixture) -> None:
    class _Ticker:
        def __init__(self, symbol: str) -> None:
            self.fast_info = {"lastPrice": 100.0, "currency": "USD"}

    with (
        patch("app.services._yfinance.yf.Ticker", side_effect=_Ticker),
        caplog.at_level(logging.INFO, logger="app.services._yfinance"),
    ):
        fetch_spot(["AAPL"])

    telemetry = [r for r in caplog.records if "source=yfinance" in r.getMessage()]
    assert len(telemetry) == 1
    assert "ticker_count=1" in telemetry[0].getMessage()
