"""Portfonia account-facts client with a <=60s cache. Scan and management share this."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from time import monotonic as _monotonic

import httpx

from vigil_app.core.config import get_settings

_MAX_CACHE_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class AccountFacts:
    auth_subject: str
    eligible: bool
    account_email: str | None
    email_verified_at: datetime | None
    observed_at: datetime


class IdentityServiceError(Exception):
    """Transport or unexpected response — never treat as eligible=false."""


def _parse_rfc3339(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise IdentityServiceError("invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_facts(payload: object, auth_subject: str) -> AccountFacts:
    if not isinstance(payload, dict):
        raise IdentityServiceError("invalid identity payload")
    eligible = payload.get("eligible")
    if not isinstance(eligible, bool):
        raise IdentityServiceError("invalid identity payload")
    email = payload.get("account_email")
    if email is not None and not isinstance(email, str):
        raise IdentityServiceError("invalid identity payload")
    observed = _parse_rfc3339(payload.get("observed_at"))
    if observed is None:
        raise IdentityServiceError("invalid identity payload")
    return AccountFacts(
        auth_subject=auth_subject,
        eligible=eligible,
        account_email=email,
        email_verified_at=_parse_rfc3339(payload.get("email_verified_at")),
        observed_at=observed,
    )


class AccountFactsClient:
    def __init__(
        self,
        *,
        http: httpx.Client,
        identity_token: str,
        cache_ttl_seconds: float = _MAX_CACHE_TTL_SECONDS,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._http = http
        self._token = identity_token
        self._ttl = min(cache_ttl_seconds, _MAX_CACHE_TTL_SECONDS)
        self._monotonic = monotonic or _monotonic
        self._cache: dict[str, tuple[float, AccountFacts]] = {}
        self._lock = Lock()

    def fetch(self, auth_subject: str, *, force_fresh: bool = False) -> AccountFacts:
        now = self._monotonic()
        if not force_fresh:
            with self._lock:
                hit = self._cache.get(auth_subject)
                if hit is not None and (now - hit[0]) < self._ttl:
                    return hit[1]
        try:
            response = self._http.get(
                f"/internal/vigil/principals/{auth_subject}",
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.HTTPError as exc:
            raise IdentityServiceError("identity service unavailable") from exc
        if response.status_code != 200:
            raise IdentityServiceError("identity service unavailable")
        try:
            payload: object = response.json()
        except ValueError as exc:
            raise IdentityServiceError("identity service unavailable") from exc
        facts = _parse_facts(payload, auth_subject)
        with self._lock:
            self._cache[auth_subject] = (self._monotonic(), facts)
        return facts


_default_lock = Lock()
_default_http: httpx.Client | None = None
_default_client: AccountFactsClient | None = None


def reset_account_facts_client() -> None:
    global _default_http, _default_client
    with _default_lock:
        if _default_http is not None:
            _default_http.close()
        _default_http = None
        _default_client = None


def _shared_client() -> AccountFactsClient:
    global _default_http, _default_client
    with _default_lock:
        if _default_client is None:
            settings = get_settings()
            _default_http = httpx.Client(
                base_url=settings.PORTFONIA_INTERNAL_BASE_URL.rstrip("/"),
                timeout=5.0,
            )
            _default_client = AccountFactsClient(
                http=_default_http,
                identity_token=settings.IDENTITY_SERVICE_TOKEN.get_secret_value(),
            )
        return _default_client


def fetch_account_facts(auth_subject: str, *, force_fresh: bool = False) -> AccountFacts:
    return _shared_client().fetch(auth_subject, force_fresh=force_fresh)
