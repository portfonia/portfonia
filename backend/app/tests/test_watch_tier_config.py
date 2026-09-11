"""Tests for the watch_tier target-weight config (#421)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.watch_tier_config import VALID_WATCH_TIERS, load_watch_tier_weights

_VALID_YAML = """
watch: 0.05
focus: 0.10
critical: 0.15
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "watch_tier_weights.yml"
    p.write_text(text, encoding="utf-8")
    return p


def test_loads_valid_config(tmp_path: Path) -> None:
    weights = load_watch_tier_weights(_write(tmp_path, _VALID_YAML))
    assert weights == {"watch": 0.05, "focus": 0.10, "critical": 0.15}


def test_rejects_missing_tier(tmp_path: Path) -> None:
    broken = "watch: 0.05\nfocus: 0.10\n"
    with pytest.raises(ValueError, match=r"missing.*critical"):
        load_watch_tier_weights(_write(tmp_path, broken))


def test_rejects_unknown_tier(tmp_path: Path) -> None:
    broken = _VALID_YAML + "extreme: 0.25\n"
    with pytest.raises(ValueError, match=r"unrecognized.*extreme"):
        load_watch_tier_weights(_write(tmp_path, broken))


def test_default_config_file_matches_closed_taxonomy() -> None:
    """The shipped config/watch_tier_weights.yml must stay in sync with
    VALID_WATCH_TIERS — regression guard against drift."""
    weights = load_watch_tier_weights()
    assert set(weights) == set(VALID_WATCH_TIERS)


def test_default_config_weights_match_issue_421_design() -> None:
    """5%/10%/15% is a fixed product decision (issue #421 Design item 2),
    not free to drift via a config edit without a matching issue update."""
    weights = load_watch_tier_weights()
    assert weights["watch"] == 0.05
    assert weights["focus"] == 0.10
    assert weights["critical"] == 0.15
