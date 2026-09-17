"""Vigil Settings fields (issue #451, P1.1).

A04: unset or malformed Vigil config must never fail Settings load or stop
existing app startup / report tasks — validated lazily inside Vigil
feature code, not here.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest

from app.core.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Generator[None, None, None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_vigil_mode_defaults_off() -> None:
    assert get_settings().VIGIL_MODE == "off"


def test_settings_loads_with_vigil_settings_entirely_unset() -> None:
    settings = get_settings()
    assert settings.VIGIL_OWNER_AUTH_SUBJECT is None
    assert settings.VIGIL_ENCRYPTION_KEY is None
    assert settings.VIGIL_ENCRYPTION_KEY_PREV is None
    assert settings.VIGIL_NOTIFICATION_KEY is None
    assert settings.VIGIL_NOTIFICATION_KEY_PREV is None


def test_settings_loads_with_malformed_vigil_encryption_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlike HOLDINGS_ENCRYPTION_KEY, VIGIL_ENCRYPTION_KEY has no eager
    Fernet-format validator at this checkpoint — a garbage value must not
    fail Settings load; format validation is deferred to whichever future
    Vigil feature code first needs the key (none exists yet)."""
    monkeypatch.setenv("VIGIL_ENCRYPTION_KEY", "not-a-valid-fernet-key")
    get_settings.cache_clear()
    settings = get_settings()  # must not raise
    assert settings.VIGIL_ENCRYPTION_KEY is not None
    assert settings.VIGIL_ENCRYPTION_KEY.get_secret_value() == "not-a-valid-fernet-key"


def test_settings_loads_with_malformed_vigil_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """VIGIL_MODE has no eager enum validator either — an unrecognized
    value must not fail Settings load (A04), only whatever feature code
    reads it later."""
    monkeypatch.setenv("VIGIL_MODE", "not-a-real-mode")
    get_settings.cache_clear()
    settings = get_settings()  # must not raise
    assert settings.VIGIL_MODE == "not-a-real-mode"
