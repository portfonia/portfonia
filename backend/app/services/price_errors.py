"""Structured price-fetch failure taxonomy (issue #56).

A price fetch (yfinance, Finnhub, Massive) can fail for reasons that want
*different* handling: a 429 wants a backoff, a dropped connection wants a
retry, "this ticker genuinely has no data" does not improve on retry. Before
this module every price fetch site flattened all three into a bare
``except Exception: logger.exception(...)``, so a caller could not tell
which case it was in.

Modeled on ``app/services/llm_errors.py``'s ``LLMErrorCode``/``ErrorPolicy``
split (issue #55): this module classifies *what kind* of failure occurred.
It deliberately does not own retry *policy* (how many attempts, how long to
wait) — ``price_fetcher.py``'s existing total-failure/partial-failure retry
loops keep their own counts and timing. Do not centralize those loops here.

``NO_DATA`` is not raised as an exception — a request can succeed (HTTP 200)
and still carry no usable price for a ticker. Callers assign
``PriceFetchErrorCode.NO_DATA`` directly when they observe that case; only
actual failures go through ``classify_exception``/``classify_http_status``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import httpx


class PriceFetchErrorCode(StrEnum):
    """What kind of failure this was, independent of which fetcher raised it."""

    RATE_LIMIT = "rate_limit"
    CONNECTION = "connection"
    NO_DATA = "no_data"


@dataclass(frozen=True)
class PriceFetchErrorPolicy:
    """What the error itself says about whether retrying could help.

    ``retryable``: could an identical fetch later plausibly succeed?
    Transient rate limits and connection faults — yes. A ticker with no
    data — no, an identical request reproduces the same empty result.
    """

    retryable: bool


_POLICIES: dict[PriceFetchErrorCode, PriceFetchErrorPolicy] = {
    PriceFetchErrorCode.RATE_LIMIT: PriceFetchErrorPolicy(retryable=True),
    PriceFetchErrorCode.CONNECTION: PriceFetchErrorPolicy(retryable=True),
    PriceFetchErrorCode.NO_DATA: PriceFetchErrorPolicy(retryable=False),
}


def policy_for(code: PriceFetchErrorCode) -> PriceFetchErrorPolicy:
    return _POLICIES[code]


def classify_http_status(status_code: int) -> PriceFetchErrorCode:
    """Map an HTTP status code from a price provider to a failure code."""
    if status_code == 429:
        return PriceFetchErrorCode.RATE_LIMIT
    return PriceFetchErrorCode.CONNECTION


def classify_exception(exc: BaseException) -> PriceFetchErrorCode:
    """Classify a raised exception into a :class:`PriceFetchErrorCode`.

    ``httpx.HTTPStatusError`` carries a real status code (Finnhub/Massive)
    and delegates to :func:`classify_http_status`. Every other exception —
    transport failures, yfinance's untyped network errors, and anything not
    specifically recognized — is CONNECTION: this taxonomy has no UNKNOWN
    bucket, and a failure to get a usable response over the wire is the
    closest honest description available for an unrecognized case.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return classify_http_status(exc.response.status_code)
    return PriceFetchErrorCode.CONNECTION
