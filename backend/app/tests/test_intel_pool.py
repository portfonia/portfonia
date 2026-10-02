"""Pool cleaning and feed error observability acceptance."""

import logging
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.news import News
from app.services import news_capture as nc
from app.services import news_fetcher as nf
from app.tests.test_intel_records import NOW, item


def test_acceptance_22_pool_cleaning(db_session: Session) -> None:
    with patch.object(
        nc,
        "fetch_news",
        return_value=nf.FetchNewsResult(
            [
                item("Nvidia video", "https://fixture.example/watch/a"),
                item("3 Dividend Stocks to Buy", "https://fixture.example/b"),
                item(),
            ],
            [],
        ),
    ):
        result = nc.capture_news(db_session)
    assert result.inserted == 1
    assert len(result.items) == 1
    assert db_session.scalars(select(News)).one().intel_label is None


def test_acceptance_31_feed_failure_log(caplog: pytest.LogCaptureFixture) -> None:
    logging.getLogger(nf.__name__).disabled = False
    resp = httpx.Response(503, request=httpx.Request("GET", "https://secret.example/feed"))
    stat = nf.FeedStat("FixtureFeed")
    with patch.object(httpx.Client, "get", return_value=resp), caplog.at_level(logging.WARNING):
        assert nf._fetch_feed("FixtureFeed", "https://secret.example/feed", NOW, stat) == []
    assert stat.errors == 1
    assert "FixtureFeed" in caplog.text and "503" in caplog.text
    assert "https://" not in caplog.text


def test_transport_logs_do_not_leak_urls(caplog: pytest.LogCaptureFixture) -> None:
    from app.services.instrument_news_sources import request

    original = httpx.Client

    def client(*args: object, **kwargs: object) -> httpx.Client:
        return original(
            timeout=12, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
        )

    logging.getLogger("httpx").disabled = False
    with (
        patch("app.services.instrument_news_sources.httpx.Client", side_effect=client),
        caplog.at_level(logging.INFO, logger="httpx"),
    ):
        request("https://query1.finance.yahoo.com/v1/finance/search", params={"q": "NVDA"})
    assert "https://" not in caplog.text


def test_acceptance_30_pool_google_suffix() -> None:
    xml = f"""<rss version="2.0"><channel><item><title>Nvidia beats estimates - Reuters</title><link>https://fixture.example/one</link><pubDate>{NOW.strftime("%a, %d %b %Y %H:%M:%S %z")}</pubDate><source>Reuters</source></item></channel></rss>"""
    with patch.object(
        httpx.Client,
        "get",
        return_value=httpx.Response(
            200, text=xml, request=httpx.Request("GET", "https://news.google.com/rss")
        ),
    ):
        rows = nf._fetch_feed("Reuters", "https://news.google.com/rss", NOW)
    assert rows[0].title == "Nvidia beats estimates"


def test_transport_filter_only_on_url_bearing_httpx() -> None:
    from app.services.intel_http import _TransportFilter, quiet_transport

    assert not any(isinstance(f, _TransportFilter) for f in logging.getLogger("httpcore").filters)
    httpx_logger = logging.getLogger("httpx")
    record = logging.LogRecord("httpx", logging.INFO, "", 0, "URL", (), None)
    assert httpx_logger.filter(record)
    with quiet_transport():
        assert not httpx_logger.filter(record)
    assert httpx_logger.filter(record)
