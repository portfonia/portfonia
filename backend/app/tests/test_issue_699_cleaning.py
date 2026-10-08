"""Issue #699: shipped residue rules and StockStory benchmarking titles."""

from datetime import datetime

import pytest

from app.core.timezones import ET
from app.services.headline_cleaning import block_reason, load_cleaning_config
from app.services.instrument_news_sources import CollectedItem
from app.services.intel_body import strip_residue
from app.services.intel_deepen_config import load_intel_deepen_config

PARAGRAPH_A = (
    "Applied Optoelectronics announced a new optical platform that expands manufacturing "
    "capacity and supports its customers worldwide."
)
PARAGRAPH_B = (
    "The company said the new factory will deliver additional products and improve "
    "supply availability this year."
)
EURO_TITLE = "# Record bets against the euro…"
EURO_PARAGRAPH = (
    "## The euro hit a 17-month low as French debt fears and Europe's energy shock "
    "weigh on the currency. Bond market stress could limit further ECB rate hikes."
)
AAOI_HEADING = "## Applied Optoelectronics Shares Trade Higher"


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (
            [
                EURO_TITLE,
                "File - Yannis Stournaras governor of Bank of Greece shows the new 20 euro "
                "note in Athens, Tuesday, 24 November 2015.",
                "Close Button",
                "facebook",
                "twitter",
                EURO_PARAGRAPH,
            ],
            EURO_TITLE + "\n" + EURO_PARAGRAPH,
        ),
        (
            [
                "The above button links to Coinbase. Yahoo Finance is not a broker-dealer "
                "or investment adviser and does not offer securities or cryptocurrencies "
                "for sale or facilitate trading.",
                PARAGRAPH_A,
            ],
            PARAGRAPH_A,
        ),
        (
            [
                PARAGRAPH_A,
                "Ad",
                "This content was partially produced with the help of AI tools and was "
                "reviewed and published by Benzinga editors.",
                "Market News and Data brought to you by Benzinga APIs",
                "Posted In:",
                "Trading IdeasMoversNews",
                "Connect With Us",
                "facebookinstagramlinkedin",
                "About Benzinga",
                "Careers",
                PARAGRAPH_B,
            ],
            PARAGRAPH_A + "\n" + PARAGRAPH_B,
        ),
        (
            [
                PARAGRAPH_A,
                "Read Next",
                "![",
                "Stock Market Today: S&P 500 … in Focus<h5>Stock Market Today: …</h5>"
                "U.S. stock futures were higher on Tuesday, as the Dow Jones, S&P 500 "
                "and Nasdaq 100 indices rose.6 min readRead this article]()",
                AAOI_HEADING,
                PARAGRAPH_B,
            ],
            PARAGRAPH_A + "\n" + AAOI_HEADING + "\n" + PARAGRAPH_B,
        ),
    ],
    ids=["euro-caption", "coinbase-disclaimer", "benzinga-footer", "read-next-card"],
)
def test_699_01_worked_examples(lines: list[str], expected: str) -> None:
    assert strip_residue("\n".join(lines), load_intel_deepen_config()) == expected


def test_699_02_read_next_prose_preserved() -> None:
    paragraph = (
        "Analysts said they will read next week's data closely before revising estimates "
        "for the coming quarter."
    )
    assert strip_residue(paragraph, load_intel_deepen_config()) == paragraph


def test_699_03_file_dot_caption_still_removed() -> None:
    caption = (
        "FILE. Photo of a journalist walking past the central bank building as officials "
        "prepare to announce borrowing costs."
    )
    assert strip_residue(caption + "\n" + PARAGRAPH_A, load_intel_deepen_config()) == PARAGRAPH_A


@pytest.mark.parametrize(
    ("title", "alias", "expected"),
    [
        (
            "Processors and Graphics Chips Stocks Q2 Results: Benchmarking Qualcomm (NASDAQ:QCOM)",
            "Qualcomm",
            "low_value_rule",
        ),
        ("ASML Poised for Q3 Upside on Strong EUV Demand, RBC Says", "ASML", None),
        ("AMD to Report Fiscal Third Quarter 2026 Financial Results", "AMD", None),
    ],
    ids=["stockstory", "asml", "amd"],
)
def test_699_04_stockstory_title_rule(title: str, alias: str, expected: str | None) -> None:
    item = CollectedItem(
        title, datetime(2026, 10, 7, 16, 15, tzinfo=ET), "https://fixture.example/story"
    )
    assert block_reason(item, [alias], [], load_cleaning_config()) == expected


@pytest.mark.parametrize(
    "residue",
    [
        "To add Benzinga News as your preferred source on Google, click here.",
        "Read this article",
    ],
    ids=["preferred-source", "read-this-article"],
)
def test_699_05_interior_residue_removed(residue: str) -> None:
    assert (
        strip_residue("\n".join([PARAGRAPH_A, residue, PARAGRAPH_B]), load_intel_deepen_config())
        == PARAGRAPH_A + "\n" + PARAGRAPH_B
    )
