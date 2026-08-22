"""Tests for the system default analysis framework loader (issue #128 Ring 1
stage B, checkpoint B1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.analysis_framework import load_analysis_framework

_VALID_YAML = """
version: "test-v1"
text: |
  ANALYSIS FRAMEWORK (house analytical stance)
  Some framework text.
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "analysis_framework.yml"
    p.write_text(text, encoding="utf-8")
    return p


def test_loads_valid_config(tmp_path: Path) -> None:
    framework = load_analysis_framework(_write(tmp_path, _VALID_YAML))
    assert framework.version == "test-v1"
    assert "ANALYSIS FRAMEWORK" in framework.text
    assert "Some framework text." in framework.text


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not found"):
        load_analysis_framework(tmp_path / "does_not_exist.yml")


def test_empty_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="version"):
        load_analysis_framework(_write(tmp_path, ""))


def test_missing_version_raises(tmp_path: Path) -> None:
    broken = "text: |\n  Some framework text.\n"
    with pytest.raises(ValueError, match="version"):
        load_analysis_framework(_write(tmp_path, broken))


def test_blank_version_raises(tmp_path: Path) -> None:
    broken = 'version: "  "\ntext: |\n  Some framework text.\n'
    with pytest.raises(ValueError, match="version"):
        load_analysis_framework(_write(tmp_path, broken))


def test_missing_text_raises(tmp_path: Path) -> None:
    broken = 'version: "test-v1"\n'
    with pytest.raises(ValueError, match="text"):
        load_analysis_framework(_write(tmp_path, broken))


def test_blank_text_raises(tmp_path: Path) -> None:
    broken = 'version: "test-v1"\ntext: "   "\n'
    with pytest.raises(ValueError, match="text"):
        load_analysis_framework(_write(tmp_path, broken))


def test_default_config_file_loads_and_has_all_eight_items() -> None:
    """The shipped config/analysis_framework.yml must actually load, and
    must carry all eight numbered attention-allocation items plus the
    self-limiting clause — the regression guard against a future edit
    silently dropping one."""
    framework = load_analysis_framework()
    assert framework.version
    for marker in (
        "ANALYSIS FRAMEWORK",
        "1. TIME HORIZON",
        "2. STRUCTURAL EVIDENCE EARNS DEPTH",
        "3. PORTFOLIO SHAPE",
        "4. MACRO AND GEOPOLITICAL TRANSMISSION",
        "5. RELEVANCE, NOT PREVALENCE",
        "6. CONDITION CHANGE WITHOUT FORECAST",
        "7. VALUATION AS A DOCUMENTED RELATIONSHIP",
        "8. TRACE TO A NAMED OBSERVABLE",
        "SELF-LIMITING CLAUSE",
    ):
        assert marker in framework.text, f"analysis_framework.yml missing: {marker}"


def test_default_config_framework_never_names_a_holding_or_advises() -> None:
    """The framework text itself must stay generic — no specific ticker/fund
    name (it applies to every user's portfolio, not this product's own
    developer's holdings) and no directive verb that would pre-empt the
    Layer-4 output scan by baking a "should" into the system prompt itself."""
    framework = load_analysis_framework()
    lowered = framework.text.lower()
    for forbidden in ("should", "recommend", "you must buy", "you must sell"):
        assert forbidden not in lowered, f"analysis_framework.yml text contains: {forbidden!r}"
