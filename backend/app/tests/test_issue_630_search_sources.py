"""Issue #630 search-title and Google News summary acceptance."""

from decimal import Decimal
from unittest.mock import patch

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.intel import InstrumentProfile
from app.models.news import News
from app.services import instrument_news_sources as src
from app.services import intel_deepen as deepen
from app.services import news_capture as nc
from app.services import news_fetcher as nf
from app.services.intel_deepen_config import load_intel_deepen_config
from app.services.intel_leads import Lead
from app.services.intel_selection import WorkUnit
from app.services.paid_search import PaidResult
from app.tests.test_intel_deepen_rules import NOW
from app.tests.test_intel_deepen_run import resolve_fixture
from app.tests.test_intel_paid import slot


def test_630_10_search_titles_unchanged(db_session: Session) -> None:
    aliases = ["Lumentum", "LITE", "AAOI"]
    db_session.add(InstrumentProfile(identifier="LITE", market="US", aliases=aliases))
    db_session.flush()
    cases = [
        "Lumentum Holdings (LITE) Jumped, But What Is Driving Attention Today? - Simply Wall St News",
        "AAOI Stock Rallies As Hyperscale AI Orders Boost Outlook - StocksToTrade",
        "Lumentum officer Wajid Ali proposes $2.57M share sale | LITE SEC Filing - Form 144",
        "Lumentum financing agreement - What investors should know",
        "Lumentum expands capacity | AI demand",
    ]
    worker = deepen.DeepenRun(
        db_session, slot(db_session), load_intel_deepen_config(), False, NOW, NOW, [], {}
    )
    try:
        for raw in cases:
            lead = Lead("https://fixture.example/article", raw, NOW)
            with (
                patch.object(worker, "_provider", return_value="tavily"),
                patch.object(
                    worker,
                    "_call",
                    return_value=(
                        "tavily",
                        PaidResult(200, Decimal(1), Decimal(".008"), leads=[lead]),
                    ),
                ),
                patch.object(
                    deepen, "classify_headlines", return_value=({0: "keep"}, 0, None)
                ) as classifier,
            ):
                provider, kept = resolve_fixture(worker, "tavily", WorkUnit("quiet", "LITE"))
            assert provider == "tavily" and len(kept) == 1
            assert kept[0].title == raw
            assert kept[0].url == lead.url and kept[0].published_at == lead.published_at
            assert lead.title == raw
            assert classifier.call_args.args[0][0].title == raw
            assert "recent_titles" not in classifier.call_args.kwargs
    finally:
        worker.close()


def xml(title: str, link: str) -> str:
    return f'<rss version="2.0"><channel><item><title>{title}</title><link>{link}</link><description><![CDATA[<p>Public company summary</p>]]></description><pubDate>{NOW.strftime("%a, %d %b %Y %H:%M:%S %z")}</pubDate><source>Publisher</source></item></channel></rss>'


def test_630_20_instrument_google_summary() -> None:
    with patch.object(
        src,
        "request",
        return_value=httpx.Response(
            200, text=xml("Broadcom financing - Publisher", "https://fixture.example/instrument")
        ),
    ):
        instrument = src.fetch_google_news("Broadcom", "en-US", NOW, NOW)
    assert len(instrument) == 1 and instrument[0].summary is None
    assert instrument[0].title == "Broadcom financing"


def test_630_20_pool_google_summary(db_session: Session) -> None:
    feeds = []
    for feed, title, link in [
        (
            "https://news.google.com/rss",
            "Broadcom reports earnings - Publisher",
            "https://fixture.example/google",
        ),
        (
            "https://fixture.example/rss",
            "Amazon signs agreement",
            "https://news.google.com/article/other-feed",
        ),
    ]:
        with patch.object(
            httpx.Client,
            "get",
            return_value=httpx.Response(
                200, text=xml(title, link), request=httpx.Request("GET", feed)
            ),
        ):
            feeds.extend(nf._fetch_feed("Fixture", feed, NOW))
    with patch.object(nc, "fetch_news", return_value=nf.FetchNewsResult(feeds, [])):
        result = nc.capture_news(db_session)
    assert result.inserted == 2
    records = {row.record["title"]: row.record for row in db_session.scalars(select(News))}
    assert records["Broadcom reports earnings"]["summary"] is None
    assert records["Amazon signs agreement"]["summary"] == "Public company summary"
