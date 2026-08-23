"""Auth-provider adapter: admin headers must not put opaque sb_ keys on Bearer."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.services.auth_provider import _admin_headers


def test_opaque_secret_key_goes_on_apikey_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """New-project keys are `sb_secret_...`, not JWTs. Putting them on
    Authorization: Bearer is rejected as Invalid JWT (supabase-js #1568)."""
    from app.core import config as config_mod

    settings = config_mod.get_settings()
    monkeypatch.setattr(
        settings, "SUPABASE_SERVICE_ROLE_KEY", SecretStr("sb_secret_testdata_xxxxxxxx")
    )
    headers = _admin_headers()
    assert headers["apikey"] == "sb_secret_testdata_xxxxxxxx"
    assert "Authorization" not in headers


def test_legacy_jwt_service_role_still_sent_as_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import config as config_mod

    settings = config_mod.get_settings()
    jwt_key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.sig"
    monkeypatch.setattr(settings, "SUPABASE_SERVICE_ROLE_KEY", SecretStr(jwt_key))
    headers = _admin_headers()
    assert headers["apikey"] == jwt_key
    assert headers["Authorization"] == f"Bearer {jwt_key}"
