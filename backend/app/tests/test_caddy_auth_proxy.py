"""Caddy Auth reverse-proxy config (Ring 1-B decision point 2)."""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_compose_requires_supabase_project_host() -> None:
    """Empty `${VAR:-}` lets Caddy start with `reverse_proxy https://`."""
    text = (_REPO_ROOT / "docker-compose.yml").read_text()
    assert "SUPABASE_PROJECT_HOST: ${SUPABASE_PROJECT_HOST:?required}" in text
