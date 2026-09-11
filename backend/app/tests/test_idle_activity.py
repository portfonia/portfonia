"""Server-side idle-timeout enforcement (issue #235)."""

from __future__ import annotations

import uuid

import pytest

from app.core import idle_activity
from app.core.idle_activity import (
    ABSOLUTE_SESSION_LIFETIME_SECONDS,
    IDLE_TIMEOUT_SECONDS,
    ActivityStoreUnavailable,
    InMemoryBackend,
    RedisBackend,
    is_idle,
    session_lifetime_expired,
    touch_activity,
)

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
_SESSION = "session-a"


@pytest.fixture
def backend() -> InMemoryBackend:
    mem = InMemoryBackend()
    idle_activity.set_backend(mem)
    return mem


def test_never_touched_session_is_not_idle(backend: InMemoryBackend) -> None:
    """No recorded activity (a session's first-ever request — including a
    fresh login) must read as active, not idle — absence of a key cannot
    be distinguished from "just logged in" vs "idle so long the record
    fell out of Redis"; is_idle only ever compares two real timestamps."""
    assert is_idle(_USER, _SESSION, now=1_000.0) is False


def test_touch_then_check_within_window_is_not_idle(backend: InMemoryBackend) -> None:
    touch_activity(_USER, _SESSION, now=1_000.0)
    assert is_idle(_USER, _SESSION, now=1_000.0 + IDLE_TIMEOUT_SECONDS - 1) is False


def test_touch_then_check_past_window_is_idle(backend: InMemoryBackend) -> None:
    touch_activity(_USER, _SESSION, now=1_000.0)
    assert is_idle(_USER, _SESSION, now=1_000.0 + IDLE_TIMEOUT_SECONDS + 1) is True


def test_repeated_touch_extends_window(backend: InMemoryBackend) -> None:
    touch_activity(_USER, _SESSION, now=1_000.0)
    touch_activity(_USER, _SESSION, now=1_000.0 + IDLE_TIMEOUT_SECONDS - 1)
    # Would have been idle relative to the first touch, but the second touch
    # reset the clock.
    assert is_idle(_USER, _SESSION, now=1_000.0 + IDLE_TIMEOUT_SECONDS + 1) is False


def test_different_session_for_same_user_has_independent_state(
    backend: InMemoryBackend,
) -> None:
    """PR #240 review round 3 (blacktomb42) ship-blocker: round 2 keyed
    Redis by user_id alone (session_id lived in the *value*), so a
    re-login's touch_activity overwrote the single record — including
    whatever the *old* session's key held. Replaying the old, superseded
    JWT afterward then found a fresh-looking timestamp under that same
    key and was waved through too, resurrecting a session that should
    have stayed dead. Keying by (user_id, session_id) means a new
    session_id starts with its own clean slate regardless of how stale
    another session for the same user is — AND that other, old session's
    own record is completely untouched by the new one."""
    touch_activity(_USER, "session-old", now=1_000.0)
    later = 1_000.0 + 100_000.0  # well past the idle window either way

    # A different session for the same user has never been touched — not
    # idle, exactly like a fresh login.
    assert is_idle(_USER, "session-new", now=later) is False

    # The old session's own record is untouched by the new one existing —
    # still idle on its own terms.
    assert is_idle(_USER, "session-old", now=later) is True


def test_distinct_users_do_not_share_activity(backend: InMemoryBackend) -> None:
    other = uuid.UUID("00000000-0000-0000-0000-0000000000f2")
    touch_activity(_USER, _SESSION, now=1_000.0)
    assert is_idle(other, _SESSION, now=1_000.0) is False


def test_is_idle_fails_open_when_store_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BrokenBackend:
        def get_timestamp(self, key: str) -> float | None:
            raise ActivityStoreUnavailable("redis down")

        def set_timestamp(self, key: str, value: float, ttl_seconds: int) -> None:
            raise ActivityStoreUnavailable("redis down")

    idle_activity.set_backend(_BrokenBackend())
    assert is_idle(_USER, _SESSION, now=1_000.0) is False


