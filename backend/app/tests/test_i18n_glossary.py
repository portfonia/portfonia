"""Tests for the locale-keyed i18n glossary loader (config/i18n_glossary.yml, #90).

Negative-path coverage the PR #91 review flagged as missing: prior tests only
exercised the happy path (confidence-label glossary pairs, TA allow-list,
email subject) via the shipped file, with no test forcing load_i18n_glossary
itself to fail loudly on a structurally broken config.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services import report_translation as rt
from app.services.asset_class_config import VALID_ASSET_CLASSES
from app.services.i18n_glossary import load_i18n_glossary, locale_for_output_lang

_VALID_YAML = """
supported_locales: [zh-Hans]

report_glossary:
  "Example Term":
    zh-Hans: 示例

forbidden_renderings:
  "Intelligence":
    zh-Hans: 智能

ta_observation_terms:
  support:
    zh-Hans: 支撑位

vendor_names:
  "Example Vendor":
    zh-Hans: 示例厂商

templates:
  example_template:
    en: "Example $x"
    zh-Hans: "示例 $x"

release_delay_terms_zh: [政府停摆]
legacy_removed_markers_zh: [新闻]
body_disclaimer_regex_terms_zh: [投资建议]
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "i18n_glossary.yml"
    p.write_text(text, encoding="utf-8")
    return p


def test_loads_valid_config(tmp_path: Path) -> None:
    g = load_i18n_glossary(_write(tmp_path, _VALID_YAML))
    assert g.supported_locales == frozenset({"zh-Hans"})
    assert g.report_glossary["Example Term"]["zh-Hans"] == "示例"
    assert g.templates["example_template"]["en"] == "Example $x"


def test_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_i18n_glossary(tmp_path / "does_not_exist.yml")


def test_rejects_locale_section_missing_supported_locale(tmp_path: Path) -> None:
    broken = _VALID_YAML.replace("    zh-Hans: 示例\n", "    fr: exemple\n")
    with pytest.raises(ValueError, match=r"report_glossary\['Example Term'\].*zh-Hans"):
        load_i18n_glossary(_write(tmp_path, broken))


def test_rejects_template_missing_en(tmp_path: Path) -> None:
    broken = _VALID_YAML.replace('    en: "Example $x"\n', "")
    with pytest.raises(ValueError, match=r"templates\['example_template'\].*en"):
        load_i18n_glossary(_write(tmp_path, broken))


def test_rejects_template_missing_supported_locale(tmp_path: Path) -> None:
    broken = _VALID_YAML.replace('    zh-Hans: "示例 $x"\n', "")
    with pytest.raises(ValueError, match=r"templates\['example_template'\].*zh-Hans"):
        load_i18n_glossary(_write(tmp_path, broken))


@pytest.mark.parametrize(
    "field",
    ["release_delay_terms_zh", "legacy_removed_markers_zh", "body_disclaimer_regex_terms_zh"],
)
def test_rejects_empty_flat_list_field(tmp_path: Path, field: str) -> None:
    broken = re.sub(rf"^{field}:.*$", f"{field}: []", _VALID_YAML, flags=re.MULTILINE)
    with pytest.raises(ValueError, match=f"{field} must be a non-empty list"):
        load_i18n_glossary(_write(tmp_path, broken))


@pytest.mark.parametrize(
    "field",
    ["release_delay_terms_zh", "legacy_removed_markers_zh", "body_disclaimer_regex_terms_zh"],
)
def test_rejects_scalar_flat_list_field(tmp_path: Path, field: str) -> None:
    """A scalar string (missing the YAML list brackets) must not silently
    character-split via "|".join() (PR #91 re-review)."""
    broken = re.sub(rf"^{field}:.*$", f"{field}: 政府停摆", _VALID_YAML, flags=re.MULTILINE)
    with pytest.raises(ValueError, match=f"{field} must be a non-empty list"):
        load_i18n_glossary(_write(tmp_path, broken))


@pytest.mark.parametrize(
    "field",
    ["release_delay_terms_zh", "legacy_removed_markers_zh", "body_disclaimer_regex_terms_zh"],
)
def test_rejects_empty_string_element_in_flat_list_field(tmp_path: Path, field: str) -> None:
    broken = re.sub(rf"^{field}:.*$", f'{field}: ["", "政府停摆"]', _VALID_YAML, flags=re.MULTILINE)
    with pytest.raises(ValueError, match=f"{field} must contain only non-empty strings"):
        load_i18n_glossary(_write(tmp_path, broken))


def test_rejects_malformed_body_disclaimer_regex(tmp_path: Path) -> None:
    broken = _VALID_YAML.replace("投资建议", "投资建议(")  # unbalanced paren
    with pytest.raises(re.error):
        load_i18n_glossary(_write(tmp_path, broken))


def test_report_glossary_covers_every_valid_asset_class() -> None:
    """Every closed-taxonomy asset_class key must ship a zh-Hans report label
    (issue #297) — a 14th asset_class must not silently render as raw English
    in the translated report again."""
    glossary = load_i18n_glossary()
    missing = VALID_ASSET_CLASSES - set(glossary.report_glossary)
    assert not missing, f"asset_class keys missing a zh-Hans label: {sorted(missing)}"


def test_locale_for_output_lang_maps_zh() -> None:
    assert locale_for_output_lang("zh") == "zh-Hans"


def test_locale_for_output_lang_unknown_passthrough() -> None:
    assert locale_for_output_lang("fr") == "fr"


def test_default_config_loads_without_error() -> None:
    """The shipped config/i18n_glossary.yml must itself pass load-time validation."""
    load_i18n_glossary()


def test_build_glossary_instruction_unknown_target_lang_returns_empty() -> None:
    assert rt._build_glossary_instruction("fr") == ""


def test_build_glossary_instruction_zh_contains_full_fixed_term_set() -> None:
    instruction = rt._build_glossary_instruction("zh")
    assert '"Portfonia Financial Analysis Report" -> "Portfonia 财经分析报告"' in instruction
    assert 'Never render any word as "智能"' in instruction
