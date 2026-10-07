"""Issue #653 retained duplicate parsing and empty-EXISTING regressions."""

from datetime import datetime
from unittest.mock import patch

import httpx
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.services import headline_cleaning as hc
from app.services import instrument_news_capture as capture
from app.services.instrument_news_sources import CollectedItem
from app.services.instrument_universe import UniverseEntry
from app.tests.test_issue_630_classifier import response
from app.tests.test_issue_635_deepening import worker as shared_worker  # noqa: F401

NOW = datetime(2026, 10, 4, 7, 30, tzinfo=ET)


def item(title: str, i: int = 0) -> CollectedItem:
    return CollectedItem(title, NOW, f"https://fixture.example/{i}")


def collect(session: Session, ticker: str, items: list[CollectedItem]) -> capture.InstrumentResult:
    with patch.object(capture, "sources_for", return_value=[("yahoo", lambda: items)]):
        return capture.collect_instrument_news(
            session, UniverseEntry(ticker, ticker, "US"), NOW, hc.load_cleaning_config()
        )


def test_653_a4_self_reference_is_not_duplicate() -> None:
    recent = [f"Stored headline {i}" for i in range(10)]
    titles = ["AAA a", "AAA b", "AAA c", "AAA d"]
    for duplicate_of, expected in (("e3", "keep"), ("e2", "duplicate")):
        rows = [{"id": 3, "label": "keep", "duplicate_of": duplicate_of}]
        with patch.object(httpx, "post", return_value=response(rows)):
            labels, _, _ = hc.classify_headlines(
                [item(t, i) for i, t in enumerate(titles)],
                "AAA",
                ["AAA"],
                recent_titles=recent,
            )
        assert labels == {3: expected}


def test_653_a5_empty_existing_is_omitted(db_session: Session) -> None:
    with patch.object(
        capture, "classify_headlines", return_value=({0: "keep"}, 0, None)
    ) as classifier:
        collect(db_session, "AAA", [item("AAA opens factory in Ohio", 0)])
        assert classifier.call_args.kwargs["recent_titles"] is None
        collect(db_session, "AAA", [item("AAA names new chief financial officer", 1)])
        assert classifier.call_args.kwargs["recent_titles"] == ["AAA opens factory in Ohio"]
