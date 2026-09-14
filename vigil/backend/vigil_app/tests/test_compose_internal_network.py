"""Shared internal network is attached only to vigil-backend (issue #452)."""

from __future__ import annotations

from pathlib import Path

_COMPOSE = Path(__file__).resolve().parents[3] / "compose.yml"


def test_vigil_backend_joins_external_portfonia_network() -> None:
    text = _COMPOSE.read_text()
    assert "name: portfonia-vigil-internal" in text
    assert "external: true" in text
    start = text.index("  vigil-backend:")
    end = text.index("  vigil-celery-worker:")
    block = text[start:end]
    assert "portfonia-vigil-internal" in block
    for name in (
        "vigil-postgres:",
        "vigil-redis:",
        "vigil-frontend:",
        "vigil-celery-worker:",
        "vigil-celery-beat:",
    ):
        idx = text.index(f"  {name}")
        nxt = text.find("\n  vigil-", idx + 3)
        block = text[idx : nxt if nxt != -1 else None]
        assert "portfonia-vigil-internal" not in block
