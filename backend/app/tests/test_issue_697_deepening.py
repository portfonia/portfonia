"""Issue #697 acceptance: dated search leads, body residue and search budget."""

from collections.abc import Iterator
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.intel import InstrumentProfile
from app.models.paid_intel import IntelArticle
from app.services import headline_cleaning as hc
from app.services import instrument_news_capture as capture
from app.services import intel_deepen as deepen
from app.services.instrument_news_sources import CollectedItem
from app.services.instrument_universe import UniverseEntry
from app.services.intel_body import body_verdict, clean_body
from app.services.intel_deepen_config import load_intel_deepen_config
from app.services.intel_digest import build_batch_report
from app.services.intel_leads import Lead
from app.services.intel_selection import WorkUnit
from app.services.paid_search import PaidResult
from app.tests.test_intel_paid import slot
from app.tests.test_issue_630_classifier import response
from app.tests.test_issue_635_deepening import headline
from app.tests.test_issue_635_deepening import worker as worker
from app.tests.test_issue_635_report import collection

# URL-free shapes reconstructed from the 2026-10-07 07:30 bodies cited in #697.
TEASER = (
    "# GLW shares rise as optical demand grows\n"
    "This headline only article is a sample of real-time intelligence available through "
    "Benzinga Pro, providing breaking news and market updates for active traders. "
    "Subscribers can follow the latest headlines throughout the trading day and receive "
    "alerts about companies, earnings announcements, analyst ratings and other market "
    "developments covered by the service."
)
PRESS_RELEASE = (
    "This is a paid press release. Contact the press release distributor directly with any inquiries.\n"
    "# Correction to Tencent student programme announcement\n"
    "The company issued a correction to an earlier announcement about its student "
    "programme at Tencent, clarifying the programme details and the participating institutions. "
    "The updated announcement contains information about the application process and "
    "the opportunities available to students taking part in the programme this year."
)
TABLE = (
    "# Lumentum news\n"
    "| Date | Headline | Source |\n"
    "| --- | --- | --- |\n"
    "| Sep. 21 | Lumentum expands optical manufacturing capacity to meet demand from "
    "global data center customers | MarketScreener |\n"
    "| Sep. 22 | Lumentum announces new optical products for cloud infrastructure "
    "customers as orders continue to grow | MarketScreener |"
)
PARAGRAPH = (
    "The Reserve Bank of India raised borrowing costs as policymakers addressed "
    "inflation pressures and assessed the outlook for economic growth this year."
)
MENU = (
    "Markets & News Breaking Headlines Stock Market News Earnings Calendar Analyst "
    "Ratings. Benzinga Pro Options Trading News and Market Updates"
)


@pytest.fixture(autouse=True)
def no_external_calls() -> Iterator[None]:
    with (
        patch("httpx.Client.send", side_effect=AssertionError("HTTP must be mocked")),
        patch("httpx.AsyncClient.send", side_effect=AssertionError("HTTP must be mocked")),
        patch("yfinance.Ticker", side_effect=AssertionError("earnings lookup removed")),
    ):
        yield


@pytest.mark.parametrize(
    ("provider", "fallback"),
    [("tavily", False), ("parallel", False), ("tavily", True)],
    ids=["tavily", "parallel", "tavily-429-parallel"],
)
def test_697_01_undated_search_not_extracted_dated_extracted(
    worker: deepen.DeepenRun, provider: str, fallback: bool
) -> None:
    leads = [
        Lead("https://fixture.example/undated", "AAA optical orders expand", None),
        Lead("https://fixture.example/dated", "AAA optical orders expand", worker.now),
    ]
    owner = "parallel" if fallback else provider
    results = [(owner, PaidResult(200, Decimal(1), Decimal(0), leads=leads))]
    if fallback:
        results.insert(0, (provider, PaidResult(429, Decimal(1), Decimal(0), "quota_or_rate")))
    with (
        patch.object(worker, "_provider", side_effect=lambda wanted: wanted),
        patch.object(
            worker,
            "_call",
            side_effect=results,
        ),
        patch.object(deepen, "classify_headlines", return_value=({0: "keep", 1: "keep"}, 0, None)),
        patch.object(worker, "_extract_batch") as extract,
    ):
        worker.run_wave(
            [WorkUnit("mover", "AAA", providers=(provider,))],
            {"AAA": [headline("AAA optical orders")]},
            worker.aliases,
        )
    assert [lead.url for _, lead in extract.call_args.args[1]] == [leads[1].url]
    assert worker.metrics[owner]["search_filtered"] == {"undated": 1}
    assert extract.call_args.args[0] == owner
    if fallback:
        assert worker.metrics[provider]["search_filtered"].get("undated", 0) == 0


