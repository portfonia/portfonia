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


def _normalized(text: str) -> str:
    """Collapse whitespace so an assertion isn't sensitive to exactly where
    the source YAML's `|` block happens to hard-wrap a sentence — the wrap
    position is incidental formatting (LLMs read hard-wrapped prose the same
    as any Markdown paragraph), not semantic content, and shifts easily on
    future edits."""
    return " ".join(text.split())


def test_v2_tightens_item1_default_to_minimal_space_for_session_moves() -> None:
    """2026-08-22 overlay-driven tightening (product owner's read of the
    2026-08-21 comparison): item 1 previously only said a session move
    "earns space only where" tied to structure — it did not say what the
    DEFAULT is absent that tie. v2 makes the default explicit (fact +
    existing connection, nothing more) and adds a self-check the model runs
    before treating a §3 paragraph as finished."""
    framework = load_analysis_framework()
    assert framework.version == "v2"
    text = _normalized(framework.text)
    assert "by default, only a statement" in text
    assert "check what is actually driving its length" in text


def test_v2_deepens_item2_beyond_event_restatement() -> None:
    """Item 2 previously stopped at "this evidence earns depth" without
    saying what that depth should actually trace TO. v2 requires tracing to
    the structural-position change the fact implies, while keeping the
    existing ban on turning that into a favorability judgment."""
    text = _normalized(load_analysis_framework().text)
    assert "not stopping at restating that the event occurred" in text
    assert (
        "not, and must not be turned into, a view about whether an entity's prospects are favorable"
    ) in text


def test_v2_item3_adds_explicit_weight_evidence_rebalance_check() -> None:
    """Item 3's weight/evidence pairing existed in v1 as description; v2
    adds an explicit self-check instruction to rebalance before finalizing
    — the prompt-level version of the product owner's request (the
    code-level automated version is deferred, see issue tracking the
    2026-08-22 follow-up)."""
    text = _normalized(load_analysis_framework().text)
    assert "Before finalizing §3, check each holding's length" in text
    assert "rebalance the allocation" in text


def test_v2_item8_bans_empty_generic_closers_by_name() -> None:
    """Item 8 already banned "closing with a generic statement" but named no
    examples. v2 names the specific phrases the product owner flagged as
    still slipping through, and clarifies they're banned only standing
    ALONE (not banned outright — "watch" language elsewhere in the prompt
    stack, e.g. _SHARED_BODY_RULES' watch-not-do framing, is a different,
    unrelated compliance rule this must not contradict)."""
    text = _normalized(load_analysis_framework().text)
    for phrase in ("worth watching", "a key variable", "bears monitoring"):
        assert phrase in text, f"item 8 v2 missing named example: {phrase!r}"
    assert "never standing alone as the sentence's entire content" in text
