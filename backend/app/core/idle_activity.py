"""Server-side idle-timeout enforcement (issue #235).

Backs the 15-minute idle window declared in
`frontend/src/lib/idle-timeout.ts` (`SESSION_IDLE_TIMEOUT_MS`) with Redis
state, so a request is rejected on idleness alone even when the underlying
Supabase JWT is still technically valid. The frontend timer
(`use-idle-logout.ts`) only ever ran in a browser tab's memory and vanished
the instant the tab/process closed — this module is the actual enforcement;
the frontend timer remains a convenience/UX layer on top of it, not a
substitute. Keep `IDLE_TIMEOUT_SECONDS` in sync with the frontend constant
by hand — no shared config crosses the Python/TypeScript boundary here.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol
from uuid import UUID

from redis import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings

logger = logging.getLogger(__name__)

IDLE_TIMEOUT_SECONDS = 15 * 60

# Redis key TTL is a garbage-collection safety net only, deliberately much
# longer than IDLE_TIMEOUT_SECONDS. The 15-minute policy is enforced by
# comparing stored timestamps (see is_idle), never by key expiry: a key's
# absence reads identically whether a user was never active or was active
# so long ago the record fell out of Redis, and only the "never active"
# reading is safe to treat as "not idle" — so the timestamp comparison,
# not expiry, has to be the enforcement mechanism.
_GC_TTL_SECONDS = 24 * 60 * 60


class ActivityStoreUnavailable(Exception):
    """Redis failed. Both is_idle and touch_activity fail open on this —
    see the fail-open note in each function's docstring for why idle-logout
    (a defense-in-depth control layered on top of JWT verification, which
    remains the primary and fail-closed boundary) does not get to take down
    every authenticated route in the app on a Redis outage.
    """


class ActivityBackend(Protocol):
    def get_timestamp(self, key: str) -> float | None: ...
    def set_timestamp(self, key: str, value: float, ttl_seconds: int) -> None: ...


class InMemoryBackend:
    """Swappable backend for tests — no live Redis required."""

    def __init__(self) -> None:
        self._data: dict[str, float] = {}

    def get_timestamp(self, key: str) -> float | None:
        return self._data.get(key)

    def set_timestamp(self, key: str, value: float, ttl_seconds: int) -> None:
        self._data[key] = value


class RedisBackend:
    def __init__(self, client: Redis) -> None:
        self._client = client

    @classmethod
    def from_settings(cls) -> RedisBackend:
        return cls(Redis.from_url(get_settings().redis_url, decode_responses=True))

    def get_timestamp(self, key: str) -> float | None:
        try:
            raw: object = self._client.get(key)
        except RedisError as exc:
            raise ActivityStoreUnavailable from exc
        if raw is None:
            return None
        return float(raw)  # type: ignore[arg-type]

    def set_timestamp(self, key: str, value: float, ttl_seconds: int) -> None:
        try:
            self._client.set(key, repr(value), ex=ttl_seconds)
        except RedisError as exc:
            raise ActivityStoreUnavailable from exc


_override: ActivityBackend | None = None
_redis: RedisBackend | None = None


def set_backend(backend: ActivityBackend | None) -> None:
    global _override
    _override = backend


def get_backend() -> ActivityBackend:
    global _redis
    if _override is not None:
        return _override
    if _redis is None:
        _redis = RedisBackend.from_settings()
    return _redis


def _activity_key(user_id: UUID) -> str:
    return f"session:active:{user_id}"


def is_idle(user_id: UUID, *, issued_at: float | None = None, now: float | None = None) -> bool:
    """True only if `user_id` has a recorded activity timestamp older than
    IDLE_TIMEOUT_SECONDS. No recorded timestamp reads as NOT idle: that
    covers both a fresh login (nothing to compare against yet) and a Redis
    outage — fail open, since this check sits in `current_principal`, the
    single choke point for every authenticated route, and treating an
    outage as "everyone is idle" would turn a Redis blip into an app-wide
    outage for a control that adds security depth on top of JWT
    verification (which stays fail-closed), not the primary auth boundary
    itself.

    `issued_at` is the presenting token's own `iat` claim. The activity
    record is keyed by user_id, not by session — login happens entirely
    client-side against Supabase (this backend has no login endpoint to
    reset the record at), so a real re-login after an idle 401 presents a
    *different* token for the *same* user_id, but would otherwise still
    read the old stale timestamp and stay 401 until the GC TTL expires
    (PR #240 review, blacktomb42). A token minted after the last recorded
    activity — whether from a fresh login or a silent refresh — is proof
    the idle record predates this session and cannot apply to it, so it's
    treated as not idle regardless of how far in the past the record is.
    The replayed *same* token from before the idle window (its `iat`
    unchanged) still reads as idle exactly as before.
    """
    moment = time.time() if now is None else now
    try:
        last_active = get_backend().get_timestamp(_activity_key(user_id))
    except ActivityStoreUnavailable:
        logger.exception("idle_activity: store unavailable, failing open")
        return False
    if last_active is None:
        return False
    if issued_at is not None and issued_at > last_active:
        return False
    return (moment - last_active) > IDLE_TIMEOUT_SECONDS


def touch_activity(user_id: UUID, *, now: float | None = None) -> None:
    """Record activity for `user_id`, resetting the idle window. Fails
    open: if Redis is down, this request's activity simply isn't recorded
    rather than raising — matching is_idle's fail-open stance above.
    """
    moment = time.time() if now is None else now
    try:
        get_backend().set_timestamp(_activity_key(user_id), moment, _GC_TTL_SECONDS)
    except ActivityStoreUnavailable:
        logger.exception("idle_activity: store unavailable, activity not recorded")
