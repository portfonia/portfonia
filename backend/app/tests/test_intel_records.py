"""Issue #620 acceptance: records, universe and report compatibility."""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.holding import Holding
from app.models.intel import NewsInstrument
from app.models.news import News
from app.services.instrument_profiles import match_instruments
from app.services.instrument_universe import intel_universe
from app.services.intel_records import (
    build_headline_record,
    link_instrument,
    store_headline,
    sweep_intel,
)
from app.services.news_fetcher import NewsItem, url_hash
from app.services.report_prompts import _build_macro_signal_themes_block
from app.services.report_serializers import _serialize_news
from app.services.window_data import load_day_news, load_news_window
from app.tests.conftest import TEST_USER_ID, seed_user

NOW = datetime(2026, 10, 2, 16, 15, tzinfo=ET)


def item(title: str = "Nvidia earnings", url: str = "https://fixture.example/story") -> NewsItem:
    return NewsItem(url_hash(url), title, url, "FixturePublisher", NOW, "<b>Board</b>  approves")


def test_acceptance_01_universe(db_session: Session) -> None:
    seed_user(db_session, TEST_USER_ID)
    for ticker, asset, mode, market in [
        ("NVDA", "stock", "auto", "US"),
        ("QQQM", "etf", "auto", "US"),
        ("600519.SS", "stock", "auto", "A-Share"),
        ("INTC", "stock", "manual", "US"),
    ]:
        db_session.add(
            Holding(
                user_id=TEST_USER_ID,
                name="private",
                ticker=ticker,
                asset_type=asset,
                pricing_mode=mode,
                market=market,
                currency="USD",
            )
        )
    second_user = uuid.uuid4()
    seed_user(db_session, second_user)
    for other_ticker, other_asset, fund_code in [
        ("NVDA", "stock", None),
        (None, "fund", "000001"),
        (None, "cash", None),
        (None, "wmf", None),
        (None, "other", None),
    ]:
        db_session.add(
            Holding(
                user_id=second_user,
                name="private",
                ticker=other_ticker,
                fund_code=fund_code,
                asset_type=other_asset,
                pricing_mode="auto",
                market="US",
                currency="USD",
            )
        )
    db_session.flush()
    assert [x.identifier for x in intel_universe(db_session, "US")] == ["NVDA"]
    assert [x.identifier for x in intel_universe(db_session, "A-Share")] == ["600519.SS"]


def test_acceptance_03_pool_only_loaders(db_session: Session) -> None:
    seed_user(db_session, TEST_USER_ID)
    store_headline(db_session, item(), "pool", "article", None)
    store_headline(
        db_session,
        item("Instrument only", "https://fixture.example/two"),
        "instrument",
        "article",
        "keep",
    )
    assert [
        x.title for x in load_news_window(db_session, NOW - timedelta(days=1), NOW, TEST_USER_ID)
    ] == ["Nvidia earnings"]
    loaded = load_day_news(db_session, NOW.date())
    assert len(loaded) == 1 and loaded[0].url == loaded[0].source == ""


def test_acceptance_07_conflict_links_existing_pool(db_session: Session) -> None:
    first, inserted = store_headline(db_session, item(), "pool", "article", None)
    assert inserted
    assert store_headline(db_session, item(), "instrument", "article", "keep") == (first, False)
    assert store_headline(db_session, item(), "pool", "article", None) == (first, False)
    assert link_instrument(db_session, first, "NVDA") == 1
    assert link_instrument(db_session, first, "NVDA") == 0
    assert len(db_session.scalars(select(News)).all()) == 1
    assert len(db_session.scalars(select(NewsInstrument)).all()) == 1


def test_acceptance_08_alias_boundaries() -> None:
    aliases = {"NVDA": ["Nvidia", "NVDA"]}
    assert match_instruments("Nvidia rises", aliases) == {"NVDA"}
    assert not match_instruments("NVDAX fund launch", aliases)
    assert not match_instruments("nvda rises", aliases)
    assert match_instruments("新贵州茅台公告", {"CN": ["贵州茅台"]}) == {"CN"}
    assert not match_instruments("X贵州茅台2", {"CN": ["贵州茅台"]})


def test_standard_record_is_clean_and_closed() -> None:
    record = build_headline_record(item(), "article", "keep", collected_at=NOW)
    assert set(record) == {
        "v",
        "kind",
        "title",
        "summary",
        "published_at",
        "collected_at",
        "label",
        "filing_form",
    }
    assert record["summary"] == "Board approves"
    assert "https://" not in str(record) and "FixturePublisher" not in str(record)


def test_acceptance_27_retention_cascades(db_session: Session) -> None:
    for age in [31, 29]:
        i = item(str(age), f"https://fixture.example/{age}")
        i = NewsItem(i.url_hash, i.title, i.url, i.source, NOW - timedelta(days=age), None)
        link_instrument(
            db_session, store_headline(db_session, i, "pool", "article", None)[0], "NVDA"
        )
    sweep_intel(db_session, NOW)
    assert len(db_session.scalars(select(News)).all()) == 1
    assert len(db_session.scalars(select(NewsInstrument)).all()) == 1


def test_acceptance_29_prompt_without_source() -> None:
    assert "  Nvidia earnings" in _build_macro_signal_themes_block(
        {
            "has_any_hit": True,
            "hits": [{"theme": "chips", "top_articles": [{"title": "Nvidia earnings"}]}],
        }
    )


def test_acceptance_32_serialization_preserves_hash() -> None:
    data = _serialize_news([item()])[0]
    assert data["url_hash"] == item().url_hash
    assert "url" not in data and "source" not in data


def test_acceptance_07_instrument_first_promoted_by_rss(db_session: Session) -> None:
    from unittest.mock import patch

    from app.services import news_capture as nc
    from app.services.news_fetcher import FetchNewsResult

    seed_user(db_session, TEST_USER_ID)
    nid, _ = store_headline(db_session, item(), "instrument", "article", "mention")
    link_instrument(db_session, nid, "NVDA")
    original = dict(db_session.get(News, nid).record)  # type: ignore[union-attr]
    with patch.object(
        nc, "fetch_news", return_value=FetchNewsResult([item("RSS replacement")], [])
    ):
        result = nc.capture_news(db_session)
    db_session.expire_all()
    row = db_session.get(News, nid)
    assert row is not None and row.origin == "pool"
    assert row.record == original and row.intel_label == "mention"
    assert result.inserted == 0
    assert len(db_session.scalars(select(News)).all()) == 1
    assert db_session.scalars(select(NewsInstrument)).one().news_id == nid
    assert [
        x.title for x in load_news_window(db_session, NOW - timedelta(days=1), NOW, TEST_USER_ID)
    ] == ["Nvidia earnings"]
    assert len(load_day_news(db_session, NOW.date())) == 1
