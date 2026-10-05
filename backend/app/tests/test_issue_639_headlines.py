"""Issue #639 stale-headline acceptance; every external call is mocked."""

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import httpx
import pandas as pd
import pytest
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.intel import InstrumentProfile
from app.services import headline_cleaning as hc
from app.services import instrument_news_capture as capture
from app.services import intel_deepen as deepen
from app.services.instrument_news_sources import CollectedItem
from app.services.instrument_universe import UniverseEntry
from app.services.intel_leads import Lead
from app.services.intel_selection import WorkUnit
from app.services.paid_search import PaidResult
from app.tests.test_intel_paid import slot
from app.tests.test_issue_630_classifier import response
from app.tests.test_issue_635_deepening import worker as shared_worker  # noqa: F401
from app.tests.test_issue_635_report import collection, report


@pytest.fixture
def worker(request: pytest.FixtureRequest) -> deepen.DeepenRun:
    from typing import cast

    return cast(deepen.DeepenRun, request.getfixturevalue("shared_worker"))


NOW = datetime(2026, 10, 3, 19, tzinfo=ET)
TITLES = [
    "QCOM Stock Drops Nearly 8% After Hours — Qualcomm's Q3 EPS Misses, Weak Q4 Outlook…",
    "Why Is Intel (INTC) Stock Volatile After Earnings? Revenue Beat Overshadowed by $11 Billion Loss",
]


def earnings(day: datetime) -> pd.DataFrame:
    return pd.DataFrame(index=pd.DatetimeIndex([day]))


@pytest.mark.parametrize("ticker,title,day", [("QCOM", TITLES[0], 2), ("INTC", TITLES[1], 3)])
@pytest.mark.parametrize("age,expected", [(70, "stale_rule"), (3, "kept")])
def test_639_01_real_earnings_titles(
    db_session: Session, ticker: str, title: str, day: int, age: int, expected: str
) -> None:
    published = NOW.replace(day=day)
    item = CollectedItem(title, published, "https://fixture.example/recap")
    db_session.add(
        InstrumentProfile(identifier=ticker, market="US", name_en=ticker, aliases=[ticker])
    )
    db_session.flush()
    with (
        patch.object(capture, "sources_for", return_value=[("yahoo", lambda: [item])]),
        patch(
            "yfinance.Ticker.get_earnings_dates",
            return_value=earnings(published - timedelta(days=age)),
        ),
        patch.object(
            capture, "classify_headlines", return_value=({0: "keep"}, 0, None)
        ) as classifier,
    ):
        result = capture.collect_instrument_news(
            db_session, UniverseEntry(ticker, ticker, "US"), NOW, hc.load_cleaning_config()
        )
    assert result.cleaning.get(expected) == 1
    if age == 70:
        classifier.assert_not_called()
        assert result.leads == []
    else:
        assert [lead.title for lead in result.leads] == [title]


def test_639_02_lookup_only_matching_cached_and_fail_open(db_session: Session) -> None:
    titles = [
        "AAA opens factory",
        "AAA Q3 earnings improve",
        "AAA after earnings appoints director",
    ]
    candidates = [
        CollectedItem(t, NOW, f"https://fixture.example/{i}") for i, t in enumerate(titles)
    ]
    with (
        patch.object(capture, "sources_for", return_value=[("yahoo", lambda: candidates)]),
        patch(
            "yfinance.Ticker.get_earnings_dates", side_effect=RuntimeError("fixture failure")
        ) as lookup,
        patch.object(
            capture, "classify_headlines", return_value=({0: "keep", 1: "keep", 2: "keep"}, 0, None)
        ),
    ):
        result = capture.collect_instrument_news(
            db_session, UniverseEntry("AAA", "AAA", "US"), NOW, hc.load_cleaning_config()
        )
    assert lookup.call_count == 1
    assert result.cleaning["stale_lookup_failed"] == 2
    assert len(result.leads) == 3
    with (
        patch.object(capture, "sources_for", return_value=[("yahoo", lambda: [candidates[0]])]),
        patch("yfinance.Ticker.get_earnings_dates") as lookup,
        patch.object(capture, "classify_headlines", return_value=({}, 0, None)),
    ):
        capture.collect_instrument_news(
            db_session, UniverseEntry("BBB", "BBB", "US"), NOW, hc.load_cleaning_config()
        )
    lookup.assert_not_called()


