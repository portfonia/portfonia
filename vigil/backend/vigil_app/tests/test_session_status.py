"""Management session-status client (issue #452). Mock HTTP, no live Portfonia."""

from __future__ import annotations

import httpx
import pytest

from vigil_app.services.session_status import (
    SessionExpired,
    SessionStatusUnavailable,
    check_session_status,
)


def _client(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(
        transport=handler,
        base_url="http://backend:8000",
    )


def test_204_is_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/auth/session-status"
        assert request.headers["authorization"] == "Bearer owner-jwt"
        return httpx.Response(204)

    check_session_status("owner-jwt", client=_client(httpx.MockTransport(handler)))


def test_401_is_session_expired() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    with pytest.raises(SessionExpired):
        check_session_status("stale-jwt", client=_client(httpx.MockTransport(handler)))


def test_transport_failure_is_unavailable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(SessionStatusUnavailable):
        check_session_status("owner-jwt", client=_client(httpx.MockTransport(handler)))


def test_unexpected_status_is_unavailable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(SessionStatusUnavailable):
        check_session_status("owner-jwt", client=_client(httpx.MockTransport(handler)))
