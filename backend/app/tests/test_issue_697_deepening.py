"""Issue #697 acceptance: dated search leads, body residue and search budget."""

from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.paid_intel import IntelArticle
from app.services import intel_deepen as deepen
from app.services.headline_cleaning import EarningsCache, load_cleaning_config
from app.services.instrument_news_sources import CollectedItem
from app.services.intel_body import body_verdict, clean_body
from app.services.intel_deepen_config import load_intel_deepen_config
from app.services.intel_digest import build_batch_report
from app.services.intel_leads import Lead
from app.services.intel_selection import WorkUnit
from app.services.paid_search import PaidResult
from app.tests.test_intel_paid import slot
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
        patch.object(EarningsCache, "_dates", return_value=[]),
    ):
        yield


@pytest.mark.parametrize("provider", ["tavily", "parallel"])
def test_697_01_undated_search_not_extracted_dated_extracted(
    worker: deepen.DeepenRun, provider: str
) -> None:
    leads = [
        Lead("https://fixture.example/undated", "AAA optical orders expand", None),
        Lead("https://fixture.example/dated", "AAA optical orders expand", worker.now),
    ]
    with (
        patch.object(worker, "_provider", return_value=provider),
        patch.object(
            worker,
            "_call",
            return_value=(provider, PaidResult(200, Decimal(1), Decimal(0), leads=leads)),
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
    assert worker.metrics[provider]["search_filtered"] == {"undated": 1}


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


def test_697_06_digest_counts_undated_as_old_news(db_session: Session) -> None:
    run = slot(db_session)
    collection(db_session, run, {"cleaning": {"stale_rule": 2, "stale_llm": 1}})
    run.details = {
        "deepening": {
            "metrics": {
                "tavily": {"search_filtered": {"undated": 1, "stale_rule": 2}},
                "parallel": {"search_filtered": {"undated": 3, "stale_llm": 1}},
            }
        }
    }
    body = build_batch_report(db_session, run)[1]
    assert "Old news republished with a new date ..... 10" in body


def test_697_07_earnings_loom_preview_and_real_recap() -> None:
    published = datetime(2026, 10, 6, 8, tzinfo=ET)
    cache, cfg = EarningsCache(), load_cleaning_config()
    item = CollectedItem("Samsung, SK Hynix shares drop as Q3 earnings loom", published, "")
    last = published.date() - timedelta(days=30)
    with patch.object(cache, "_dates", return_value=[last, published.date() + timedelta(days=16)]):
        assert cache.stale_reason(item, "SKHY", cfg) is None
    with patch.object(cache, "_dates", return_value=[last, published.date() + timedelta(days=60)]):
        assert cache.stale_reason(item, "SKHY", cfg) == "stale_rule"
    recap = CollectedItem("Q3 earnings beat estimates", published, "")
    with patch.object(cache, "_dates", return_value=[last, published.date() + timedelta(days=16)]):
        assert cache.stale_reason(recap, "SKHY", cfg) == "stale_rule"


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
