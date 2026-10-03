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
    "Amazon.com (NASDAQ:AMZN) Stock Price Up 1.3% - Here's What Happened",
    "Nellore Capital Management LLC Acquires 10,000 Shares of Amazon.com, Inc. $AMZN",
    "Amazon.com, Inc. $AMZN Shares Sold by Coppell Advisory Solutions LLC",
    "Amazon.com, Inc. $AMZN Stock Sold by B. Metzler seel. Sohn & Co. AG",
    "Amazon.com, Inc. $AMZN Shares Sold by Retirement Income Solutions Inc.",
    "PYA Waltman Capital LLC Boosts Stake in Amazon.com, Inc. $AMZN",
    "4,695 Amazon.com, Inc. $AMZN Shares Acquired by Ballast Inc.",
    "3,167 Amazon.com, Inc. $AMZN Shares Sold by Resolute Advisors LLC",
    "4,546 Amazon.com, Inc. $AMZN Shares Sold by SkyOak Wealth LLC",
    "Amazon.com, Inc. $AMZN Stock Bought by Secure Asset Management LLC",
    "5,269 Amazon.com, Inc. $AMZN Shares Acquired by Symphony Financial Ltd. Co.",
    "Amazon.com, Inc. $AMZN Stock Sold by Natural Investments LLC",
    "Nan Shan Life Insurance Co. Ltd. Increases Stake in Amazon.com, Inc. $AMZN",
    "Amazon.com, Inc. $AMZN Stock Bought by Lone Peak Global Investors LLC",
    "Amazon.com, Inc. $AMZN Stock Sold by Ironvine Capital Partners LLC",
    "Independence Bank of Kentucky Boosts Holdings in Amazon.com, Inc. $AMZN",
    "Broadcom Inc. $AVGO Stock Acquired by CX Institutional",
    "Polaris Financial Partners Purchases 3,065 Shares of Broadcom Inc. $AVGO",
    "Broadcom (NASDAQ:AVGO) Stock Price Up 3.3% - Here's What Happened",
    "Broadcom Inc. stock outperforms competitors on strong trading day",
    "Broadcom Inc. $AVGO Position Increased by GoalVest Advisory LLC",
    "Broadcom stock after-hours at EUR 315.33: plus 2.75 percent versus prior close",
    "Global Wealth Strategies & Associates Grows Stake in Broadcom Inc. $AVGO",
    "First Financial Bank Trust Division Buys 8,788 Shares of Broadcom Inc. $AVGO",
    "Broadcom (NASDAQ:AVGO) Shares Down 2.1% - Here's What Happened",
    "Broadcom Inc. stock underperforms Thursday when compared to competitors",
    "Broadcom Inc. $AVGO Stock Sold by Coastline Trust Co",
    "Broadcom stock pre-market at EUR 314.43: plus 1.22 percent",
    "Broadcom Inc. stock underperforms Wednesday when compared to competitors",
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
