"""P1.1-A02: missing owner/key/database settings fail startup."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vigil_app.core.config import Settings


def _apply(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_settings_load_when_all_required_present(required_env: dict[str, str]) -> None:
    settings = Settings(_env_file=None)
    assert required_env["VIGIL_OWNER_AUTH_SUBJECT"] == settings.OWNER_AUTH_SUBJECT
    assert required_env["VIGIL_DB_NAME"] == settings.DB_NAME
    assert settings.DISPATCH_ENABLED is False
    assert settings.RELEASE_ENABLED is False
    assert settings.ARMING_ENABLED is False
    assert settings.CELERY_QUEUE_PREFIX.startswith("vigil")
    assert settings.redis_url.endswith("/15")


@pytest.mark.parametrize(
    "missing",
    [
        "VIGIL_DB_NAME",
        "VIGIL_DB_PASSWORD",
        "VIGIL_OWNER_AUTH_SUBJECT",
        "VIGIL_KEK",
        "VIGIL_KEK_VERSION",
        "VIGIL_NOTIFICATION_KEY",
        "VIGIL_NONCE_KEY",
        "VIGIL_ALTCHA_HMAC_KEY",
        "VIGIL_OBJECT_BUCKET",
        "VIGIL_REDIS_KEY_PREFIX",
        "VIGIL_CELERY_QUEUE_PREFIX",
        "VIGIL_IDENTITY_SERVICE_TOKEN",
        "VIGIL_OPS_API_TOKEN",
        "VIGIL_RESEND_API_KEY",
        "VIGIL_AUTH_ISSUER",
        "VIGIL_PORTFONIA_INTERNAL_BASE_URL",
    ],
)
def test_missing_required_setting_fails_startup(
    monkeypatch: pytest.MonkeyPatch, required_env: dict[str, str], missing: str
) -> None:
    _apply(monkeypatch, required_env)
    monkeypatch.delenv(missing, raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_blank_owner_fails(monkeypatch: pytest.MonkeyPatch, required_env: dict[str, str]) -> None:
    _apply(monkeypatch, required_env)
    monkeypatch.setenv("VIGIL_OWNER_AUTH_SUBJECT", "  ")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_kek_must_differ_from_notification_key(
    monkeypatch: pytest.MonkeyPatch, required_env: dict[str, str]
) -> None:
    _apply(monkeypatch, required_env)
    monkeypatch.setenv("VIGIL_NOTIFICATION_KEY", required_env["VIGIL_KEK"])
    with pytest.raises(ValidationError, match="NOTIFICATION_KEY"):
        Settings(_env_file=None)


def test_settings_ignore_unprefixed_portfonia_db_name(
    monkeypatch: pytest.MonkeyPatch, required_env: dict[str, str]
) -> None:
    _apply(monkeypatch, required_env)
    monkeypatch.setenv("DB_NAME", "portfonia_dev")
    settings = Settings(_env_file=None)
    assert required_env["VIGIL_DB_NAME"] == settings.DB_NAME
    assert settings.DB_NAME != "portfonia_dev"
