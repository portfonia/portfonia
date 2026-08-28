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
# absence reads identically whether a session was never active or was
# active so long ago the record fell out of Redis, and only the "never
# active" reading is safe to treat as "not idle" — so the timestamp
# comparison, not expiry, has to be the enforcement mechanism.
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


def _activity_key(user_id: UUID, session_id: str) -> str:
    """Keyed by (user_id, session_id), not user_id alone (PR #240 review
    round 3, blacktomb42). A user-only key means a re-login's touch_activity
    overwrites the single record with the new session — including whatever
    the *old* session's key held — so replaying the old, superseded JWT
    afterward finds the new session's fresh timestamp sitting under the
    same key and is waved through as "not idle" until jwt_exp (3600s). Two
    genuinely different sessions must never be able to keep each other
    alive; each session gets its own key, aging out independently on its
    own actual activity."""
    return f"session:active:{user_id}:{session_id}"


def is_idle(user_id: UUID, session_id: str, *, now: float | None = None) -> bool:
    """True only if this exact (user_id, session_id) has a recorded
    activity timestamp older than IDLE_TIMEOUT_SECONDS. No recorded
    timestamp reads as NOT idle: that covers both a session's first-ever
    request (nothing to compare against yet — this is what makes a real
    re-login work immediately, since a new session_id has no key at all)
    and a Redis outage — fail open, since this check sits in
    `current_principal`, the single choke point for every authenticated
    route, and treating an outage as "everyone is idle" would turn a Redis
    blip into an app-wide outage for a control that adds security depth on
    top of JWT verification (which stays fail-closed), not the primary
    auth boundary itself.

    `session_id` is required, not optional: `verify_access_token` now
    rejects any token missing it, since a session-scoped key cannot be
    formed without one (round 3 review — round 2 kept session_id optional
    and stuffed it into a still user-keyed record's *value*, which is what
    let a re-login's write resurrect the old session in the first place).
    There is no cross-session comparison here at all anymore: each session
    is checked purely against its own history.
    """
    moment = time.time() if now is None else now
    try:
        last_active = get_backend().get_timestamp(_activity_key(user_id, session_id))
    except ActivityStoreUnavailable:
        logger.exception("idle_activity: store unavailable, failing open")
        return False
    if last_active is None:
        return False
    return (moment - last_active) > IDLE_TIMEOUT_SECONDS


def touch_activity(user_id: UUID, session_id: str, *, now: float | None = None) -> None:
    """Record activity for this (user_id, session_id), resetting that
    session's own idle window. Fails open: if Redis is down, this
    request's activity simply isn't recorded rather than raising —
    matching is_idle's fail-open stance above.
    """
    moment = time.time() if now is None else now
    try:
        get_backend().set_timestamp(_activity_key(user_id, session_id), moment, _GC_TTL_SECONDS)
    except ActivityStoreUnavailable:
        logger.exception("idle_activity: store unavailable, activity not recorded")
