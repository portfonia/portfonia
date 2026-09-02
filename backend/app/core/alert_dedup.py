"""Durable suppression of duplicate ops alerts (issue #298).

`send_ops_alert`'s Resend Idempotency-Key only collapses retries of the same
task within a 24h window — it cannot stop a 24h-apart weekday beat from
re-alerting on a fund NAV date that stays stuck for days. This store records
"already alerted for (fund_code, date-state)" so a persisting condition emails
once until the state changes. Same swappable-backend shape as
`app/core/idle_activity.py`.
"""

from __future__ import annotations

import logging
from typing import Protocol

from redis import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class AlertDedupUnavailable(Exception):
    """Redis failed. Both already_alerted and mark_alerted fail open on this
    (see their docstrings) — the dedup is anti-spam only, and suppressing an
    alert because the dedup store is down would hide the very data-staleness
    the alert exists to surface (deliberately opposite rate_limit.py's
    fail-closed convention, same reasoning as idle_activity.py's fail-open).
    """


class AlertDedupBackend(Protocol):
    def contains(self, key: str) -> bool: ...
    def add(self, key: str, ttl_seconds: int) -> None: ...


class InMemoryBackend:
    """Test substitute — no live Redis required."""

    def __init__(self) -> None:
        self._data: set[str] = set()

    def contains(self, key: str) -> bool:
        return key in self._data

    def add(self, key: str, ttl_seconds: int) -> None:
        self._data.add(key)


class RedisBackend:
    def __init__(self, client: Redis) -> None:
        self._client = client

    @classmethod
    def from_settings(cls) -> RedisBackend:
        return cls(Redis.from_url(get_settings().redis_url, decode_responses=True))

    def contains(self, key: str) -> bool:
        try:
            return bool(self._client.exists(f"alertdedup:{key}"))
        except RedisError as exc:
            raise AlertDedupUnavailable from exc

    def add(self, key: str, ttl_seconds: int) -> None:
        try:
            self._client.set(f"alertdedup:{key}", "1", ex=ttl_seconds)
        except RedisError as exc:
            raise AlertDedupUnavailable from exc


_override: AlertDedupBackend | None = None
_redis: RedisBackend | None = None


def set_backend(backend: AlertDedupBackend | None) -> None:
    global _override
    _override = backend


def get_backend() -> AlertDedupBackend:
    global _redis
    if _override is not None:
        return _override
    if _redis is None:
        _redis = RedisBackend.from_settings()
    return _redis


def already_alerted(key: str) -> bool:
    """True when this key was already recorded — the caller should skip the alert.

    Fail open on Redis outage: reads as "not alerted" so the alert still
    sends; losing the dedup is better than losing the alert.
    """
    try:
        return get_backend().contains(key)
    except AlertDedupUnavailable:
        logger.warning("alert dedup unavailable (Redis); proceeding without suppression")
        return False


def mark_alerted(key: str, ttl_seconds: int) -> None:
    """Record an alert so the same state does not re-alert.

    The TTL is a garbage-collection safety net only: keys embed the state
    (e.g. the NAV date), so a changed state produces a new key and a fresh
    alert regardless of expiry. Fail open on Redis outage (see
    AlertDedupUnavailable).
    """
    try:
        get_backend().add(key, ttl_seconds)
    except AlertDedupUnavailable:
        logger.warning("alert dedup unavailable (Redis); suppression window not recorded")