def test_639_03_classifier_date_and_stale_label() -> None:
    # #653: the model no longer returns "stale"; "stale" comes only from a recap whose
    # earnings date is old.
    with patch.object(
        httpx, "post", return_value=response([{"id": 0, "label": "keep", "recap": True}])
    ) as post:
        labels, _, _ = hc.classify_headlines(
            [CollectedItem("AAA old merger recap", NOW, "https://fixture.example/a")],
            "AAA",
            ["AAA"],
            batch_date=NOW.date(),
            recap_is_stale=lambda item: True,
        )
    assert labels == {0: "stale"}
    prompt = post.call_args.kwargs["json"]["messages"][0]["content"]
    assert "2026-10-03" in prompt and "recap =" in prompt and "stale =" not in prompt


def test_639_03_collection_drops_stale_llm(db_session: Session) -> None:
    item = CollectedItem("AAA old merger recap", NOW, "https://fixture.example/a")
    with (
        patch.object(capture, "sources_for", return_value=[("yahoo", lambda: [item])]),
        patch.object(
            capture, "classify_headlines", return_value=({0: "stale"}, 0, None)
        ) as classifier,
    ):
        result = capture.collect_instrument_news(
            db_session, UniverseEntry("AAA", "AAA", "US"), NOW, hc.load_cleaning_config()
        )
    assert result.cleaning.get("stale_llm") == 1
    assert result.leads == []
    assert classifier.call_args.kwargs["batch_date"] == NOW.date()


def test_639_03_paid_search_drops_stale_llm(worker: deepen.DeepenRun) -> None:
    lead = Lead("https://fixture.example/a", "AAA old merger recap", worker.now)
    with (
        patch.object(
            worker,
            "_call",
            return_value=("tavily", PaidResult(200, Decimal(1), Decimal(0), leads=[lead])),
        ),
        patch.object(
            deepen, "classify_headlines", return_value=({0: "stale"}, 0, None)
        ) as classifier,
    ):
        _, leads = worker._search_headline(
            "tavily",
            WorkUnit("quiet", "AAA"),
            CollectedItem("AAA merger", worker.now, "https://fixture.example/h"),
            set(),
        )
    assert worker.metrics["tavily"]["search_filtered"].get("stale_llm") == 1
    assert leads == []
    assert classifier.call_args.kwargs["batch_date"] == worker.now.astimezone(ET).date()


def test_639_04_digest_merges_stale_counts_and_samples(db_session: Session) -> None:
    run = slot(db_session)
    collection(
        db_session,
        run,
        {
            "cleaning": {"stale_rule": 2, "stale_llm": 3},
            "cleaning_samples": {
                "stale_rule": ["Old one", "Old two"],
                "stale_llm": ["Old one", "Old three", "Old four"],
            },
        },
    )
    run.details = {
        "deepening": {
            "metrics": {"tavily": {"search_filtered": {"stale_rule": 1, "stale_llm": 1}}},
            "search_samples": {"stale_rule": ["Old paid"]},
        }
    }
    _, body, _ = report(db_session, run)
    all_lines = body.splitlines()
    index = next(
        i for i, line in enumerate(all_lines) if "Old news republished with a new date" in line
    )
    assert all_lines[index] == "  Old news republished with a new date ..... 7"
    # Issue #670: samples sit on the indented line below the count.
    examples = all_lines[index + 1]
    assert examples.startswith("      e.g. ")
    assert examples.count('"Old one"') == 1
    assert '"Old paid"' in examples and '"Old four"' not in examples
    assert examples.count('"') == 6


def test_639_12a_paid_missing_date_uses_batch_time(worker: deepen.DeepenRun) -> None:
    lead = Lead("https://fixture.example/a", "AAA Q3 earnings improve", None)
    with (
        patch.object(
            worker,
            "_call",
            return_value=("tavily", PaidResult(200, Decimal(1), Decimal(0), leads=[lead])),
        ),
        patch(
            "yfinance.Ticker.get_earnings_dates",
            return_value=earnings(worker.now - timedelta(days=20)),
        ) as lookup,
        patch.object(
            deepen, "classify_headlines", return_value=({0: "keep"}, 0, None)
        ) as classifier,
    ):
        _, leads = worker._search_headline(
            "tavily",
            WorkUnit("quiet", "AAA"),
            CollectedItem("AAA outlook", worker.now, "https://fixture.example/h"),
            set(),
        )
    assert worker.metrics["tavily"]["search_filtered"].get("stale_rule") == 1
    assert leads == []
    assert lookup.call_count == 1
    classifier.assert_not_called()
