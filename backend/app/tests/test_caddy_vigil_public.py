"""Vigil frontend reverse proxy: vigil.portfonia.com (P1.3 deploy follow-up).

portfonia-vigil-public is a separate, narrowly scoped external network from
portfonia-vigil-internal (issue #452, test_caddy_internal_deny.py) — the two
must never collapse into one. Internal is for the backend-to-backend
identity-check hop; public is for Caddy to reach vigil-frontend.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]

# A real top-level service key starts a new line at exactly two-space
# indent followed by a non-space character (e.g. "\n  caddy:"). A nested
# property line ("\n    image: ...", four-space indent) also contains the
# substring "\n  " as its first three characters — plain substring search
# for "\n  " matches those too and cuts the block short. Anchor with a
# regex instead so only real top-level keys count as a boundary.
_NEXT_TOP_LEVEL_KEY = re.compile(r"\n {2}\S")


def _service_block(text: str, service: str) -> str:
    start = text.find(f"\n  {service}")
    assert start != -1, f"missing service block for {service}"
    rest = text[start + 1 :]
    m = _NEXT_TOP_LEVEL_KEY.search(rest)
    return rest[: m.start()] if m else rest


def _only_service_on_network(text: str, network: str, expected_service: str) -> None:
    services = (
        "postgres:",
        "redis:",
        "backend:",
        "frontend:",
        "caddy:",
        "celery-worker:",
        "celery-beat:",
    )
    for service in services:
        if text.find(f"\n  {service}") == -1:
            continue
        block = _service_block(text, service)
        if service == f"{expected_service}:":
            assert network in block, f"{expected_service} must attach to {network}"
        else:
            assert network not in block, f"{service} must not attach to {network}"


def test_caddy_reverse_proxies_vigil_domain_to_vigil_frontend() -> None:
    text = (_REPO_ROOT / "Caddyfile").read_text()
    vigil_block_start = text.index("vigil.portfonia.com {")
    vigil_block = text[vigil_block_start:]
    assert "reverse_proxy vigil-frontend:3000" in vigil_block


def test_compose_attaches_only_caddy_to_vigil_public_network() -> None:
    text = (_REPO_ROOT / "docker-compose.yml").read_text()
    assert "portfonia-vigil-public" in text
    networks_block = text[: text.index("services:")]
    assert "portfonia-vigil-public" in networks_block and "external: true" in networks_block
    _only_service_on_network(text, "portfonia-vigil-public", "caddy")


def test_vigil_compose_attaches_only_frontend_to_vigil_public_network() -> None:
    text = (_REPO_ROOT / "vigil" / "compose.yml").read_text()
    assert "portfonia-vigil-public" in text
    services = ("vigil-postgres:", "vigil-redis:", "vigil-backend:", "vigil-celery-worker:")
    for service in services:
        block = _service_block(text, service)
        assert "portfonia-vigil-public" not in block, f"{service} must not attach to public net"

    start = text.index("\n  vigil-frontend:")
    frontend_block = text[start:]
    assert "portfonia-vigil-public" in frontend_block
