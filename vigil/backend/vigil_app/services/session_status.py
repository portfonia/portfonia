"""Portfonia session-status client. Management path only — never the scan path."""

from __future__ import annotations

import httpx

from vigil_app.core.config import get_settings


class SessionExpired(Exception):
    """Portfonia rejected the bearer (idle or absolute lifetime)."""


class SessionStatusUnavailable(Exception):
    """Transport or unexpected status from /auth/session-status."""


def check_session_status(token: str, *, client: httpx.Client | None = None) -> None:
    if client is not None:
        _get(client, token)
        return
    with httpx.Client(
        base_url=get_settings().PORTFONIA_INTERNAL_BASE_URL.rstrip("/"),
        timeout=5.0,
    ) as http:
        _get(http, token)


def _get(http: httpx.Client, token: str) -> None:
    try:
        response = http.get(
            "/auth/session-status",
            headers={"Authorization": f"Bearer {token}"},
        )
    except httpx.HTTPError as exc:
        raise SessionStatusUnavailable("session status unavailable") from exc
    if response.status_code == 204:
        return
    if response.status_code == 401:
        raise SessionExpired("session expired")
    raise SessionStatusUnavailable("session status unavailable")
