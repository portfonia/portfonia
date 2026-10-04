"""Issue #653 recap-based stale detection, classifier parsing and report lines.

Every external call (yfinance, OpenRouter, paid search) is mocked.
"""

import importlib.util
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import httpx
import pandas as pd
import pytest
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.intel import IntelSlotRun
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

NOW = datetime(2026, 10, 4, 7, 30, tzinfo=ET)
RECAP_SENTENCE = (
    "recap = true if the item reports or reacts to THIS company's own periodic financial "
    "results (quarterly or annual revenue, EPS, profit, margins, or guidance issued with "
    "results); false otherwise, including operating data such as deliveries, production or "
    "sales volumes, and previews of results not yet released."
)


@pytest.fixture
def worker(request: pytest.FixtureRequest) -> deepen.DeepenRun:
    from typing import cast

    return cast(deepen.DeepenRun, request.getfixturevalue("shared_worker"))


def earnings(day: datetime) -> pd.DataFrame:
    return pd.DataFrame(index=pd.DatetimeIndex([day]))


def item(title: str, i: int = 0) -> CollectedItem:
    return CollectedItem(title, NOW, f"https://fixture.example/{i}")


def collect(session: Session, ticker: str, items: list[CollectedItem]) -> capture.InstrumentResult:
    with patch.object(capture, "sources_for", return_value=[("yahoo", lambda: items)]):
        return capture.collect_instrument_news(
            session, UniverseEntry(ticker, ticker, "US"), NOW, hc.load_cleaning_config()
        )


def test_653_a1_lxml_installed() -> None:
    assert importlib.util.find_spec("lxml") is not None


def test_653_a2_recap_skips_pattern_gate() -> None:
    config = hc.load_cleaning_config()
    recap = item("AAA Beat on Revenue, Profit, and Guidance")
    with patch(
        "yfinance.Ticker.get_earnings_dates", return_value=earnings(NOW - timedelta(days=60))
    ):
        assert hc.EarningsCache().stale_reason(recap, "AAA", config, recap=True) == "stale_rule"
    with patch(
        "yfinance.Ticker.get_earnings_dates", return_value=earnings(NOW - timedelta(days=2))
    ):
        assert hc.EarningsCache().stale_reason(recap, "AAA", config, recap=True) is None
    with patch("yfinance.Ticker.get_earnings_dates") as lookup:
        assert hc.EarningsCache().stale_reason(recap, "AAA", config) is None
    lookup.assert_not_called()


def test_653_a3_recap_label_and_model_stale_rejected() -> None:
    rows = [
        {"id": 0, "label": "keep", "recap": True},
        {"id": 1, "label": "keep", "recap": False},
        {"id": 2, "label": "promo", "recap": True},
        {"id": 3, "label": "stale", "recap": False},
    ]
    seen: list[str] = []

    def always_stale(candidate: CollectedItem) -> bool:
        seen.append(candidate.title)
        return True

    titles = ["AAA a", "AAA b", "AAA c", "AAA d"]
    with patch.object(httpx, "post", return_value=response(rows)):
        labels, _, error = hc.classify_headlines(
            [item(t, i) for i, t in enumerate(titles)],
            "AAA",
            ["AAA"],
            recap_is_stale=always_stale,
        )
    assert labels == {0: "stale", 1: "keep", 2: "promo"}
    assert seen == ["AAA a"]
    assert error is None


def test_653_a4_self_reference_is_not_duplicate() -> None:
    recent = [f"Stored headline {i}" for i in range(10)]
    titles = ["AAA a", "AAA b", "AAA c", "AAA d"]
    for duplicate_of, expected in (("e3", "keep"), ("e2", "duplicate")):
        rows = [{"id": 3, "label": "keep", "recap": False, "duplicate_of": duplicate_of}]
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


def test_653_a6_prompt_recap_without_age_judgement() -> None:
    with patch.object(httpx, "post", return_value=response([])) as post:
        hc.classify_headlines([item("AAA a")], "AAA", ["AAA"], recent_titles=["x"])
    prompt = post.call_args.kwargs["json"]["messages"][0]["content"]
    assert RECAP_SENTENCE in prompt
    assert "stale =" not in prompt and "more than 7 days" not in prompt
    assert '"recap": true|false, "duplicate_of": "e<k>" | int | null}' in prompt
    with patch.object(httpx, "post", return_value=response([])) as post:
        hc.classify_headlines([item("AAA a")], "AAA", ["AAA"])
    prompt = post.call_args.kwargs["json"]["messages"][0]["content"]
    assert (
        'Output ONLY JSON: {"labels": [{"id": int, "label": "keep|mention|promo|unrelated", '
        '"recap": true|false}]}'
    ) in prompt
    assert "duplicate_of" not in prompt


