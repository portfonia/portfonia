"""Unit tests for the Massive.com close-node OHLCV fallback (_massive.py, #56).

No database, no live network — httpx.Client mocked the same way as
app/tests/test_fund_nav_fetcher.py / test_finnhub.py.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.services._massive import fetch_prev_close_ohlcv

_API_KEY = "test-massive-key"


@pytest.fixture(autouse=True)
def _reenable_module_logger_for_caplog() -> None:
    """A db_session-using test elsewhere in the suite runs `alembic upgrade`,
    whose fileConfig() defaults disable_existing_loggers=True and silently
    disables this already-imported module's logger regardless of test file
    or run order — re-enable so caplog can see telemetry records (same
    mechanism as test_fund_nav_fetcher.py)."""
    logging.getLogger("app.services._massive").disabled = False


def _bar(
    o: float = 115.55, h: float = 117.59, low: float = 114.13, c: float = 115.97, v: float = 1000.0
) -> dict[str, object]:
    ts_ms = int(datetime(2026, 9, 2, tzinfo=UTC).timestamp() * 1000)
    return {"o": o, "h": h, "l": low, "c": c, "v": v, "t": ts_ms}


def _patched_client(body_by_ticker: dict[str, dict[str, object]]) -> MagicMock:
    def _get(url: str, **kwargs: object) -> MagicMock:
        ticker = next(t for t in body_by_ticker if f"/ticker/{t}/prev" in url)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = body_by_ticker[ticker]
        resp.raise_for_status = MagicMock()
        return resp

    client = MagicMock()
    client.get.side_effect = _get
    cm = MagicMock()
    cm.__enter__.return_value = client
    cm.__exit__.return_value = False
    return cm


def test_fetch_prev_close_ohlcv_happy_path() -> None:
    with patch(
        "app.services._massive.httpx.Client",
        return_value=_patched_client({"AAPL": {"results": [_bar()], "status": "OK"}}),
    ):
        result = fetch_prev_close_ohlcv(["AAPL"], _API_KEY)

    assert set(result.keys()) == {"AAPL"}
    trade_date, o, h, low, c, vol = result["AAPL"]
    assert trade_date == date(2026, 9, 2)
    assert o == pytest.approx(115.55)
    assert h == pytest.approx(117.59)
    assert low == pytest.approx(114.13)
    assert c == pytest.approx(115.97)
    assert vol == pytest.approx(1000.0)


def test_fetch_prev_close_ohlcv_current_day_withheld_403_fails_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Free tier returns 403 NOT_AUTHORIZED for the current trading day —
    must skip that ticker, never raise."""

    def _get(url: str, **kwargs: object) -> MagicMock:
        request = httpx.Request("GET", url)
        response = httpx.Response(
            403,
            json={"status": "NOT_AUTHORIZED", "message": "plan doesn't include"},
            request=request,
        )
        resp = MagicMock()
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "forbidden", request=request, response=response
        )
        return resp

    client = MagicMock()
    client.get.side_effect = _get
    cm = MagicMock()
    cm.__enter__.return_value = client
    cm.__exit__.return_value = False

    with (
        patch("app.services._massive.httpx.Client", return_value=cm),
        caplog.at_level(logging.INFO, logger="app.services._massive"),
    ):
        result = fetch_prev_close_ohlcv(["AAPL"], _API_KEY)

    assert result == {}
    telemetry = [r for r in caplog.records if "source=massive" in r.getMessage()]
    assert len(telemetry) == 1
    assert "error_type=" in telemetry[0].getMessage()


def test_fetch_prev_close_ohlcv_empty_results_fails_open() -> None:
    with patch(
        "app.services._massive.httpx.Client",
        return_value=_patched_client({"BOGUS": {"results": [], "status": "OK"}}),
    ):
        result = fetch_prev_close_ohlcv(["BOGUS"], _API_KEY)

    assert result == {}


def test_fetch_prev_close_ohlcv_malformed_bar_fails_open() -> None:
    with patch(
        "app.services._massive.httpx.Client",
        return_value=_patched_client(
            {"AAPL": {"results": [{"o": "not-a-number"}], "status": "OK"}}
        ),
    ):
        result = fetch_prev_close_ohlcv(["AAPL"], _API_KEY)

    assert result == {}


def test_fetch_prev_close_ohlcv_network_error_fails_open_for_that_ticker_only() -> None:
    def _get(url: str, **kwargs: object) -> MagicMock:
        if "/ticker/BAD/prev" in url:
            raise httpx.ConnectError("connection refused")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"results": [_bar()], "status": "OK"}
        resp.raise_for_status = MagicMock()
        return resp

    client = MagicMock()
    client.get.side_effect = _get
    cm = MagicMock()
    cm.__enter__.return_value = client
    cm.__exit__.return_value = False

    with patch("app.services._massive.httpx.Client", return_value=cm):
        result = fetch_prev_close_ohlcv(["BAD", "GOOD"], _API_KEY)

    assert set(result.keys()) == {"GOOD"}


def test_fetch_prev_close_ohlcv_uses_new_massive_domain_not_legacy_polygon() -> None:
    seen_urls: list[str] = []

    def _get(url: str, **kwargs: object) -> MagicMock:
        seen_urls.append(url)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"results": [_bar()], "status": "OK"}
        resp.raise_for_status = MagicMock()
        return resp

    client = MagicMock()
    client.get.side_effect = _get
    cm = MagicMock()
    cm.__enter__.return_value = client
    cm.__exit__.return_value = False

    with patch("app.services._massive.httpx.Client", return_value=cm):
        fetch_prev_close_ohlcv(["AAPL"], _API_KEY)

    assert len(seen_urls) == 1
    assert seen_urls[0].startswith("https://api.massive.com/")
    assert "api.polygon.io" not in seen_urls[0]


def test_fetch_prev_close_ohlcv_logs_telemetry_on_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        patch(
            "app.services._massive.httpx.Client",
            return_value=_patched_client({"AAPL": {"results": [_bar()], "status": "OK"}}),
        ),
        caplog.at_level(logging.INFO, logger="app.services._massive"),
    ):
        fetch_prev_close_ohlcv(["AAPL"], _API_KEY)

    telemetry = [r for r in caplog.records if "source=massive" in r.getMessage()]
    assert len(telemetry) == 1
    assert "ticker_count=1" in telemetry[0].getMessage()
    assert "latency_ms=" in telemetry[0].getMessage()


def test_fetch_prev_close_ohlcv_empty_input_makes_no_request() -> None:
    client = MagicMock()
    with patch("app.services._massive.httpx.Client", return_value=client):
        result = fetch_prev_close_ohlcv([], _API_KEY)
    assert result == {}
    client.assert_not_called()
