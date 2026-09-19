"""Unit tests for the Twelve Data historical FX fetch (_twelvedata.py, #406).

No database, no live network -- httpx.Client mocked the same way as
app/tests/test_massive.py / test_finnhub.py.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.services._twelvedata import fetch_daily_history, fetch_live_rate

_API_KEY = "test-twelvedata-key"


def _response(status: str = "ok", values: list[dict[str, object]] | None = None) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"status": status, "values": values if values is not None else []}
    return resp


def _patched_client(resp: MagicMock) -> MagicMock:
    client = MagicMock()
    client.get.return_value = resp
    cm = MagicMock()
    cm.__enter__.return_value = client
    cm.__exit__.return_value = False
    return cm


def test_parses_and_sorts_ascending_by_date() -> None:
    resp = _response(
        values=[
            {"datetime": "2021-09-10", "close": "6.46"},
            {"datetime": "2021-09-09", "close": "6.45"},
        ]
    )
    with patch("app.services._twelvedata.httpx.Client", return_value=_patched_client(resp)):
        points = fetch_daily_history("USD/CNH", date(2021, 9, 9), date(2021, 9, 10), _API_KEY)

    assert points == [(date(2021, 9, 9), Decimal("6.45")), (date(2021, 9, 10), Decimal("6.46"))]


@pytest.mark.parametrize("bad_close", ["NaN", "Infinity", "-Infinity", "0", "-1.5"])
def test_rejects_non_finite_and_non_positive_closes(bad_close: str) -> None:
    resp = _response(values=[{"datetime": "2021-09-09", "close": bad_close}])
    with (
        patch("app.services._twelvedata.httpx.Client", return_value=_patched_client(resp)),
        pytest.raises(ValueError, match="non-finite/non-positive"),
    ):
        fetch_daily_history("USD/CNH", date(2021, 9, 9), date(2021, 9, 9), _API_KEY)


def test_rejects_error_status_response() -> None:
    resp = _response(status="error")
    with (
        patch("app.services._twelvedata.httpx.Client", return_value=_patched_client(resp)),
        pytest.raises(ValueError, match="error response"),
    ):
        fetch_daily_history("USD/CNH", date(2021, 9, 9), date(2021, 9, 9), _API_KEY)


def test_rejects_malformed_point_missing_close() -> None:
    resp = _response(values=[{"datetime": "2021-09-09"}])
    with (
        patch("app.services._twelvedata.httpx.Client", return_value=_patched_client(resp)),
        pytest.raises(ValueError, match="malformed point"),
    ):
        fetch_daily_history("USD/CNH", date(2021, 9, 9), date(2021, 9, 9), _API_KEY)


# ---------------------------------------------------------------------------
# fetch_live_rate (issue #519) — /price, not /time_series. Twelve Data's
# daily-bar endpoint rejects a same-day-range query outright (confirmed live,
# `start_date == end_date` -> 400 "No data is available"), independent of
# publish timing; /price is the always-live quote route.
# ---------------------------------------------------------------------------


def _price_response(price: str) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"price": price}
    return resp


def test_fetch_live_rate_returns_decimal_price() -> None:
    with patch(
        "app.services._twelvedata.httpx.Client",
        return_value=_patched_client(_price_response("1.40341")),
    ):
        rate = fetch_live_rate("USD/AUD", _API_KEY)

    assert rate == Decimal("1.40341")


def test_fetch_live_rate_rejects_non_finite_or_non_positive() -> None:
    with (
        patch(
            "app.services._twelvedata.httpx.Client",
            return_value=_patched_client(_price_response("0")),
        ),
        pytest.raises(ValueError, match="non-finite/non-positive"),
    ):
        fetch_live_rate("USD/AUD", _API_KEY)


def test_fetch_live_rate_rejects_missing_price_field() -> None:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"code": 400, "message": "no data"}
    with (
        patch("app.services._twelvedata.httpx.Client", return_value=_patched_client(resp)),
        pytest.raises(ValueError, match="missing 'price'"),
    ):
        fetch_live_rate("USD/AUD", _API_KEY)
