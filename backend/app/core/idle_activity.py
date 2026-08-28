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


# (timestamp, session_id-at-that-timestamp). session_id is Supabase's own
# JWT claim (a required claim on every Supabase-issued access token —
# `RequiredClaims` in @supabase/auth-js) identifying the login session: it
# stays constant across a token refresh and only changes on an actual new
# login. Recorded alongside the timestamp so is_idle can tell "the same
# session, just refreshed in the background" apart from "a genuinely new
# login" — see is_idle's docstring for why that distinction is the whole
# point (PR #240 review round 2).
_ActivityRecord = tuple[float, str | None]


class ActivityBackend(Protocol):
    def get_record(self, key: str) -> _ActivityRecord | None: ...
    def set_record(
        self, key: str, timestamp: float, session_id: str | None, ttl_seconds: int
    ) -> None: ...


class InMemoryBackend:
    """Swappable backend for tests — no live Redis required."""

    def __init__(self) -> None:
        self._data: dict[str, _ActivityRecord] = {}

    def get_record(self, key: str) -> _ActivityRecord | None:
        return self._data.get(key)

    def set_record(
        self, key: str, timestamp: float, session_id: str | None, ttl_seconds: int
    ) -> None:
        self._data[key] = (timestamp, session_id)


class RedisBackend:
    def __init__(self, client: Redis) -> None:
        self._client = client

    @classmethod
    def from_settings(cls) -> RedisBackend:
        return cls(Redis.from_url(get_settings().redis_url, decode_responses=True))

    def get_record(self, key: str) -> _ActivityRecord | None:
        try:
            raw: object = self._client.get(key)
        except RedisError as exc:
            raise ActivityStoreUnavailable from exc
        if not isinstance(raw, str):
            return None
        timestamp_str, _, session_id = raw.partition("|")
        try:
            timestamp = float(timestamp_str)
        except ValueError:
            return None
        return timestamp, (session_id or None)

    def set_record(
        self, key: str, timestamp: float, session_id: str | None, ttl_seconds: int
    ) -> None:
        try:
            self._client.set(key, f"{timestamp}|{session_id or ''}", ex=ttl_seconds)
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


def is_idle(user_id: UUID, *, session_id: str | None = None, now: float | None = None) -> bool:
    """True only if `user_id` has a recorded activity timestamp older than
    IDLE_TIMEOUT_SECONDS *for the same login session*. No recorded record
    reads as NOT idle: that covers both a fresh login (nothing to compare
    against yet) and a Redis outage — fail open, since this check sits in
    `current_principal`, the single choke point for every authenticated
    route, and treating an outage as "everyone is idle" would turn a Redis
    blip into an app-wide outage for a control that adds security depth on
    top of JWT verification (which stays fail-closed), not the primary auth
    boundary itself.

    `session_id` is the presenting token's own `session_id` claim (a
    required claim on every Supabase access token). PR #240 review round 1
    fixed a real re-login staying 401'd by comparing the token's `iat`
    against the stale record instead — but `iat` changes on every token
    refresh too, and Supabase's client SDK auto-refreshes on a background
    timer as long as a tab stays open, independent of any user interaction
    or request to this backend. That made an unattended overnight tab
    (never touched, but still open) look freshly active on the very next
    request, which is exactly the scenario issue #235 was filed for —
    review round 2 caught it. `session_id` does not have this problem: it
    stays constant across a refresh and only changes on an actual new
    login, so a mismatch is real evidence the record predates this
    session, while a match means "same session, possibly refreshed" and
    the ordinary timestamp comparison below still applies in full.
    """
    moment = time.time() if now is None else now
    try:
        record = get_backend().get_record(_activity_key(user_id))
    except ActivityStoreUnavailable:
        logger.exception("idle_activity: store unavailable, failing open")
        return False
    if record is None:
        return False
    last_active, recorded_session_id = record
    if (
        session_id is not None
        and recorded_session_id is not None
        and session_id != recorded_session_id
    ):
        return False
    return (moment - last_active) > IDLE_TIMEOUT_SECONDS


def touch_activity(
    user_id: UUID, *, session_id: str | None = None, now: float | None = None
) -> None:
    """Record activity for `user_id` under `session_id`, resetting the idle
    window. Fails open: if Redis is down, this request's activity simply
    isn't recorded rather than raising — matching is_idle's fail-open
    stance above.
    """
    moment = time.time() if now is None else now
    try:
        get_backend().set_record(_activity_key(user_id), moment, session_id, _GC_TTL_SECONDS)
    except ActivityStoreUnavailable:
        logger.exception("idle_activity: store unavailable, activity not recorded")
