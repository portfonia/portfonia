"""P1.2: independent JWT verifier and owner allowlist (issue #452)."""

from __future__ import annotations

import inspect

import jwt
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from vigil_app.core import auth as auth_mod
from vigil_app.core.auth import InvalidAccessToken, require_owner, verify_access_token
from vigil_app.core.config import get_settings


class _FakeSigningKey:
    key = "fake-key"


class _FakeJwksClient:
    def get_signing_key_from_jwt(self, token: str) -> object:
        return _FakeSigningKey()


def _request(authorization: str | None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode("latin-1")))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/vault",
            "headers": headers,
            "http_version": "1.1",
            "scheme": "http",
            "client": ("127.0.0.1", 123),
            "server": ("test", 80),
        }
    )


def test_verifier_is_es256_rs256_not_imported_from_portfonia() -> None:
    source = inspect.getsource(verify_access_token)
    assert 'algorithms=["ES256", "RS256"]' in source
    assert "app.services.auth_provider" not in inspect.getsource(auth_mod)
    assert "app.core.deps" not in inspect.getsource(auth_mod)


def test_missing_bearer_is_401() -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_owner(_request(None))
    assert exc_info.value.status_code == 401


def test_forged_token_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    def _reject(_token: str) -> object:
        raise InvalidAccessToken("forged")

    monkeypatch.setattr(auth_mod, "verify_access_token", _reject)
    with pytest.raises(HTTPException) as exc_info:
        require_owner(_request("Bearer forged.token"))
    assert exc_info.value.status_code == 401


def test_expired_token_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise jwt.ExpiredSignatureError("expired")

    monkeypatch.setattr(auth_mod, "_jwks", lambda: _FakeJwksClient())
    monkeypatch.setattr(jwt, "decode", _raise)
    with pytest.raises(InvalidAccessToken):
        verify_access_token("aaa.bbb.ccc")
    with pytest.raises(HTTPException) as exc_info:
        require_owner(_request("Bearer expired.token"))
    assert exc_info.value.status_code == 401


def test_empty_session_id_is_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    def _decode(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "sub": get_settings().OWNER_AUTH_SUBJECT,
            "role": "authenticated",
            "session_id": "",
        }

    monkeypatch.setattr(auth_mod, "_jwks", lambda: _FakeJwksClient())
    monkeypatch.setattr(jwt, "decode", _decode)
    with pytest.raises(InvalidAccessToken):
        verify_access_token("aaa.bbb.ccc")


def test_other_valid_subject_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    def _ok(_token: str) -> auth_mod.AccessTokenClaims:
        return auth_mod.AccessTokenClaims(
            sub="someone-else",
            session_id="sess-1",
            token=_token,
        )

    monkeypatch.setattr(auth_mod, "verify_access_token", _ok)
    with pytest.raises(HTTPException) as exc_info:
        require_owner(_request("Bearer good.token"))
    assert exc_info.value.status_code == 403


def test_owner_subject_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = get_settings().OWNER_AUTH_SUBJECT

    def _ok(_token: str) -> auth_mod.AccessTokenClaims:
        return auth_mod.AccessTokenClaims(sub=owner, session_id="sess-1", token=_token)

    monkeypatch.setattr(auth_mod, "verify_access_token", _ok)
    claims = require_owner(_request("Bearer good.token"))
    assert claims.sub == owner
    assert claims.token == "good.token"
