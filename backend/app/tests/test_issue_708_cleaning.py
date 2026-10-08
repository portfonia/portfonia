"""Issue #708: shipped Barchart, share-button and City AM residue rules."""

import pytest

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
BARCHART_TITLE = (
    "# Lumentum Could See a $3.3 Billion Payoff. Nvidia's Optics Shift Is Moving "
    "Faster Than Expected."
)
BARCHART_BYLINE = r"Jabran Kundi - Barchart \- Wed Oct 7, 8:26AM CDT Columnist"
BARCHART_HEADING = "## Nvidia's Optics Shift Is Arriving Early"


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (
            [
                "Your Free 7-Day Premier trial ends on . Extend Your Trial For Another "
                "30 Days . Time remaining NaN d Days",
                ":",
                "NaN h Hours",
                "News Menu",
                "+ Markets Today",
                BARCHART_TITLE,
                BARCHART_BYLINE,
                "Share",
                "*",
                "Follow us on Google News Follow us on Google News Follow this Author Follow",
                "Growth & Income by Koto Amatsukami via Shutterstock",
                BARCHART_HEADING,
                PARAGRAPH_A,
            ],
            "\n".join([BARCHART_TITLE, BARCHART_BYLINE, BARCHART_HEADING, PARAGRAPH_A]),
        ),
        (
            [
                PARAGRAPH_A,
                "Close Button",
                "facebook",
                "facebook",
                "twitter",
                "send",
                "threads",
                "whatsapp",
                PARAGRAPH_B,
            ],
            PARAGRAPH_A + "\n" + PARAGRAPH_B,
        ),
        (
            [
                "Submit a story",
                "Tell us your story.",
                "Featured",
                "### Peppa Pig gets Lionesses call-up as Football Association scores "
                "licensing deal with Hasbro",
                PARAGRAPH_A,
                "Submit a story",
                "Tell us your story.",
                "Featured",
                "### Elsinore: meet Ian Charleson's real Hamlet director Richard Eyre",
                PARAGRAPH_B,
            ],
            PARAGRAPH_A + "\n" + PARAGRAPH_B,
        ),
    ],
    ids=["barchart", "interior-share-buttons", "city-am-sidebar"],
)
def test_708_01_worked_examples(lines: list[str], expected: str) -> None:
    assert strip_residue("\n".join(lines), load_intel_deepen_config()) == expected


@pytest.mark.parametrize(
    "paragraph",
    [
        "Featured speakers at the conference said demand for optical components would "
        "remain strong through next year.",
        "Investors can share the report with clients after the company files its "
        "quarterly statement next week.",
        "Your free trial of the new analytics service ends on Friday, the company said "
        "in a statement to customers.",
    ],
    ids=["featured-prose", "share-prose", "trial-prose"],
)
def test_708_02_prose_preserved(paragraph: str) -> None:
    assert strip_residue(paragraph, load_intel_deepen_config()) == paragraph
