"""Issue #630 body cleaning acceptance, using URL-free residue fixtures."""

from pathlib import Path

import pytest
import yaml

from app.services.intel_body import body_verdict, clean_body
from app.services.intel_deepen_config import load_intel_deepen_config

ARTICLE = "The company announced a new financing agreement that supports production growth and expands capacity."


def clean(text: str, host: str = "example.com") -> str:
    return clean_body("https://" + host + "/article", text, load_intel_deepen_config())


def test_630_01_navigation_page() -> None:
    text = "\n".join(
        [
            "Skip to navigation Skip to right column",
            "# Yahoo Finance",
            "Sign in",
            "1. My Portfolio",
            "2. 1. Markets",
            "1. Stock market",
            "4. Student Loan Forgiveness",
        ]
    )
    # This page has no Yahoo-specific marker and no prose paragraph.
    assert clean(text, "finance.yahoo.com") == ""
    assert body_verdict(clean(text, "finance.yahoo.com"), load_intel_deepen_config()) == (
        False,
        "empty",
    )


def test_630_02_byline_and_title() -> None:
    title = "# Amkor (AMKR) Stock Could Be 45% Undervalued On Cash Flow"
    paragraph = "Amkor Technology has delivered strong share gains as demand for advanced packaging expands across its global customers."
    text = "\n".join(
        [
            "United States",
            "Semiconductors",
            "NasdaqGS:AMKR",
            title,
            "Simply Wall St",
            "Reviewed byBailey",
            paragraph,
        ]
    )
    result = clean(text)
    assert result.startswith(title + "\nAmkor Technology has delivered strong share gains")
    assert not any(word in result for word in ["Simply Wall St", "Reviewed by", "NasdaqGS:AMKR"])


def test_630_03_prompts_photo_timestamp() -> None:
    title = "# Tesla posts stronger-than-expected quarterly deliveries"
    paragraph = "Shares of the Austin, Texas-based company rose nearly 2 per cent after quarterly deliveries exceeded expectations."
    text = "\n".join(
        [
            title,
            "Sign up now: Get ST's newsletters delivered to your inbox",
            "Tesla shares rose nearly 2 per cent in pre-market trading.",
            "PHOTO: REUTERS",
            "Published Oct 02, 2026, 09:39 PM",
            paragraph,
        ]
    )
    result = clean(text)
    assert result.splitlines()[0] == title
    assert result.splitlines()[1].startswith("Shares of the Austin")
    assert not any(word in result for word in ["Sign up now", "PHOTO:", "Published Oct 02"])


def test_630_04_section_block() -> None:
    text = "\n".join(
        [
            ARTICLE,
            "## Most read",
            "### Diesel taxes in Europe: Which countries charge the most?",
            "### Stellantis to halt production at another plant",
            "A fuel station shows record fuel prices as supply restrictions spread across several European markets.",
        ]
    )
    result = clean(text)
    assert "###" not in result and "Most read" not in result
    assert ARTICLE in result
    assert "A fuel station shows record fuel prices" in result


def test_630_05_ticker_strip_and_menu() -> None:
    text = "\n".join(
        [ARTICLE, "* VRT +2.46%", "* COHR +5.59%", "1. My Portfolio", "2. 1. Markets", ARTICLE]
    )
    result = clean(text)
    assert result == ARTICLE + "\n" + ARTICLE
    assert clean(text, "finance.yahoo.com") == result


def test_630_06_truncation_order() -> None:
    residue = ("Advertisement\n" * 108)[:1500]
    paragraph = (ARTICLE + " ") * 19
    paragraph = paragraph[:1799] + "."
    assert len(residue) == 1500 and len(paragraph) == 1800
    result = clean(residue + "\n" + paragraph)
    assert len(result) <= 2000
    assert paragraph[:1700] in result
    assert result == paragraph
    assert "Advertisement" not in result


def test_630_07_bare_domains_and_company_names() -> None:
    amazon = "Amazon.com said on Thursday it would offload about $8 billion of Nvidia chips to outside investors."
    path = "The company released its financing details today; see example.com/markets/today for details about the transaction."
    text = "\n".join([amazon, "3 hours ago  |  247wallst.com", "Straitstimes.com", path, amazon])
    result = clean(text)
    assert result == "\n".join([amazon, path.replace("example.com/markets/today", ""), amazon])
    assert "247wallst.com" not in result and "Straitstimes.com" not in result
    assert result.count(amazon) == 2


def test_630_08_paywall_marker() -> None:
    text = "available only for our paid subscribers" + "x" * 361
    assert len(text) == 400
    assert body_verdict(text, load_intel_deepen_config()) == (False, "paywall")


def test_630_09_invalid_residue_regex(tmp_path: Path) -> None:
    data = load_intel_deepen_config().model_dump()
    data["body_cleaning"] = {
        "paragraph_min_words": 12,
        "paragraph_min_cjk_chars": 30,
        "residue_line_patterns": ["["],
        "section_block_headings": ["most read"],
    }
    path = tmp_path / "deepen.yml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError):
        load_intel_deepen_config(path)


def test_630_23_cjk_paragraphs() -> None:
    paragraph = (
        "\u516c\u53f8\u5ba3\u5e03\u65b0\u7684\u878d\u8d44\u534f\u8bae\u652f\u6301\u4ea7\u80fd\u6269\u5f20\u5e76\u63d0\u5347\u751f\u4ea7\u6548\u7387"
        * 8
    ) + "\u3002"
    second = (
        "\u8463\u4e8b\u4f1a\u6279\u51c6\u56de\u8d2d\u8ba1\u5212\u5e76\u9884\u8ba1\u672a\u6765\u6536\u5165\u5c06\u6301\u7eed\u589e\u957f"
        * 8
    ) + "\u3002"
    short = "\u516c" * 20 + "\u3002"
    assert load_intel_deepen_config().body_cleaning.paragraph_min_cjk_chars == 30
    result = clean("\n".join([short, paragraph, second]))
    assert result == paragraph + "\n" + second
    assert body_verdict(result, load_intel_deepen_config()) == (True, None)


@pytest.mark.parametrize(
    "paragraph",
    [
        "Related to the buyback, the board said on Thursday it would expand the program by $5 billion this year.",
        "Updated its 2026 outlook after the board approved a larger buyback program on Thursday evening.",
        "Published Oct 02 a detailed financing update after the board approved a larger buyback program on Thursday evening.",
        "The board discussed its trading disclosure obligations after approving a larger buyback program on Thursday evening.",
    ],
)
def test_630_24_prose_not_residue(paragraph: str) -> None:
    assert clean(paragraph) == paragraph


@pytest.mark.parametrize(
    "residue", ["Published Oct 02, 2026", "# Trading disclosure", "# Yahoo Finance"]
)
def test_630_24_short_residue_removed(residue: str) -> None:
    assert clean("\n".join([ARTICLE, residue, ARTICLE])) == ARTICLE + "\n" + ARTICLE


def test_630_25_last_title_before_prose() -> None:
    text = "\n".join(
        ["# Yahoo Finance", "Sign in", "# Earlier headline", "# Real headline", ARTICLE]
    )
    assert clean(text) == "# Real headline\n" + ARTICLE
