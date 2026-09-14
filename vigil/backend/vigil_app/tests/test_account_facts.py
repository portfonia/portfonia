"""Account-facts client + cache (issue #452). Mock HTTP, no live Portfonia."""

from __future__ import annotations

import httpx
import pytest

from vigil_app.services.account_facts import (
    AccountFactsClient,
    IdentityServiceError,
)


def _payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "auth_subject": "owner-sub-test",
        "eligible": True,
        "account_email": "owner@example.com",
        "email_verified_at": "2026-01-01T00:00:00Z",
        "observed_at": "2026-09-14T12:00:00Z",
    }
    body.update(overrides)
    return body


def _http(handler: object) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        base_url="http://backend:8000",
    )


def test_fetches_internal_path_with_identity_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_payload())

    client = AccountFactsClient(
        http=_http(handler),
        identity_token="identity-token-test",
        cache_ttl_seconds=60,
        monotonic=lambda: 0.0,
    )
    facts = client.fetch("owner-sub-test")
    assert facts.eligible is True
    assert facts.account_email == "owner@example.com"
    assert seen[0].url.path == "/internal/vigil/principals/owner-sub-test"
    assert seen[0].headers["authorization"] == "Bearer identity-token-test"


def test_routine_cache_reuses_within_60s() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_payload())

    clock = {"t": 0.0}
    client = AccountFactsClient(
        http=_http(handler),
        identity_token="identity-token-test",
        cache_ttl_seconds=60,
        monotonic=lambda: clock["t"],
    )
    client.fetch("owner-sub-test")
    clock["t"] = 59.0
    client.fetch("owner-sub-test")
    assert calls["n"] == 1


def test_force_fresh_bypasses_cache() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_payload())

    client = AccountFactsClient(
        http=_http(handler),
        identity_token="identity-token-test",
        cache_ttl_seconds=60,
        monotonic=lambda: 0.0,
    )
    client.fetch("owner-sub-test")
    client.fetch("owner-sub-test", force_fresh=True)
    assert calls["n"] == 2


def test_ttl_capped_at_60s() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_payload())

    clock = {"t": 0.0}
    client = AccountFactsClient(
        http=_http(handler),
        identity_token="identity-token-test",
        cache_ttl_seconds=600,
        monotonic=lambda: clock["t"],
    )
    client.fetch("owner-sub-test")
    clock["t"] = 61.0
    client.fetch("owner-sub-test")
    assert calls["n"] == 2


def test_http_error_is_not_ineligible() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = AccountFactsClient(
        http=_http(handler),
        identity_token="identity-token-test",
        cache_ttl_seconds=60,
        monotonic=lambda: 0.0,
    )
    with pytest.raises(IdentityServiceError):
        client.fetch("owner-sub-test")


def test_module_fetch_reuses_cache_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_payload())

    from vigil_app.services import account_facts as facts_mod

    facts_mod.reset_account_facts_client()
    http = _http(handler)
    client = AccountFactsClient(
        http=http,
        identity_token="identity-token-test",
        cache_ttl_seconds=60,
        monotonic=lambda: 0.0,
    )
    monkeypatch.setattr(facts_mod, "_shared_client", lambda: client)
    facts_mod.fetch_account_facts("owner-sub-test")
    facts_mod.fetch_account_facts("owner-sub-test")
    assert calls["n"] == 1
    facts_mod.reset_account_facts_client()


def test_non_200_is_not_ineligible() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = AccountFactsClient(
        http=_http(handler),
        identity_token="identity-token-test",
        cache_ttl_seconds=60,
        monotonic=lambda: 0.0,
    )
    with pytest.raises(IdentityServiceError):
        client.fetch("owner-sub-test")
