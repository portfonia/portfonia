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


def test_rejects_infinite_wait(tmp_path: Path) -> None:
    """PyYAML parses `.inf` as a float; `inf < 0` is False so the old
    negative-only check let it through. time.sleep(inf) never returns and
    the generating task has no time_limit — a one-character YAML edit could
    pin a Celery worker forever (PR #142 review)."""
    yaml_text = """
ratelimit_backoff_seconds: [5.0, .inf]
connect_backoff_seconds: [30.0, 90.0]
"""
    with pytest.raises(ValueError, match="finite"):
        load_llm_retry_config(_write(tmp_path, yaml_text))


def test_rejects_negative_infinite_wait(tmp_path: Path) -> None:
    yaml_text = """
ratelimit_backoff_seconds: [5.0, -.inf]
connect_backoff_seconds: [30.0, 90.0]
"""
    with pytest.raises(ValueError, match="finite"):
        load_llm_retry_config(_write(tmp_path, yaml_text))


def test_rejects_nan_wait(tmp_path: Path) -> None:
    """nan < 0 is also False — same escape as inf, and nan would otherwise
    fail later inside time.sleep as a bare unqualified ValueError instead of
    the path/key-qualified error this loader promises."""
    yaml_text = """
ratelimit_backoff_seconds: [5.0, .nan]
connect_backoff_seconds: [30.0, 90.0]
"""
    with pytest.raises(ValueError, match="finite"):
        load_llm_retry_config(_write(tmp_path, yaml_text))


def test_rejects_null_entry(tmp_path: Path) -> None:
    """float(None) raises an unadorned TypeError — must surface as a
    path/key-qualified ValueError instead."""
    yaml_text = """
ratelimit_backoff_seconds: [5.0, null]
connect_backoff_seconds: [30.0, 90.0]
"""
    with pytest.raises(ValueError, match="ratelimit_backoff_seconds"):
        load_llm_retry_config(_write(tmp_path, yaml_text))


def test_rejects_non_numeric_string_entry(tmp_path: Path) -> None:
    yaml_text = """
ratelimit_backoff_seconds: ["abc", 5.0]
connect_backoff_seconds: [30.0, 90.0]
"""
    with pytest.raises(ValueError, match="ratelimit_backoff_seconds"):
        load_llm_retry_config(_write(tmp_path, yaml_text))


def test_rejects_wait_above_ceiling(tmp_path: Path) -> None:
    """A typo (3000 instead of 30.0) or an overlong list must not be able to
    block a prefork worker for tens of minutes per LLM call (PR #142
    review, suggestion 1). Ceiling matches Celery's default_retry_delay."""
    yaml_text = """
ratelimit_backoff_seconds: [5.0, 3000.0]
connect_backoff_seconds: [30.0, 90.0]
"""
    with pytest.raises(ValueError, match="exceeds"):
        load_llm_retry_config(_write(tmp_path, yaml_text))


def test_rejects_sequence_too_long(tmp_path: Path) -> None:
    yaml_text = """
ratelimit_backoff_seconds: [1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
connect_backoff_seconds: [30.0, 90.0]
"""
    with pytest.raises(ValueError, match="exceeds"):
        load_llm_retry_config(_write(tmp_path, yaml_text))


def test_default_config_file_loads() -> None:
    """The shipped config/llm_retry.yml must itself be valid — this is the
    regression guard against the default file drifting out of shape."""
    config = load_llm_retry_config()
    assert config.ratelimit_backoff_seconds == (5.0, 15.0)
    assert config.connect_backoff_seconds == (30.0, 90.0)
