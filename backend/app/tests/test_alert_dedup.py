"""Tests for the durable ops-alert dedup store (issue #298)."""

from __future__ import annotations

from app.core import alert_dedup


def test_in_memory_backend_contains_and_add() -> None:
    backend = alert_dedup.InMemoryBackend()
    assert backend.contains("k") is False
    backend.add("k", ttl_seconds=60)
    assert backend.contains("k") is True


def test_mark_then_already_alerted() -> None:
    backend = alert_dedup.InMemoryBackend()
    alert_dedup.set_backend(backend)
    try:
        assert alert_dedup.already_alerted("fund-x-2026-08-27") is False
        alert_dedup.mark_alerted("fund-x-2026-08-27", ttl_seconds=60)
        assert alert_dedup.already_alerted("fund-x-2026-08-27") is True
    finally:
        alert_dedup.set_backend(None)


class _RaisingBackend:
    """Simulates a Redis outage: both reads fail."""

    def contains(self, key: str) -> bool:
        raise alert_dedup.AlertDedupUnavailable

    def add(self, key: str, ttl_seconds: int) -> None:
        raise alert_dedup.AlertDedupUnavailable


def test_fail_open_on_store_outage() -> None:
    """A Redis outage must not silence the alert: already_alerted reads False
    and mark_alerted does not raise. The dedup is anti-spam only; the alert
    itself is the safety net (issue #298 review)."""
    alert_dedup.set_backend(_RaisingBackend())
    try:
        assert alert_dedup.already_alerted("k") is False
        alert_dedup.mark_alerted("k", ttl_seconds=60)  # must not raise
    finally:
        alert_dedup.set_backend(None)