def test_redis_backend_unparseable_value_raises_store_unavailable() -> None:
    """PR #240 review round 4 (blacktomb42) non-blocking finding: a Redis
    key holding something that isn't a parseable float (corruption, a
    future format change, manual tampering) previously let `float(raw)`
    raise `ValueError` straight through `get_timestamp` — uncaught by the
    `except RedisError` clause, surfacing as an unhandled 500 on every
    authenticated route instead of failing open the same way a genuine
    connection failure does."""

    class _FakeClient:
        def get(self, key: str) -> str:
            return "not-a-float"

    backend = RedisBackend(_FakeClient())  # type: ignore[arg-type]
    with pytest.raises(ActivityStoreUnavailable):
        backend.get_timestamp("some-key")


def test_touch_activity_fails_open_when_store_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BrokenBackend:
        def get_timestamp(self, key: str) -> float | None:
            raise ActivityStoreUnavailable("redis down")

        def set_timestamp(self, key: str, value: float, ttl_seconds: int) -> None:
            raise ActivityStoreUnavailable("redis down")

    idle_activity.set_backend(_BrokenBackend())
    # Must not raise.
    touch_activity(_USER, _SESSION, now=1_000.0)


# issue #236: absolute session lifetime cap.


def test_first_call_for_session_is_not_expired_and_records_start(
    backend: InMemoryBackend,
) -> None:
    """A session's first-ever call (including a fresh login) must read as
    not-expired — same absence-is-safe logic as is_idle — and must record
    that moment as the session's start so later calls have something to
    compare against."""
    assert session_lifetime_expired(_USER, _SESSION, now=1_000.0) is False
    assert backend.get_timestamp(idle_activity._lifetime_key(_USER, _SESSION)) == 1_000.0


def test_within_lifetime_window_is_not_expired(backend: InMemoryBackend) -> None:
    session_lifetime_expired(_USER, _SESSION, now=1_000.0)
    later = 1_000.0 + ABSOLUTE_SESSION_LIFETIME_SECONDS - 1
    assert session_lifetime_expired(_USER, _SESSION, now=later) is False


def test_past_lifetime_window_is_expired(backend: InMemoryBackend) -> None:
    session_lifetime_expired(_USER, _SESSION, now=1_000.0)
    later = 1_000.0 + ABSOLUTE_SESSION_LIFETIME_SECONDS + 1
    assert session_lifetime_expired(_USER, _SESSION, now=later) is True


def test_activity_within_window_does_not_push_out_lifetime_expiry(
    backend: InMemoryBackend,
) -> None:
    """Unlike touch_activity's rolling reset, repeated calls must never
    move the recorded start forward — the cap is absolute, not idle-style
    renewable."""
    session_lifetime_expired(_USER, _SESSION, now=1_000.0)
    mid = 1_000.0 + ABSOLUTE_SESSION_LIFETIME_SECONDS - 1
    assert session_lifetime_expired(_USER, _SESSION, now=mid) is False
    # If this second call had reset the clock, `late` below would read as
    # not-expired. It must not.
    late = 1_000.0 + ABSOLUTE_SESSION_LIFETIME_SECONDS + 1
    assert session_lifetime_expired(_USER, _SESSION, now=late) is True


def test_different_session_for_same_user_has_independent_lifetime(
    backend: InMemoryBackend,
) -> None:
    session_lifetime_expired(_USER, "session-old", now=1_000.0)
    later = 1_000.0 + ABSOLUTE_SESSION_LIFETIME_SECONDS + 1
    # The old session is expired on its own terms...
    assert session_lifetime_expired(_USER, "session-old", now=later) is True
    # ...but a different session_id for the same user, first seen only now,
    # starts its own fresh window.
    assert session_lifetime_expired(_USER, "session-new", now=later) is False


def test_session_lifetime_expired_fails_open_when_store_unavailable() -> None:
    class _BrokenBackend:
        def get_timestamp(self, key: str) -> float | None:
            raise ActivityStoreUnavailable("redis down")

        def set_timestamp(self, key: str, value: float, ttl_seconds: int) -> None:
            raise ActivityStoreUnavailable("redis down")

    idle_activity.set_backend(_BrokenBackend())
    assert session_lifetime_expired(_USER, _SESSION, now=1_000.0) is False