def test_653_a7_collection_recap_old_dropped_fresh_kept(db_session: Session) -> None:
    items = [item("AAA Beat on Revenue, Profit, and Guidance", 0), item("AAA signs supply deal", 1)]
    rows = [{"id": 0, "label": "keep", "recap": True}, {"id": 1, "label": "keep", "recap": False}]
    with (
        patch.object(capture, "classify_headlines", hc.classify_headlines),
        patch.object(httpx, "post", return_value=response(rows)),
        patch(
            "yfinance.Ticker.get_earnings_dates", return_value=earnings(NOW - timedelta(days=60))
        ),
    ):
        result = collect(db_session, "AAA", items)
    assert result.cleaning.get("stale_llm") == 1
    assert result.samples["stale_llm"] == ["AAA Beat on Revenue, Profit, and Guidance"]
    assert [lead.title for lead in result.leads] == ["AAA signs supply deal"]

    fresh = [item("BBB Just Reported And Analysts Are Boosting Their Estimates", 2)]
    with (
        patch.object(capture, "classify_headlines", hc.classify_headlines),
        patch.object(
            httpx, "post", return_value=response([{"id": 0, "label": "keep", "recap": True}])
        ),
        patch("yfinance.Ticker.get_earnings_dates", return_value=earnings(NOW - timedelta(days=2))),
    ):
        result = collect(db_session, "BBB", fresh)
    assert result.cleaning.get("stale_llm") is None
    assert [lead.title for lead in result.leads] == [fresh[0].title]


def test_653_a7_lookup_failure_counted_once_per_headline(db_session: Session) -> None:
    items = [item("CCC Q3 earnings improve", 0), item("CCC beat on revenue and profit", 1)]
    rows = [{"id": 0, "label": "keep", "recap": True}, {"id": 1, "label": "keep", "recap": True}]
    with (
        patch.object(capture, "classify_headlines", hc.classify_headlines),
        patch.object(httpx, "post", return_value=response(rows)),
        patch("yfinance.Ticker.get_earnings_dates", side_effect=RuntimeError("fixture")),
    ):
        result = collect(db_session, "CCC", items)
    assert result.cleaning["stale_lookup_failed"] == 2
    assert len(result.leads) == 2


@pytest.mark.parametrize("counter", ["stale_llm", "stale_lookup_failed"])
def test_653_a8_paid_search_recap(worker: deepen.DeepenRun, counter: str) -> None:
    lookup = (
        patch("yfinance.Ticker.get_earnings_dates", return_value=earnings(NOW - timedelta(days=60)))
        if counter == "stale_llm"
        else patch("yfinance.Ticker.get_earnings_dates", side_effect=RuntimeError("fixture"))
    )
    lead = Lead("https://fixture.example/a", "AAA beat on revenue and profit", worker.now)
    with (
        patch.object(
            worker,
            "_call",
            return_value=("tavily", PaidResult(200, Decimal(1), Decimal(0), leads=[lead])),
        ),
        patch.object(deepen, "classify_headlines", hc.classify_headlines),
        patch.object(
            httpx, "post", return_value=response([{"id": 0, "label": "keep", "recap": True}])
        ),
        lookup,
    ):
        _, leads = worker._search_headline(
            "tavily",
            WorkUnit("quiet", "AAA"),
            CollectedItem("AAA results", worker.now, "https://fixture.example/h"),
            set(),
        )
    assert worker.metrics["tavily"]["search_filtered"].get(counter) == 1
    assert leads == ([] if counter == "stale_llm" else [lead])


def test_653_a8_paid_regex_recap_lookup_failure_counted_once(worker: deepen.DeepenRun) -> None:
    lead = Lead("https://fixture.example/a", "AAA Q3 earnings improve", worker.now)
    with (
        patch.object(
            worker,
            "_call",
            return_value=("tavily", PaidResult(200, Decimal(1), Decimal(0), leads=[lead])),
        ),
        patch.object(deepen, "classify_headlines", hc.classify_headlines),
        patch.object(
            httpx, "post", return_value=response([{"id": 0, "label": "keep", "recap": True}])
        ),
        patch("yfinance.Ticker.get_earnings_dates", side_effect=RuntimeError("fixture")),
    ):
        _, leads = worker._search_headline(
            "tavily",
            WorkUnit("quiet", "AAA"),
            CollectedItem("AAA results", worker.now, "https://fixture.example/h"),
            set(),
        )
    assert worker.metrics["tavily"]["search_filtered"].get("stale_lookup_failed") == 1
    assert leads == [lead]


def test_653_a9_report_lookup_failures_and_row_label(db_session: Session) -> None:
    run = slot(db_session)
    collection(db_session, run, {"cleaning": {"stale_lookup_failed": 3, "stale_rule": 1}})
    run.details = {
        "deepening": {"metrics": {"tavily": {"search_filtered": {"stale_lookup_failed": 2}}}}
    }
    _, body, _ = report(db_session, run)
    assert "Earnings-date check failed for 5 earnings-recap headlines; they were kept." in body
    row = next(line for line in body.splitlines() if "Old news republished" in line)
    assert row.startswith("  Old news republished with a new date ..... 1")
    assert len("Old news republished with a new date .....") == 42

    run2 = IntelSlotRun(
        slot="pre_open", run_date=run.run_date, started_at=run.started_at, status="ok", details={}
    )
    db_session.add(run2)
    db_session.flush()
    collection(db_session, run2, {"cleaning": {"stale_rule": 1}})
    _, body, _ = report(db_session, run2)
    assert "Earnings-date check failed" not in body
