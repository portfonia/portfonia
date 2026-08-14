"""Tests for the admin-editable LLM retry/backoff config (#38)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.llm_retry_config import load_llm_retry_config

_VALID_YAML = """
ratelimit_backoff_seconds: [5.0, 15.0]
connect_backoff_seconds: [30.0, 90.0]
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "llm_retry.yml"
    p.write_text(text, encoding="utf-8")
    return p


def test_loads_valid_config(tmp_path: Path) -> None:
    config = load_llm_retry_config(_write(tmp_path, _VALID_YAML))
    assert config.ratelimit_backoff_seconds == (5.0, 15.0)
    assert config.connect_backoff_seconds == (30.0, 90.0)


def test_empty_list_means_no_retries(tmp_path: Path) -> None:
    """An admin can set a sequence to [] to disable in-process retry for that
    error class entirely — not a config error, a deliberate 'fail fast'."""
    yaml_text = """
ratelimit_backoff_seconds: []
connect_backoff_seconds: [30.0]
"""
    config = load_llm_retry_config(_write(tmp_path, yaml_text))
    assert config.ratelimit_backoff_seconds == ()
    assert config.connect_backoff_seconds == (30.0,)


def test_rejects_missing_key(tmp_path: Path) -> None:
    yaml_text = "ratelimit_backoff_seconds: [5.0, 15.0]\n"
    with pytest.raises(ValueError, match="connect_backoff_seconds"):
        load_llm_retry_config(_write(tmp_path, yaml_text))


def test_rejects_non_list_value(tmp_path: Path) -> None:
    yaml_text = """
ratelimit_backoff_seconds: 5.0
connect_backoff_seconds: [30.0, 90.0]
"""
    with pytest.raises(ValueError, match="must be a list"):
        load_llm_retry_config(_write(tmp_path, yaml_text))


def test_rejects_negative_wait(tmp_path: Path) -> None:
    yaml_text = """
ratelimit_backoff_seconds: [5.0, -1.0]
connect_backoff_seconds: [30.0, 90.0]
"""
    with pytest.raises(ValueError, match="negative"):
        load_llm_retry_config(_write(tmp_path, yaml_text))


def test_default_config_file_loads() -> None:
    """The shipped config/llm_retry.yml must itself be valid — this is the
    regression guard against the default file drifting out of shape."""
    config = load_llm_retry_config()
    assert config.ratelimit_backoff_seconds == (5.0, 15.0)
    assert config.connect_backoff_seconds == (30.0, 90.0)
