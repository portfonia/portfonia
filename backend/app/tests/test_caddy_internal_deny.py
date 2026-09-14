"""P1.2-A03: public reverse proxy must deny /internal/* (issue #452)."""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_caddy_denies_internal_before_backend_proxy() -> None:
    text = (_REPO_ROOT / "Caddyfile").read_text()
    api_block_start = text.index("api.portfonia.com {")
    api_block = text[api_block_start:]
    deny_at = api_block.index("handle /internal/*")
    proxy_at = api_block.index("reverse_proxy backend:8000")
    assert deny_at < proxy_at
    assert "respond 404" in api_block[deny_at:proxy_at]


def test_compose_attaches_only_backend_to_vigil_internal_network() -> None:
    text = (_REPO_ROOT / "docker-compose.yml").read_text()
    assert "portfonia-vigil-internal" in text
    assert "external: true" in text
    backend_idx = text.index("  backend:\n")
    celery_idx = text.index("  celery-worker:\n")
    backend_block = text[backend_idx:celery_idx]
    assert "portfonia-vigil-internal" in backend_block
    for service in ("postgres:", "redis:", "frontend:", "caddy:", "celery-worker:"):
        start = text.index(f"  {service}")
        rest = text[start + 1 :]
        nxt = rest.find("\n  ")
        block = rest[:nxt] if nxt != -1 else rest
        assert "portfonia-vigil-internal" not in block
