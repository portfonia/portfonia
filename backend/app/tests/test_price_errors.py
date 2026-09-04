"""Unit tests for the price-fetch error taxonomy (issue #56).

No database, no network — pure classification logic.
"""

from __future__ import annotations

import httpx
import pytest

from app.services.price_errors import (
    PriceFetchErrorCode,
    classify_exception,
    classify_http_status,
    policy_for,
)


def test_classify_http_status_429_is_rate_limit() -> None:
    assert classify_http_status(429) == PriceFetchErrorCode.RATE_LIMIT


@pytest.mark.parametrize("status", [400, 403, 404, 500, 502, 503])
def test_classify_http_status_non_429_is_connection(status: int) -> None:
    assert classify_http_status(status) == PriceFetchErrorCode.CONNECTION


def test_classify_exception_http_status_error_delegates_to_status_classification() -> None:
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(429, request=request)
    exc = httpx.HTTPStatusError("rate limited", request=request, response=response)
    assert classify_exception(exc) == PriceFetchErrorCode.RATE_LIMIT


def test_classify_exception_transport_error_is_connection() -> None:
    exc = httpx.ConnectError("connection refused")
    assert classify_exception(exc) == PriceFetchErrorCode.CONNECTION


def test_classify_exception_os_error_is_connection() -> None:
    assert classify_exception(OSError("network unreachable")) == PriceFetchErrorCode.CONNECTION


def test_classify_exception_unknown_falls_back_to_connection() -> None:
    """No UNKNOWN bucket in this taxonomy — an unrecognized failure is still
    a fetch that didn't get usable data over the wire, so it defaults to
    CONNECTION rather than being silently dropped or crashing classify()."""
    assert classify_exception(ValueError("boom")) == PriceFetchErrorCode.CONNECTION


def test_rate_limit_and_connection_are_retryable() -> None:
    assert policy_for(PriceFetchErrorCode.RATE_LIMIT).retryable is True
    assert policy_for(PriceFetchErrorCode.CONNECTION).retryable is True


def test_no_data_is_not_retryable() -> None:
    """A ticker with genuinely no data won't produce data on retry — retrying
    it can't help, unlike a transient rate limit or connection blip."""
    assert policy_for(PriceFetchErrorCode.NO_DATA).retryable is False
