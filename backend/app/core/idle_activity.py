"""Server-side idle-timeout + absolute session lifetime enforcement
(issue #235, extended by issue #236).

Backs the 15-minute idle window declared in
`frontend/src/lib/idle-timeout.ts` (`SESSION_IDLE_TIMEOUT_MS`) with Redis
state, so a request is rejected on idleness alone even when the underlying
Supabase JWT is still technically valid. The frontend timer
(`use-idle-logout.ts`) only ever ran in a browser tab's memory and vanished
the instant the tab/process closed — this module is the actual enforcement;
the frontend timer remains a convenience/UX layer on top of it, not a
substitute. Keep `IDLE_TIMEOUT_SECONDS` in sync with the frontend constant
by hand — no shared config crosses the Python/TypeScript boundary here.

Issue #236 adds a second, independent control in this same file: an
8-hour absolute session lifetime cap, needed because Supabase's own native
"Time-box user sessions" setting is Pro-tier-only and inert on this
project's Free plan (`sessions_timebox=0` regardless of the Dashboard
value). Unlike the idle window above, this clock never resets on
activity — see `session_lifetime_expired`.
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
    def set_timestamp_if_absent(self, key: str, value: float, ttl_seconds: int) -> None: ...


class InMemoryBackend:
    """Swappable backend for tests — no live Redis required."""

    def __init__(self) -> None:
        self._data: dict[str, float] = {}

    def get_timestamp(self, key: str) -> float | None:
        return self._data.get(key)

    def set_timestamp(self, key: str, value: float, ttl_seconds: int) -> None:
        self._data[key] = value

    def set_timestamp_if_absent(self, key: str, value: float, ttl_seconds: int) -> None:
        self._data.setdefault(key, value)


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
        try:
            return float(raw)  # type: ignore[arg-type]
        except (ValueError, TypeError) as exc:
            # A key holding something that isn't a parseable timestamp
            # (corruption, a future format change, manual tampering) is a
            # store problem the same way a connection failure is — it must
            # fail open the same way, not surface as an unhandled 500 on
            # every authenticated route (PR #240 review round 4).
            raise ActivityStoreUnavailable from exc

    def set_timestamp(self, key: str, value: float, ttl_seconds: int) -> None:
        try:
            self._client.set(key, repr(value), ex=ttl_seconds)
        except RedisError as exc:
            raise ActivityStoreUnavailable from exc

    def set_timestamp_if_absent(self, key: str, value: float, ttl_seconds: int) -> None:
        """NX write — PR #432 review (blacktomb42, non-blocking): a plain
        get-then-set has a race between two concurrent first requests for a
        brand-new session_id, where the later SET would silently move the
        recorded lifetime-start forward. Only session_lifetime_expired's
        write-once path needs this; touch_activity's rolling reset still
        uses the unconditional set_timestamp above.
        """
        try:
            self._client.set(key, repr(value), ex=ttl_seconds, nx=True)
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


# issue #236: 8 hours, product-owner decision 2026-09-11 — a permanent
# app-level mechanism (not a stopgap pending a Supabase Pro upgrade, whose
# native Time-box setting would otherwise cover this).
ABSOLUTE_SESSION_LIFETIME_SECONDS = 8 * 60 * 60

# Safety-net TTL for the lifetime-start marker below, deliberately far
# longer than the 8h window it enforces — same "TTL is a GC safety net,
# never the enforcement mechanism" rationale as _GC_TTL_SECONDS above, but
# re-derived: this marker is write-once (see session_lifetime_expired), so
# losing it to GC before the 8h cap fires would silently restart a capped
# session's clock, which _GC_TTL_SECONDS's own 24h margin over a 15-minute
# window does not by itself protect against here.
_LIFETIME_GC_TTL_SECONDS = ABSOLUTE_SESSION_LIFETIME_SECONDS + 24 * 60 * 60


def _lifetime_key(user_id: UUID, session_id: str) -> str:
    return f"session:lifetime_start:{user_id}:{session_id}"


def session_lifetime_expired(user_id: UUID, session_id: str, *, now: float | None = None) -> bool:
    """True only once more than ABSOLUTE_SESSION_LIFETIME_SECONDS have
    elapsed since this exact (user_id, session_id)'s first call here.

    Write-once, unlike touch_activity's rolling reset: the first call for a
    session records `now` as that session's start and returns False; every
    later call for the same session_id only ever reads that same stored
    value, never overwrites it. A fresh login always gets its own new
    session_id (Supabase JWT claim), so it always starts a fresh window —
    same per-session isolation as is_idle, for the same reason (PR #240
    review round 3): a user_id-only key would let a re-login's write
    silently reset an unrelated session's clock.

    Fails open on Redis outage, matching is_idle's stance — defense-in-
    depth on top of JWT verification, not the primary auth boundary.

    The first-touch write uses set_timestamp_if_absent (Redis SET NX), not
    a plain set_timestamp: two concurrent first requests for a brand-new
    session_id both reading `started is None` would otherwise race on a
    plain SET, with whichever write lands second silently moving the
    recorded start forward (PR #432 review, non-blocking but cheap to
    close). NX means only the first writer's timestamp ever sticks,
    regardless of request ordering after that point — this call doesn't
    need to know which request won, since either way `now` is close enough
    to the true session start that returning False here is still correct.
    """
    moment = time.time() if now is None else now
    key = _lifetime_key(user_id, session_id)
    try:
        started = get_backend().get_timestamp(key)
        if started is None:
            get_backend().set_timestamp_if_absent(key, moment, _LIFETIME_GC_TTL_SECONDS)
            return False
    except ActivityStoreUnavailable:
        logger.exception("idle_activity: lifetime store unavailable, failing open")
        return False
    return (moment - started) > ABSOLUTE_SESSION_LIFETIME_SECONDS
