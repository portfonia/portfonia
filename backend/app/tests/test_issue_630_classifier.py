"""Issue #630 classifier parsing and low-value headline acceptance."""

import json
from unittest.mock import patch

import httpx

from app.services import headline_cleaning as hc
from app.services.instrument_news_sources import CollectedItem
from app.tests.test_intel_deepen_rules import NOW

STORED = "Broadcom to provide Anthropic with up to $42 billion in financing, Reuters reports"
TITLES = [
    "Anthropic's IPO Filing Reveals a $42 Billion Broadcom Lending Deal",
    "Broadcom raises $60 billion in debt to fund Anthropic AI chips",
    "Broadcom Starts Amassing $60 Billion to Fund Chips for Anthropic",
]
DUPLICATE_INSTRUCTION = 'Some items may repeat an event already covered. EXISTING lists earlier headlines for this company. For each item, set "duplicate_of" to the id of an EXISTING headline ("e0", "e1", ...) or of a lower-numbered item in this batch that reports the same event with no new material fact (no new figure, party, or stage). Otherwise set it to null. A follow-up with new facts is not a duplicate.'
PROMO = "promo = stock-pick, buy/sell, comparison, prediction or listicle content; institutional holding-change notices (a fund bought, sold or changed its stake); routine price-move recaps with no stated company-specific cause;"


def items(titles: list[str] = TITLES) -> list[CollectedItem]:
    return [
        CollectedItem(title, NOW, f"https://fixture.example/{i}") for i, title in enumerate(titles)
    ]


def response(labels: list[dict[str, object]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": json.dumps({"labels": labels})}}],
            "usage": {"cost": 0.001},
        },
        request=httpx.Request("POST", "https://fixture.example"),
    )


def test_630_12_prompt_collection_and_search() -> None:
    lead = items()[0]
    lead.summary = "Public summary. " * 20
    with patch.object(httpx, "post", return_value=response([])) as post:
        hc.classify_headlines([lead], "AVGO", ["Broadcom"], recent_titles=[STORED])
    payload = post.call_args.kwargs["json"]
    assert DUPLICATE_INSTRUCTION in payload["messages"][0]["content"]
    assert (
        payload["messages"][1]["content"]
        == f"EXISTING:\ne0\t{STORED}\n0\tAVGO (Broadcom)\t{lead.title}\t{lead.summary[:160]}"
    )
    assert '"duplicate_of"' in payload["messages"][0]["content"]
    with patch.object(httpx, "post", return_value=response([])) as post:
        hc.classify_headlines([lead], "AVGO", ["Broadcom"])
    payload = post.call_args.kwargs["json"]
    assert "EXISTING" not in str(payload["messages"])
    assert "duplicate_of" not in str(payload["messages"])
    assert payload["messages"][1]["content"].endswith(lead.summary[:160])


def test_630_15_invalid_duplicate_references() -> None:
    rows = [
        {"id": 0, "label": "mention", "duplicate_of": "e9"},
        {"id": 1, "label": "keep", "duplicate_of": -1},
        {"id": 2, "label": "keep", "duplicate_of": 5},
    ]
    with patch.object(httpx, "post", return_value=response(rows)):
        labels, cost, error = hc.classify_headlines(
            items(), "AVGO", ["Broadcom"], recent_titles=[STORED, "Second", "Third"]
        )
    assert labels == {0: "mention", 1: "keep", 2: "keep"}
    assert cost == 0.001 and error is None


LOW_VALUE = [
    "Polaris Financial Partners Purchases 3,065 Shares of Broadcom Inc. $AVGO",
    "North Fund Buys 100 Shares of Amazon",
    "Lake Fund Acquires 101 Shares of Broadcom",
    "West Fund Sells 102 Shares of Amazon",
    "South Fund Sold 103 Shares of Broadcom",
    "East Fund Acquired 104 Shares of Amazon",
    "Ridge Fund Purchased 105 Shares of Broadcom",
    "Amazon Stake Increased By Pine Fund",
    "Broadcom Position Decreased By Cedar Fund",
    "Amazon Holdings Raised By River Fund",
    "Broadcom Stake Trimmed By Valley Fund",
    "Forest Fund Grows Its Stake In Amazon",
    "Meadow Fund Raises Position In Broadcom",
    "Stone Fund Cuts Holdings In Amazon",
    "$AVGO Stock Acquired By Hill Fund",
    "$AMZN Shares Sold By Bay Fund",
    "$AVGO Position Purchased By Plain Fund",
    "Broadcom (NASDAQ:AVGO) Stock Price Up 3.3% - Here's What Happened",
    "Amazon Stock Down 1.2% - Here's Why",
    "Broadcom Shares Up 2.1% - Here's What Happened",
    "Amazon Shares Price Down 0.7% - Here's Why",
    "Broadcom Stock Up 1.8% - Here's What Happened",
    "Amazon Stock Price Up 0.9% - Here's Why",
    "Broadcom Stock Down 2.5% - Here's Why",
    "Amazon Outperforms Its Competitors In Daily Trading",
    "Broadcom Underperforms Competitors In Tuesday Session",
    "Amazon Stock Pre-Market At $220",
    "Broadcom Stock After-Hours At $300",
    "Amazon Stock After-Hours At $221",
]


def test_630_18_low_value_titles() -> None:
    assert len(LOW_VALUE) == 29
    config = hc.load_cleaning_config()
    for title in LOW_VALUE:
        lead = items([title])[0]
        assert (
            hc.block_reason(lead, ["Broadcom", "AVGO", "Amazon", "AMZN"], [], config)
            == "low_value_rule"
        ), title
    for title in [
        "Broadcom Stocks Jump 3.5% as AI Chip Guide Reaches $21.7 Billion",
        "Amazon seeks to offload about $8 billion of Nvidia chips",
        "B. Riley Raises Price Target on Lam Research to $390 From $350, Keeps Buy Rating",
    ]:
        assert (
            hc.block_reason(items([title])[0], ["Broadcom", "Amazon", "Lam Research"], [], config)
            is None
        )


def test_630_19_prompt_promo_definition() -> None:
    assert PROMO in hc.SYSTEM_PROMPT