@pytest.mark.parametrize("body", [TEASER, PRESS_RELEASE], ids=["benzinga-teaser", "paid-release"])
def test_697_02_teaser_and_paid_release_are_paywall(
    worker: deepen.DeepenRun, db_session: Session, body: str
) -> None:
    worker.aliases["AAA"] = ["GLW", "Tencent"]
    lead = Lead("https://fixture.example/article", "GLW / Tencent announcement", worker.now)
    with patch.object(
        worker,
        "_call",
        return_value=("tavily", PaidResult(200, Decimal(1), Decimal(0), bodies={lead.url: body})),
    ):
        worker._extract_batch("tavily", [(WorkUnit("quiet", "AAA"), lead)])
    article = db_session.scalars(select(IntelArticle)).one()
    assert (article.status, article.reject_reason) == ("rejected", "paywall")
    assert body_verdict(body, worker.cfg) == (False, "paywall")


def test_697_03_table_has_no_paragraph() -> None:
    assert clean_body("https://fixture.example/lite", TABLE, load_intel_deepen_config()) == ""


@pytest.mark.parametrize("prefix", ["FILE.", "_FILE."], ids=["plain", "markdown"])
def test_697_04_caption_and_social_lines_removed(prefix: str) -> None:
    body = (
        prefix
        + " A journalist walks past the central bank building as officials prepare to announce borrowing costs.\nFacebook\nTwitter\nLinkedIn\n"
        + PARAGRAPH
    )
    cleaned = clean_body("https://fixture.example/india", body, load_intel_deepen_config())
    assert cleaned.startswith(PARAGRAPH)
    assert not {"Facebook", "Twitter", "LinkedIn"} & set(cleaned.splitlines())


def test_697_05_benzinga_section_menu_removed() -> None:
    prose = "SPCX announced a new launch agreement that expands satellite capacity and supports additional commercial customers this year."
    cleaned = clean_body(
        "https://fixture.example/spcx", MENU + "\n" + prose, load_intel_deepen_config()
    )
    assert MENU not in cleaned
    assert cleaned == prose


def test_697_06_digest_counts_undated_without_earnings_rows(db_session: Session) -> None:
    run = slot(db_session)
    collection(
        db_session, run, {"cleaning": {"stale_rule": 2, "stale_llm": 1, "stale_lookup_failed": 3}}
    )
    run.details = {
        "deepening": {
            "metrics": {
                "tavily": {"search_filtered": {"undated": 1, "stale_rule": 2}},
                "parallel": {"search_filtered": {"undated": 3, "stale_llm": 1}},
            },
            "search_samples": {"stale_rule": ["Old earnings sample"]},
        }
    }
    body = build_batch_report(db_session, run)[1]
    assert "Search results with no publication date .. 4" in body
    assert "Old news" not in body
    assert "Earnings-date" not in body
    assert "Old earnings sample" not in body


EARNINGS_TITLES = [
    ("SKHY", "SK Hynix", "Samsung, SK Hynix shares drop as Q3 earnings loom"),
    (
        "AMKR",
        "Amkor",
        "Amkor Technology to Announce Third Quarter 2026 Financial Results on October 26, 2026",
    ),
]


