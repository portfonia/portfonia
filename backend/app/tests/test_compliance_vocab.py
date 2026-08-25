"""Tests for the compliance vocabulary data loader (config/compliance_vocab.yml, #90).

Mirrors test_asset_class_config.py's pattern (PR #91 review): the compliance
scan/prompt terms and the hand-tuned context-aware regex live in this dedicated
config file now, not inline in forbidden_vocab.py's source, so this is the
regression guard against a bad edit shipping silently.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.compliance.forbidden_vocab import (
    FORBIDDEN_OUTPUT_PATTERNS,
    PROMPT_VOCAB_STRING,
    _load_zh_vocab,
)

_VALID_YAML = """
scan_terms:
  - term: 强烈买入
  - term: 投资建议
scan_regex_patterns:
  - '止损[位点价]'
prompt_only_terms:
  - term: 目标价
context_scan_terms:
  - 止损
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "compliance_vocab.yml"
    p.write_text(text, encoding="utf-8")
    return p


def test_loads_valid_config(tmp_path: Path) -> None:
    vocab = _load_zh_vocab(_write(tmp_path, _VALID_YAML))
    assert vocab.scan_terms == ("强烈买入", "投资建议")
    assert vocab.scan_regex_patterns == ("止损[位点价]",)
    assert vocab.prompt_only_terms == ("目标价",)
    assert vocab.context_scan_terms == ("止损",)


def test_rejects_empty_scan_terms(tmp_path: Path) -> None:
    broken = _VALID_YAML.replace(
        "scan_terms:\n  - term: 强烈买入\n  - term: 投资建议\n", "scan_terms: []\n"
    )
    with pytest.raises(ValueError, match="scan_terms must be a non-empty list"):
        _load_zh_vocab(_write(tmp_path, broken))


def test_rejects_empty_scan_regex_patterns(tmp_path: Path) -> None:
    broken = _VALID_YAML.replace(
        "scan_regex_patterns:\n  - '止损[位点价]'\n", "scan_regex_patterns: []\n"
    )
    with pytest.raises(ValueError, match="scan_regex_patterns must be a non-empty list"):
        _load_zh_vocab(_write(tmp_path, broken))


def test_rejects_scalar_scan_regex_patterns(tmp_path: Path) -> None:
    """A scalar string (missing the YAML list dash) must not silently
    character-split into single-char "patterns" via tuple() (PR #91 re-review)."""
    broken = _VALID_YAML.replace(
        "scan_regex_patterns:\n  - '止损[位点价]'\n", "scan_regex_patterns: 止损位点价\n"
    )
    with pytest.raises(ValueError, match="scan_regex_patterns must be a non-empty list"):
        _load_zh_vocab(_write(tmp_path, broken))


def test_rejects_empty_string_element_in_scan_regex_patterns(tmp_path: Path) -> None:
    broken = _VALID_YAML.replace(
        "scan_regex_patterns:\n  - '止损[位点价]'\n",
        "scan_regex_patterns:\n  - ''\n  - '止损[位点价]'\n",
    )
    with pytest.raises(ValueError, match="scan_regex_patterns must contain only non-empty strings"):
        _load_zh_vocab(_write(tmp_path, broken))


def test_rejects_empty_prompt_only_terms(tmp_path: Path) -> None:
    broken = _VALID_YAML.replace(
        "prompt_only_terms:\n  - term: 目标价\n", "prompt_only_terms: []\n"
    )
    with pytest.raises(ValueError, match="prompt_only_terms must be a non-empty list"):
        _load_zh_vocab(_write(tmp_path, broken))


def test_rejects_empty_context_scan_terms(tmp_path: Path) -> None:
    broken = _VALID_YAML.replace("context_scan_terms:\n  - 止损\n", "context_scan_terms: []\n")
    with pytest.raises(ValueError, match="context_scan_terms must be a non-empty list"):
        _load_zh_vocab(_write(tmp_path, broken))


