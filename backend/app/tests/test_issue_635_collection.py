"""Dedicated instrument-only reconciliation and same-batch RSS acceptance."""

from datetime import timedelta
from unittest.mock import patch

from sqlalchemy.orm import Session

from app.models.intel import InstrumentProfile, NewsInstrument
from app.services import headline_cleaning as cleaning
from app.services import instrument_news_capture as capture
from app.services.instrument_news_sources import CollectedItem
from app.services.instrument_universe import UniverseEntry
from app.services.intel_records import link_instrument, store_headline
from app.tests.test_intel_deepen_rules import NOW

ENTRY = UniverseEntry("AAA", "AAA", "US")


def profile(session: Session) -> None:
    session.add(InstrumentProfile(identifier="AAA", market="US", name_en="AAA", aliases=["AAA"]))
    session.flush()


def stored(session: Session, title: str, earlier: bool) -> None:
    nid, _ = store_headline(
        session,
        CollectedItem(title, NOW, "https://fixture.example/stored/" + str(earlier)).headline(),
        "pool",
        "article",
        None,
    )
    link_instrument(session, nid, "AAA")
    link = session.query(NewsInstrument).filter_by(news_id=nid, identifier="AAA").one()
    link.created_at = NOW + timedelta(seconds=-1 if earlier else 1)
    session.flush()


def test_635_09_every_fate_counts_once_and_reconciles(db_session: Session) -> None:
    profile(db_session)
    stored(db_session, "AAA factory construction permit approved", True)
    titles = [
        "AAA stale",
        "AAA video",
        "Other company merger",
        "3 AAA Stocks to Buy",
        "AAA factory construction permit approved",
        "AAA satellite launch successful",
        "AAA satellite launch successful",
        "AAA supplier contract secured",
        "AAA board appoints director",
        "AAA submarine engine developed",
    ]
    # Only the first satellite item is kept; the last three are classifier drops.
    rows = [
        CollectedItem(title, NOW - timedelta(minutes=20 - i), f"https://fixture.example/{i}")
        for i, title in enumerate(titles)
    ]
    rows[0].published_at = NOW - timedelta(hours=49)
    rows[1].url = "https://fixture.example/watch/video"
    labels_by_title = {
        titles[5]: "keep",
        titles[7]: "promo",
        titles[8]: "unrelated",
        titles[9]: "duplicate",
    }

    def classify(
        items: list[CollectedItem], *args: object, **kwargs: object
    ) -> tuple[dict[int, str], float, None]:
        return {i: labels_by_title[item.title] for i, item in enumerate(items)}, 0, None

    with (
        patch.object(capture, "sources_for", return_value=[("yahoo", lambda: rows)]),
        patch.object(capture, "classify_headlines", side_effect=classify),
    ):
        result = capture.collect_instrument_news(
            db_session, ENTRY, NOW, cleaning.load_cleaning_config()
        )
    counters = [
        "out_of_window",
        "non_article",
        "unrelated_rule",
        "low_value_rule",
        "duplicate_earlier",
        "duplicate",
        "promo_llm",
        "unrelated_llm",
        "duplicate_llm",
        "kept",
    ]
    assert {key: result.cleaning.get(key, 0) for key in counters} == dict.fromkeys(counters, 1)
    assert result.stats["yahoo"]["items"] == sum(result.cleaning[key] for key in counters) == 10
    assert len(result.leads) == 1 and result.leads[0].label == "keep"


def test_635_09a_current_batch_rss_is_duplicate_not_earlier(db_session: Session) -> None:
    profile(db_session)
    title = "AAA factory construction permit approved"
    stored(db_session, title, False)
    historical = "AAA semiconductor supply agreement signed"
    stored(db_session, historical, True)
    with (
        patch.object(
            capture,
            "sources_for",
            return_value=[
                (
                    "yahoo",
                    lambda: [
                        CollectedItem(title, NOW, "https://fixture.example/fetched"),
                        CollectedItem(historical, NOW, "https://fixture.example/history-repeat"),
                    ],
                )
            ],
        ),
        patch.object(capture, "classify_headlines") as classifier,
    ):
        result = capture.collect_instrument_news(
            db_session, ENTRY, NOW, cleaning.load_cleaning_config()
        )
    assert result.cleaning == {"duplicate": 1, "duplicate_earlier": 1}
    assert not result.leads
    classifier.assert_not_called()