@pytest.mark.parametrize("identifier,alias,title", EARNINGS_TITLES, ids=["skhy", "amkor"])
def test_697_07_collection_keeps_earnings_without_yfinance(
    db_session: Session, identifier: str, alias: str, title: str
) -> None:
    from app.tests.test_intel_deepen_rules import NOW

    item = CollectedItem(title, NOW, "https://fixture.example/earnings")
    db_session.add(
        InstrumentProfile(identifier=identifier, market="US", name_en=alias, aliases=[alias])
    )
    db_session.flush()
    with (
        patch("yfinance.Ticker", side_effect=AssertionError("earnings lookup removed")) as ticker,
        patch.object(capture, "sources_for", return_value=[("yahoo", lambda: [item])]),
        patch.object(capture, "classify_headlines", hc.classify_headlines),
        patch("httpx.post", return_value=response([{"id": 0, "label": "keep", "recap": True}])),
    ):
        result = capture.collect_instrument_news(
            db_session, UniverseEntry(identifier, identifier, "US"), NOW, hc.load_cleaning_config()
        )
    ticker.assert_not_called()
    assert result.leads == [item]
    assert item.label == "keep"
    assert result.cleaning.get("kept") == 1


@pytest.mark.parametrize("identifier,alias,title", EARNINGS_TITLES, ids=["skhy", "amkor"])
def test_697_07_paid_search_keeps_earnings_without_yfinance(
    worker: deepen.DeepenRun, identifier: str, alias: str, title: str
) -> None:
    worker.aliases[identifier] = [alias]
    lead = Lead("https://fixture.example/earnings", title, worker.now)
    with (
        patch("yfinance.Ticker", side_effect=AssertionError("earnings lookup removed")) as ticker,
        patch.object(
            worker,
            "_call",
            return_value=("tavily", PaidResult(200, Decimal(1), Decimal(0), leads=[lead])),
        ),
        patch.object(deepen, "classify_headlines", hc.classify_headlines),
        patch("httpx.post", return_value=response([{"id": 0, "label": "keep", "recap": True}])),
    ):
        provider, leads = worker._search_headline(
            "tavily", WorkUnit("quiet", identifier), CollectedItem(title, worker.now, ""), set()
        )
    ticker.assert_not_called()
    assert provider == "tavily" and leads == [lead]
    assert worker.metrics[provider]["search_filtered"] == {}
    assert "search_samples" not in worker.details()


def test_697_07_classifier_ignores_recap_and_omits_earnings_prompt() -> None:
    from app.tests.test_intel_deepen_rules import NOW

    item = CollectedItem(EARNINGS_TITLES[0][2], NOW, "")
    with patch(
        "httpx.post", return_value=response([{"id": 0, "label": "keep", "recap": True}])
    ) as post:
        labels, _, error = hc.classify_headlines(
            [item], "SKHY", ["SK Hynix"], recent_titles=["Earlier development"]
        )
    assert labels == {0: "keep"} and error is None
    prompt = post.call_args.kwargs["json"]["messages"][0]["content"]
    assert "recap =" not in prompt and '"recap":' not in prompt
    assert "Batch date (ET)" not in prompt
    assert '"label": "keep|mention|promo|unrelated", "duplicate_of": "e<k>" | int | null}' in prompt


@pytest.mark.parametrize("first_resolves", [True, False], ids=["resolved", "unresolved"])
def test_697_08_resolve_stops_only_after_success(
    worker: deepen.DeepenRun, first_resolves: bool
) -> None:
    lead = Lead("https://fixture.example/body", "AAA agreement", worker.now)
    results = [("tavily", [lead] if first_resolves else []), ("tavily", [lead])]
    unit = WorkUnit("quiet", "AAA")
    with patch.object(worker, "_search_headline", side_effect=results) as search:
        chosen, owned = worker._resolve(
            "tavily", unit, [headline("AAA deal"), headline("AAA factory")]
        )
    assert search.call_count == (1 if first_resolves else 2)
    assert chosen == "tavily" and owned == [("tavily", lead)]
    assert worker.metrics["tavily"]["headlines_resolved"] == 1
    assert worker.metrics["tavily"]["headlines_unresolved"] == (0 if first_resolves else 1)
    assert worker.outcomes[0]["note"] is None
