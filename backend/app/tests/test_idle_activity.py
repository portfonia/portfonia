"""Server-side idle-timeout enforcement (issue #235)."""

from __future__ import annotations

import uuid

import pytest

from app.core import idle_activity
from app.core.idle_activity import (
    IDLE_TIMEOUT_SECONDS,
    ActivityStoreUnavailable,
    InMemoryBackend,
    is_idle,
    touch_activity,
)

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000f1")


@pytest.fixture
def backend() -> InMemoryBackend:
    mem = InMemoryBackend()
    idle_activity.set_backend(mem)
    return mem


def test_never_touched_user_is_not_idle(backend: InMemoryBackend) -> None:
    """No recorded activity (fresh login) must read as active, not idle —
    absence of a key cannot be distinguished from "just logged in" vs
    "idle so long the record fell out of Redis"; is_idle only ever
    compares two real timestamps."""
    assert is_idle(_USER, now=1_000.0) is False


def test_touch_then_check_within_window_is_not_idle(backend: InMemoryBackend) -> None:
    touch_activity(_USER, now=1_000.0)
    assert is_idle(_USER, now=1_000.0 + IDLE_TIMEOUT_SECONDS - 1) is False


def test_touch_then_check_past_window_is_idle(backend: InMemoryBackend) -> None:
    touch_activity(_USER, now=1_000.0)
    assert is_idle(_USER, now=1_000.0 + IDLE_TIMEOUT_SECONDS + 1) is True


def test_repeated_touch_extends_window(backend: InMemoryBackend) -> None:
    touch_activity(_USER, now=1_000.0)
    touch_activity(_USER, now=1_000.0 + IDLE_TIMEOUT_SECONDS - 1)
    # Would have been idle relative to the first touch, but the second touch
    # reset the clock.
    assert is_idle(_USER, now=1_000.0 + IDLE_TIMEOUT_SECONDS + 1) is False


def test_distinct_users_do_not_share_activity(backend: InMemoryBackend) -> None:
    other = uuid.UUID("00000000-0000-0000-0000-0000000000f2")
    touch_activity(_USER, now=1_000.0)
    assert is_idle(other, now=1_000.0) is False


def test_is_idle_fails_open_when_store_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BrokenBackend:
        def get_timestamp(self, key: str) -> float | None:
            raise ActivityStoreUnavailable("redis down")

        def set_timestamp(self, key: str, value: float, ttl_seconds: int) -> None:
            raise ActivityStoreUnavailable("redis down")

    idle_activity.set_backend(_BrokenBackend())
    assert is_idle(_USER, now=1_000.0) is False


def test_touch_activity_fails_open_when_store_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BrokenBackend:
        def get_timestamp(self, key: str) -> float | None:
            raise ActivityStoreUnavailable("redis down")

        def set_timestamp(self, key: str, value: float, ttl_seconds: int) -> None:
            raise ActivityStoreUnavailable("redis down")

    idle_activity.set_backend(_BrokenBackend())
    # Must not raise.
    touch_activity(_USER, now=1_000.0)
