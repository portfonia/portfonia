"""Issue #639 bounded late ingestion and surfaced-ledger retention acceptance."""

import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.news import News
from app.models.news_surfaced import NewsSurfaced
from app.services import instrument_news_capture as capture
from app.services.headline_cleaning import load_cleaning_config
from app.services.instrument_universe import UniverseEntry
from app.services.intel_records import link_instrument, store_headline, sweep_intel
from app.services.window_data import load_instrument_news_by_identifier, load_news_window
from app.tests.conftest import seed_user
from app.tests.test_issue_639_headlines import NOW
from app.tests.test_report_generator import _news_item

USER = uuid.UUID(int=639)


def test_639_07a_both_readers_bound_late_ingestion(db_session: Session) -> None:
    seed_user(db_session, USER)
    start = datetime(2026, 10, 5, 17, tzinfo=ET)
    end = start + timedelta(hours=2)
    for age in (49, 48, 47, -3):
        for origin in ("pool", "instrument"):
            item = replace(_news_item(f"{origin} {age}"), published_at=start - timedelta(hours=age))
            nid, _ = store_headline(db_session, item, origin, "article", "keep")
            if origin == "instrument":
                link_instrument(db_session, nid, "AAA")
    assert [n.title for n in load_news_window(db_session, start, end, USER)] == ["pool 47"]
    assert [
        n.title
        for n in load_instrument_news_by_identifier(db_session, start, end, USER, ["AAA"])["AAA"]
    ] == ["instrument 47"]
    from app.services.news_fetcher import LATE_INGEST_WINDOW

    assert timedelta(hours=48) == LATE_INGEST_WINDOW
    with patch.object(capture, "sources_for", return_value=[]) as sources:
        capture.collect_instrument_news(
            db_session, UniverseEntry("AAA", "AAA", "US"), NOW, load_cleaning_config()
        )
    assert sources.call_args.args[2] == NOW - LATE_INGEST_WINDOW


def test_639_08_sweep_deletes_only_expired_news_and_ledger(db_session: Session) -> None:
    seed_user(db_session, USER)
    ids = []
    for age in (31, 5):
        item = replace(_news_item(f"Retention {age}"), published_at=NOW - timedelta(days=age))
        nid, _ = store_headline(db_session, item, "pool", "article", "keep")
        ids.append(nid)
        db_session.add(NewsSurfaced(user_id=USER, news_id=nid, report_id=uuid.uuid4()))
    db_session.flush()
    counts = sweep_intel(db_session, NOW)
    assert counts["news_deleted"] == 1
    assert counts.get("news_surfaced_deleted") == 1
    assert list(db_session.scalars(select(News.id))) == [ids[1]]
    assert list(db_session.scalars(select(NewsSurfaced.news_id))) == [ids[1]]
