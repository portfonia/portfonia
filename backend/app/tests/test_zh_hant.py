"""Shared Traditional Chinese converter contract (issue #582)."""

from pathlib import Path

import pytest

from app.services.zh_hant import load_terms, to_traditional


def test_conversion_preserves_markdown_and_numbers() -> None:
    source = "| AAPL | \u8f6f\u4ef6\u548c\u5185\u5b58\u4eca\u5929\u4e0a\u6da8 2.3% |"
    expected = "| AAPL | \u8edf\u9ad4\u548c\u8a18\u61b6\u9ad4\u4eca\u5929\u4e0a\u6f32 2.3% |"
    assert to_traditional(source) == expected
    assert to_traditional(to_traditional(source)) == expected


def test_overrides_win_and_longest_source_wins() -> None:
    source = "\u7eb3\u6307\u548c\u7eb3\u65af\u8fbe\u514b\uff0c\u6676\u5706\u4ee3\u5de5\u5382\uff0c\u6301\u4ed3\u673a\u6784"
    expected = "\u90a3\u65af\u9054\u514b\u548c\u90a3\u65af\u9054\u514b\uff0c\u6676\u5713\u4ee3\u5de5\u5ee0\uff0c\u4fdd\u7ba1\u6a5f\u69cb"
    assert to_traditional(source) == expected
    assert to_traditional(expected) == expected


@pytest.mark.parametrize("source", ["", "| AAPL | 2.3% | [link](https://example.com)\n"])
def test_ascii_is_unchanged(source: str) -> None:
    assert to_traditional(source) == source


@pytest.mark.parametrize(
    "content", ["source: ''", "source: one\nsource: two", "'': target", "source: 42"]
)
def test_invalid_override_file_raises(tmp_path: Path, content: str) -> None:
    path = tmp_path / "terms.yml"
    path.write_text(content)
    with pytest.raises(ValueError):
        load_terms(path)


def test_all_glossary_values_are_idempotent() -> None:
    from app.services.i18n_glossary import load_i18n_glossary

    glossary = load_i18n_glossary()
    for section in [glossary.report_glossary, glossary.templates, glossary.forbidden_renderings]:
        for translations in section.values():
            for value in translations.values():
                converted = to_traditional(value)
                assert to_traditional(converted) == converted
