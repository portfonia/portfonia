"""Default-deny authentication coverage for every API route (issue #645)."""

from __future__ import annotations

from collections.abc import Callable

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from app.core.agent_auth import agent_principal
from app.core.deps import current_principal, require_ops_token
from app.main import _api_doc_urls, app

AUTH_DEPENDENCIES = {current_principal, require_ops_token, agent_principal}

# Intentionally public routes. Adding one requires a reason comment here.
PUBLIC_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api-tokens/revoke-by-link"),  # HMAC-signed, 30-day link
        ("GET", "/auth/invite-email"),  # invite token plus Redis limit
        ("POST", "/auth/signup"),  # invite token plus Redis limit
        ("GET", "/auth/altcha-challenge"),  # stateless Altcha challenge
        ("POST", "/auth/forgot-password"),  # Altcha plus Redis limit
        ("GET", "/invitation-letters/unsubscribe"),  # HMAC-signed unsubscribe link
        ("POST", "/invitation-letters/unsubscribe"),  # HMAC-signed unsubscribe link
        ("GET", "/waitlist/altcha-challenge"),  # stateless Altcha challenge
        ("POST", "/waitlist"),  # Altcha plus Redis limit
        ("POST", "/webhooks/paddle"),  # Paddle signature verification
        ("GET", "/email-verifications/altcha-challenge"),  # stateless Altcha challenge
        ("GET", "/email-verifications/status"),  # verification token lookup
        ("POST", "/email-verifications/confirm"),  # verification token plus Altcha
        ("GET", "/unsubscribe/status"),  # HMAC-signed unsubscribe token
        ("POST", "/unsubscribe/confirm"),  # HMAC-signed unsubscribe token
        ("GET", "/health"),  # liveness probe, no user data
    }
)


def _dependency_calls(dependant: Dependant) -> set[Callable[..., object]]:
    """All callables in the dependency tree, recursively."""
    calls: set[Callable[..., object]] = set()
    for dep in dependant.dependencies:
        if dep.call is not None:
            calls.add(dep.call)
        calls |= _dependency_calls(dep)
    return calls


def test_every_route_is_authenticated_or_explicitly_public() -> None:
    offenders: list[str] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if _dependency_calls(route.dependant).isdisjoint(AUTH_DEPENDENCIES):
            for method in sorted(route.methods):
                if (method, route.path) not in PUBLIC_ROUTES:
                    offenders.append(f"{method} {route.path}")
    assert not offenders, "unauthenticated routes missing from PUBLIC_ROUTES: " + ", ".join(
        offenders
    )


def test_public_allowlist_has_no_stale_entries() -> None:
    known = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    stale = sorted(f"{method} {path}" for method, path in PUBLIC_ROUTES - known)
    assert not stale, "PUBLIC_ROUTES entries with no route: " + ", ".join(stale)


def test_public_allowlist_entries_are_actually_unauthenticated() -> None:
    by_pair = {
        (method, route.path): route
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    offenders = [
        f"{method} {path}"
        for method, path in sorted(PUBLIC_ROUTES)
        if not _dependency_calls(by_pair[(method, path)].dependant).isdisjoint(AUTH_DEPENDENCIES)
    ]
    assert not offenders, "allowlisted routes that now have auth: " + ", ".join(offenders)


def test_api_doc_urls_disabled_in_production() -> None:
    assert _api_doc_urls("production") == {
        "openapi_url": None,
        "docs_url": None,
        "redoc_url": None,
    }
    assert _api_doc_urls("development") == {
        "openapi_url": "/openapi.json",
        "docs_url": "/docs",
        "redoc_url": "/redoc",
    }