def test_rejects_scalar_context_scan_terms(tmp_path: Path) -> None:
    """A scalar string must not silently character-split into single-char
    prompt terms via tuple() — same class of bug as scan_regex_patterns
    (PR #91 re-review)."""
    broken = _VALID_YAML.replace("context_scan_terms:\n  - 止损\n", "context_scan_terms: 止损\n")
    with pytest.raises(ValueError, match="context_scan_terms must be a non-empty list"):
        _load_zh_vocab(_write(tmp_path, broken))


def test_rejects_malformed_regex(tmp_path: Path) -> None:
    broken = _VALID_YAML.replace("止损[位点价]", "止损[位点价")  # unbalanced bracket
    with pytest.raises(re.error):
        _load_zh_vocab(_write(tmp_path, broken))


def test_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _load_zh_vocab(tmp_path / "does_not_exist.yml")


# Golden regression: the shipped config/compliance_vocab.yml's exact term set.
# A single-character edit to the hard-won 止损 v3 regex (#74/#75) fails here,
# not silently as a weaker/wrong compliance backstop in production.
_GOLDEN_SCAN_TERMS = ("强烈买入", "投资建议", "超买", "超卖")
_GOLDEN_SCAN_REGEX_PATTERNS = (
    r"(建议|应该|需要|请)(?:(?!关注|留意|注意|警惕)[^。,，、\n]){0,6}止损",  # noqa: RUF001
    r"止损[位点价]",
    r"(立即|立刻|马上|赶紧)[^。,，、\n]{0,2}止损(?!位|点|价|驱动|盘|单)",  # noqa: RUF001
    r"(跌破|涨破|触及|低于|高于)(?=[^。,，、\n]{0,10}\d)[^。,，、\n]{0,10}止损(?!位|点|价|线|驱动|盘|单)",  # noqa: RUF001
    r"(建议|应该|需要|请)(?:(?!关注|留意|注意|警惕)[^。,，、\n]){0,6}清仓",  # noqa: RUF001
    r"(?<!被迫)(?<!强制)(立即|立刻|马上|赶紧|尽快)(?:(?!披露|宣布|启动|公告)[^。,，、\n]){0,6}清仓(?!了|程序|计划|动作|流程)",  # noqa: RUF001
    r"(全部|直接|完全)清仓(?!了)",
    r"(跌破|涨破|触及|低于|高于)(?=[^。,，、\n]{0,10}\d)[^。,，、\n]{0,10}清仓(?!了)",  # noqa: RUF001
)
_GOLDEN_PROMPT_ONLY_TERMS = ("目标价", "增持", "减持", "入场")
_GOLDEN_CONTEXT_SCAN_TERMS = ("止损", "清仓")

_EN_REGEX_PATTERN_COUNT = 12  # forbidden_vocab._EN_REGEX_PATTERNS, unchanged by #90


def test_default_config_matches_golden_vocab() -> None:
    vocab = _load_zh_vocab()
    assert vocab.scan_terms == _GOLDEN_SCAN_TERMS
    assert vocab.scan_regex_patterns == _GOLDEN_SCAN_REGEX_PATTERNS
    assert vocab.prompt_only_terms == _GOLDEN_PROMPT_ONLY_TERMS
    assert vocab.context_scan_terms == _GOLDEN_CONTEXT_SCAN_TERMS


def test_module_level_singletons_reflect_shipped_config() -> None:
    """FORBIDDEN_OUTPUT_PATTERNS / PROMPT_VOCAB_STRING built at import time from
    the shipped file — public API surface, unchanged by the #90 data move."""
    expected_pattern_count = (
        _EN_REGEX_PATTERN_COUNT + len(_GOLDEN_SCAN_TERMS) + len(_GOLDEN_SCAN_REGEX_PATTERNS)
    )
    assert len(FORBIDDEN_OUTPUT_PATTERNS) == expected_pattern_count
    for term in _GOLDEN_SCAN_TERMS + _GOLDEN_PROMPT_ONLY_TERMS + _GOLDEN_CONTEXT_SCAN_TERMS:
        assert term in PROMPT_VOCAB_STRING
