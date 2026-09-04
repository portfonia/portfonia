"""Unit tests for the Finnhub US-market fallback (_finnhub.py, issue #56).

No database, no live network — httpx.Client is mocked the same way as
app/tests/test_fund_nav_fetcher.py.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.services._finnhub import FinnhubQuote, fetch_quotes

_API_KEY = "test-finnhub-key"


@pytest.fixture(autouse=True)
def _reenable_module_logger_for_caplog() -> None:
    """A db_session-using test elsewhere in the suite runs `alembic upgrade`,
    whose fileConfig() defaults disable_existing_loggers=True and silently
    disables this already-imported module's logger regardless of test file
    or run order — re-enable so caplog can see telemetry records (same
    mechanism as test_fund_nav_fetcher.py)."""
    logging.getLogger("app.services._finnhub").disabled = False


def _patched_client(quote_by_ticker: dict[str, dict[str, object]]) -> MagicMock:
    """Mock httpx.Client whose .get dispatches on the `symbol` query param."""

    def _get(url: str, **kwargs: object) -> MagicMock:
        params = kwargs.get("params", {})
        symbol = params["symbol"]  # type: ignore[index]
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = quote_by_ticker[symbol]
        resp.raise_for_status = MagicMock()
        return resp

    client = MagicMock()
    client.get.side_effect = _get
    cm = MagicMock()
    cm.__enter__.return_value = client
    cm.__exit__.return_value = False
    return cm


def test_fetch_quotes_happy_path() -> None:
    with patch(
        "app.services._finnhub.httpx.Client",
        return_value=_patched_client({"AAPL": {"c": 328.21, "pc": 324.96}}),
    ):
        result = fetch_quotes(["AAPL"], _API_KEY)

    assert result == {"AAPL": FinnhubQuote(last=328.21, previous_close=324.96)}


def test_fetch_quotes_error_field_skips_ticker() -> None:
    """Free tier returns {"error": ...} (not a raised HTTP error) for a
    symbol it can't serve — must be treated as no usable data, not a crash."""
    with patch(
        "app.services._finnhub.httpx.Client",
        return_value=_patched_client(
            {"0700.HK": {"error": "You don't have access to this resource."}}
        ),
    ):
        result = fetch_quotes(["0700.HK"], _API_KEY)

    assert result == {}


def test_fetch_quotes_zero_price_skips_ticker() -> None:
    """Finnhub's unknown-symbol convention: all-zero fields, HTTP 200."""
    with patch(
        "app.services._finnhub.httpx.Client",
        return_value=_patched_client({"BOGUS": {"c": 0, "pc": 0}}),
    ):
        result = fetch_quotes(["BOGUS"], _API_KEY)

    assert result == {}


def test_fetch_quotes_one_bad_one_good_does_not_abort_batch() -> None:
    with patch(
        "app.services._finnhub.httpx.Client",
        return_value=_patched_client(
            {"AAPL": {"c": 328.21, "pc": 324.96}, "BOGUS": {"c": 0, "pc": 0}}
        ),
    ):
        result = fetch_quotes(["AAPL", "BOGUS"], _API_KEY)

    assert set(result.keys()) == {"AAPL"}


def test_fetch_quotes_http_error_fails_open(caplog: pytest.LogCaptureFixture) -> None:
    """A raised HTTPStatusError (e.g. 429) must skip only that ticker, never raise."""

    def _get(url: str, **kwargs: object) -> MagicMock:
        request = httpx.Request("GET", url)
        response = httpx.Response(429, request=request)
        resp = MagicMock()
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "rate limited", request=request, response=response
        )
        return resp

    client = MagicMock()
    client.get.side_effect = _get
    cm = MagicMock()
    cm.__enter__.return_value = client
    cm.__exit__.return_value = False

    with (
        patch("app.services._finnhub.httpx.Client", return_value=cm),
        caplog.at_level(logging.INFO, logger="app.services._finnhub"),
    ):
        result = fetch_quotes(["AAPL"], _API_KEY)

    assert result == {}
    telemetry = [r for r in caplog.records if "source=finnhub" in r.getMessage()]
    assert len(telemetry) == 1
    assert "error_type=rate_limit" in telemetry[0].getMessage()


def test_fetch_quotes_network_error_fails_open_for_that_ticker_only() -> None:
    def _get(url: str, **kwargs: object) -> MagicMock:
        params = kwargs.get("params", {})
        if params["symbol"] == "BAD":  # type: ignore[index]
            raise httpx.ConnectError("connection refused")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"c": 100.0, "pc": 99.0}
        resp.raise_for_status = MagicMock()
        return resp

    client = MagicMock()
    client.get.side_effect = _get
    cm = MagicMock()
    cm.__enter__.return_value = client
    cm.__exit__.return_value = False

    with patch("app.services._finnhub.httpx.Client", return_value=cm):
        result = fetch_quotes(["BAD", "GOOD"], _API_KEY)

    assert set(result.keys()) == {"GOOD"}


def test_fetch_quotes_logs_telemetry_on_success(caplog: pytest.LogCaptureFixture) -> None:
    with (
        patch(
            "app.services._finnhub.httpx.Client",
            return_value=_patched_client({"AAPL": {"c": 328.21, "pc": 324.96}}),
        ),
        caplog.at_level(logging.INFO, logger="app.services._finnhub"),
    ):
        fetch_quotes(["AAPL"], _API_KEY)

    telemetry = [r for r in caplog.records if "source=finnhub" in r.getMessage()]
    assert len(telemetry) == 1
    assert "ticker_count=1" in telemetry[0].getMessage()
    assert "latency_ms=" in telemetry[0].getMessage()


def test_fetch_quotes_empty_input_makes_no_request() -> None:
    client = MagicMock()
    with patch("app.services._finnhub.httpx.Client", return_value=client):
        result = fetch_quotes([], _API_KEY)
    assert result == {}
    client.assert_not_called()
