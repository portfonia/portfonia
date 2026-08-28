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


def test_new_session_id_overrides_stale_record_is_not_idle(backend: InMemoryBackend) -> None:
    """PR #240 review round 1 (blacktomb42): the activity record is keyed
    by user_id, not by session, and this backend has no login endpoint to
    reset it at — login is client-direct to Supabase. Without this
    override, a real re-login after an idle 401 would present a new
    session_id for the same user_id and still read the old stale
    timestamp, staying 401 until the 24h GC TTL. A *different* session_id
    proves this is a genuine new login, not just a refreshed token for the
    same one — that's real evidence the record predates this session."""
    touch_activity(_USER, session_id="session-old", now=1_000.0)
    assert is_idle(_USER, session_id="session-new", now=1_000.0 + IDLE_TIMEOUT_SECONDS + 5) is False


def test_same_session_id_past_window_is_still_idle(backend: InMemoryBackend) -> None:
    """PR #240 review round 2 (blacktomb42): round 1 compared the token's
    `iat` instead of session_id, which broke on a silent background token
    refresh — Supabase's client SDK auto-refreshes on a timer as long as a
    tab stays open, independent of any user interaction, minting a new
    `iat` without a new session_id. The exact "left the tab open overnight"
    case issue #235 was filed for: same session_id (no real new login
    happened), well past the idle window, must still read as idle no
    matter how recently a refresh minted a new access token."""
    touch_activity(_USER, session_id="session-a", now=1_000.0)
    assert is_idle(_USER, session_id="session-a", now=1_000.0 + IDLE_TIMEOUT_SECONDS + 1) is True


def test_distinct_users_do_not_share_activity(backend: InMemoryBackend) -> None:
    other = uuid.UUID("00000000-0000-0000-0000-0000000000f2")
    touch_activity(_USER, now=1_000.0)
    assert is_idle(other, now=1_000.0) is False


def test_is_idle_fails_open_when_store_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BrokenBackend:
        def get_record(self, key: str) -> tuple[float, str | None] | None:
            raise ActivityStoreUnavailable("redis down")

        def set_record(
            self, key: str, timestamp: float, session_id: str | None, ttl_seconds: int
        ) -> None:
            raise ActivityStoreUnavailable("redis down")

    idle_activity.set_backend(_BrokenBackend())
    assert is_idle(_USER, now=1_000.0) is False


def test_touch_activity_fails_open_when_store_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BrokenBackend:
        def get_record(self, key: str) -> tuple[float, str | None] | None:
            raise ActivityStoreUnavailable("redis down")

        def set_record(
            self, key: str, timestamp: float, session_id: str | None, ttl_seconds: int
        ) -> None:
            raise ActivityStoreUnavailable("redis down")

    idle_activity.set_backend(_BrokenBackend())
    # Must not raise.
    touch_activity(_USER, now=1_000.0)
