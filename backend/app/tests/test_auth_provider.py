"""Auth-provider adapter: admin headers must not put opaque sb_ keys on Bearer."""

from __future__ import annotations

import jwt
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


def test_create_auth_user_wraps_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from app.services import auth_provider as ap

    class _Boom:
        def __enter__(self) -> object:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(self, *args: object, **kwargs: object) -> object:
            raise httpx.ConnectError("down")

        def delete(self, *args: object, **kwargs: object) -> object:
            raise httpx.ConnectError("down")

    monkeypatch.setattr("httpx.Client", lambda **kwargs: _Boom())
    with pytest.raises(ap.AuthProviderError):
        ap.create_auth_user("a@example.com", "password-ok")


def test_verify_access_token_maps_jwks_transport_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib.error import URLError

    from app.services import auth_provider as ap

    class _Client:
        def get_signing_key_from_jwt(self, token: str) -> object:
            raise URLError("jwks unreachable")

    monkeypatch.setattr(ap, "_jwks", lambda: _Client())
    with pytest.raises(ap.InvalidAccessToken):
        ap.verify_access_token("aaa.bbb.ccc")


class _FakeSigningKey:
    key = "fake-key"


class _FakeJwksClient:
    def get_signing_key_from_jwt(self, token: str) -> object:
        return _FakeSigningKey()


def test_missing_session_id_claim_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #240 review round 3 (blacktomb42): session_id is now a
    load-bearing input to idle_activity.py's per-session Redis key, not
    just informational — a token that somehow lacks it must be rejected
    outright here, not let a session-scoped idle check silently degrade.
    A real Supabase token always carries it (a required claim per
    @supabase/auth-js's RequiredClaims), so this is PyJWT's own `require`
    option doing its job on a token that doesn't."""
    from app.services import auth_provider as ap

    def _raise_missing_claim(*args: object, **kwargs: object) -> dict[str, object]:
        raise jwt.MissingRequiredClaimError("session_id")

    monkeypatch.setattr(ap, "_jwks", lambda: _FakeJwksClient())
    monkeypatch.setattr(jwt, "decode", _raise_missing_claim)
    with pytest.raises(ap.InvalidAccessToken):
        ap.verify_access_token("aaa.bbb.ccc")


def test_empty_session_id_claim_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty string satisfies PyJWT's `require` option (the key is
    present) but is not a usable session identifier — this is the
    application-level check catching what `require` structurally cannot."""
    from app.services import auth_provider as ap

    def _decode_with_empty_session_id(*args: object, **kwargs: object) -> dict[str, object]:
        return {"sub": "user-1", "role": "authenticated", "session_id": ""}

    monkeypatch.setattr(ap, "_jwks", lambda: _FakeJwksClient())
    monkeypatch.setattr(jwt, "decode", _decode_with_empty_session_id)
    with pytest.raises(ap.InvalidAccessToken):
        ap.verify_access_token("aaa.bbb.ccc")


def test_legacy_jwt_service_role_still_sent_as_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import config as config_mod

    settings = config_mod.get_settings()
    jwt_key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.sig"
    monkeypatch.setattr(settings, "SUPABASE_SERVICE_ROLE_KEY", SecretStr(jwt_key))
    headers = _admin_headers()
    assert headers["apikey"] == jwt_key
    assert headers["Authorization"] == f"Bearer {jwt_key}"


class _FakeResponse:
    def __init__(self, status_code: int, body: object = None) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        if self._body is None:
            raise ValueError("no body")
        return self._body


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get(self, *args: object, **kwargs: object) -> _FakeResponse:
        return self._response

    def delete(self, *args: object, **kwargs: object) -> _FakeResponse:
        return self._response


def test_delete_auth_user_returns_true_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import auth_provider as ap

    monkeypatch.setattr("httpx.Client", lambda **kwargs: _FakeClient(_FakeResponse(200)))
    assert ap.delete_auth_user("sub-1") is True


def test_delete_auth_user_returns_false_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """404 (already gone) is idempotent success, not an error — issue #225
    requirement A.2: the caller (admin purge endpoint) must be able to
    retry safely after a previous partial failure."""
    from app.services import auth_provider as ap

    monkeypatch.setattr("httpx.Client", lambda **kwargs: _FakeClient(_FakeResponse(404)))
    assert ap.delete_auth_user("sub-1") is False


def test_delete_auth_user_raises_on_other_status(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import auth_provider as ap

    monkeypatch.setattr("httpx.Client", lambda **kwargs: _FakeClient(_FakeResponse(500)))
    with pytest.raises(ap.AuthProviderError):
        ap.delete_auth_user("sub-1")


def test_get_auth_user_returns_info_on_200(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import auth_provider as ap

    monkeypatch.setattr(
        "httpx.Client",
        lambda **kwargs: _FakeClient(_FakeResponse(200, {"id": "sub-1", "email": "a@example.com"})),
    )
    info = ap.get_auth_user("sub-1")
    assert info == ap.AuthUserInfo(id="sub-1", email="a@example.com")


def test_get_auth_user_returns_none_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import auth_provider as ap

    monkeypatch.setattr("httpx.Client", lambda **kwargs: _FakeClient(_FakeResponse(404)))
    assert ap.get_auth_user("sub-1") is None


def test_get_auth_user_raises_on_other_status(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import auth_provider as ap

    monkeypatch.setattr("httpx.Client", lambda **kwargs: _FakeClient(_FakeResponse(500)))
    with pytest.raises(ap.AuthProviderError):
        ap.get_auth_user("sub-1")


def test_get_auth_user_raises_on_missing_email(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import auth_provider as ap

    monkeypatch.setattr(
        "httpx.Client", lambda **kwargs: _FakeClient(_FakeResponse(200, {"id": "sub-1"}))
    )
    with pytest.raises(ap.AuthProviderError):
        ap.get_auth_user("sub-1")


def test_get_auth_user_raises_on_non_dict_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #246 round 1 review: `data.get("email")` assumed a dict body — a
    list response (malformed or an unexpected GoTrue shape) would otherwise
    raise AttributeError instead of the intended AuthProviderError."""
    from app.services import auth_provider as ap

    monkeypatch.setattr(
        "httpx.Client", lambda **kwargs: _FakeClient(_FakeResponse(200, ["not", "a", "dict"]))
    )
    with pytest.raises(ap.AuthProviderError):
        ap.get_auth_user("sub-1")


def test_get_auth_user_wraps_transport_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from app.services import auth_provider as ap

    class _Boom:
        def __enter__(self) -> object:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, *args: object, **kwargs: object) -> object:
            raise httpx.ConnectError("down")

    monkeypatch.setattr("httpx.Client", lambda **kwargs: _Boom())
    with pytest.raises(ap.AuthProviderError):
        ap.get_auth_user("sub-1")
