"""Dedicated headline-resolution acceptance tests; all paid HTTP is mocked."""

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

import httpx
import pytest
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.intel import InstrumentProfile
from app.services import intel_deepen as deepen
from app.services.instrument_news_sources import CollectedItem
from app.services.instrument_profiles import match_instruments
from app.services.intel_deepen_config import load_intel_deepen_config
from app.services.intel_leads import Lead
from app.services.intel_selection import WorkUnit
from app.services.paid_search import PaidResult, ParallelClient
from app.tests.test_intel_deepen_rules import NOW
from app.tests.test_intel_paid import slot


@pytest.fixture
def worker(db_session: Session) -> Iterator[deepen.DeepenRun]:
    db_session.add(InstrumentProfile(identifier="AAA", market="US", aliases=["AAA"]))
    db_session.flush()
    settings = get_settings().model_copy(update={"PARALLEL_API_KEY": None, "INTEL_PAID_WORKERS": 1})
    with (
        patch.object(deepen, "get_settings", return_value=settings),
        patch.object(
            deepen,
            "SessionLocal",
            side_effect=lambda: Session(
                bind=db_session.connection(), join_transaction_mode="create_savepoint"
            ),
        ),
    ):
        result = deepen.DeepenRun(
            db_session,
            slot(db_session),
            load_intel_deepen_config(),
            False,
            NOW,
            NOW - timedelta(days=2),
            [],
            {},
        )
        try:
            yield result
        finally:
            result.close()


def headline(title: str, days: int = 0, label: str = "keep") -> CollectedItem:
    item = CollectedItem(
        title,
        NOW - timedelta(days=days),
        "https://news.google.com/" + title,
        url_kind="google_news",
    )
    item.label = label
    return item


def test_635_01_direct_extract_without_search(worker: deepen.DeepenRun) -> None:
    item = CollectedItem("AAA agreement", NOW, "https://fixture.example/a")
    with patch.object(
        worker, "_call", return_value=("tavily", PaidResult(200, Decimal(0), Decimal(0)))
    ) as call:
        worker.run_wave(
            [WorkUnit("mover", "AAA", providers=("tavily",))], {"AAA": [item]}, {"AAA": ["AAA"]}
        )
    assert [c.args[1] for c in call.call_args_list] == ["extract"]


def test_635_02_two_kept_headlines_title_queries_and_dates(worker: deepen.DeepenRun) -> None:
    rows = [
        headline("AAA mention newest", label="mention"),
        headline("AAA keep older"),
        headline("AAA keep oldest", 1),
    ]
    rows[1].published_at = NOW - timedelta(hours=1)
    with patch(
        "app.services.paid_search.post", return_value=httpx.Response(200, json={"results": []})
    ) as post:
        worker.run_wave(
            [WorkUnit("quiet", "AAA", providers=("tavily",))], {"AAA": rows}, {"AAA": ["AAA"]}
        )
    payloads = [c.kwargs["json"] for c in post.call_args_list]
    assert [p["query"] for p in payloads] == ["AAA keep older", "AAA keep oldest"]
    assert [(p["start_date"], p["end_date"]) for p in payloads] == [
        ("2026-10-01", "2026-10-02"),
        ("2026-09-30", "2026-10-02"),
    ]


def test_635_03_mover_without_news_has_no_paid_call(worker: deepen.DeepenRun) -> None:
    with patch.object(worker, "_call", return_value=None) as call:
        worker.run_wave([WorkUnit("mover", "AAA", providers=("tavily",))], {}, {"AAA": ["AAA"]})
    assert call.call_count == 0
    assert worker.details()["outcomes"] == [
        {
            "kind": "mover",
            "identifier": "AAA",
            "theme": "",
            "reason": "",
            "provider": None,
            "via": "none",
            "searches": 0,
            "accepted": 0,
            "rejected": {},
            "note": "no_news",
        }
    ]


def test_635_04_date_first_survivor_and_unrelated(worker: deepen.DeepenRun) -> None:
    results = [
        [
            Lead("https://fixture.example/old", "AAA old", NOW - timedelta(days=2)),
            Lead("https://fixture.example/first", "AAA first", NOW),
            Lead("https://fixture.example/second", "AAA second", NOW),
        ],
        [Lead("https://fixture.example/other", "Other company", NOW)],
    ]
    with (
        patch(
            "app.services.paid_search.TavilyClient.search",
            side_effect=[PaidResult(200, Decimal(1), Decimal(0), leads=leads) for leads in results],
        ),
        patch.object(deepen, "classify_headlines", return_value=({0: "keep", 1: "keep"}, 0, None)),
        patch.object(worker, "_extract_batch") as extract,
    ):
        worker.run_wave(
            [WorkUnit("quiet", "AAA", providers=("tavily",))],
            {"AAA": [headline("AAA deal"), headline("AAA factory")]},
            {"AAA": ["AAA"]},
        )
    assert [lead.url for _, lead in extract.call_args.args[1]] == ["https://fixture.example/first"]
    assert worker.metrics["tavily"]["search_filtered"]["unrelated_rule"] == 1
    assert (
        worker.metrics["tavily"]["headlines_resolved"]
        == worker.metrics["tavily"]["headlines_unresolved"]
        == 1
    )


def test_635_05_cap_stops_mid_unit(worker: deepen.DeepenRun) -> None:
    worker.search_cap = 1
    with patch(
        "app.services.paid_search.post", return_value=httpx.Response(200, json={"results": []})
    ) as post:
        worker.run_wave(
            [WorkUnit("quiet", "AAA", providers=("tavily",))],
            {"AAA": [headline("AAA deal"), headline("AAA factory")]},
            {"AAA": ["AAA"]},
        )
    assert post.call_count == 1
    outcomes = worker.details()["outcomes"]
    assert isinstance(outcomes, list) and outcomes[0]["note"] == "cap_reached"


def test_635_06_parallel_date_and_full_content_payloads() -> None:
    client = ParallelClient(get_settings(), 3)
    with patch.object(client, "_call", return_value=Mock(leads=[])) as call:
        client.search("AAA deal", NOW.date() - timedelta(days=1), NOW.date())
        client.extract(["https://fixture.example/a"], "AAA")
    search = call.call_args_list[0].args[1]
    assert search["advanced_settings"] == {
        "source_policy": {"after_date": "2026-10-01"},
        "max_results": 3,
    }
    assert "max_results" not in search
    assert call.call_args_list[1].args[1]["full_content"] is True


def test_635_08_english_boundary_unchanged() -> None:
    assert match_instruments("AMDX rallies", {"AMD": ["AMD"]}) == set()


def test_635_07_real_cjk_name_followed_by_digit() -> None:
    title = "長城汽車9月新車銷量近11.5萬輛　按年跌近一成四"
    assert match_instruments(title, {"2333.HK": ["長城汽車"]}) == {"2333.HK"}


def test_635_extract_fallback_retains_headline_outcome(worker: deepen.DeepenRun) -> None:
    unit = WorkUnit("quiet", "AAA", providers=("tavily",))
    lead = Lead("https://fixture.example/article", "AAA agreement", NOW)
    worker._outcome(unit, "tavily", "search")
    with (
        patch.object(worker, "_provider", side_effect=lambda wanted: wanted),
        patch.object(worker.usage, "extract_size", side_effect=lambda provider, count: count),
        patch.object(
            worker,
            "_call",
            side_effect=[
                ("tavily", PaidResult(401, Decimal(0), Decimal(0), "invalid_key")),
                (
                    "parallel",
                    PaidResult(
                        200,
                        Decimal(1),
                        Decimal(".001"),
                        bodies={
                            lead.url: "The company announced an agreement to build a new factory and expand manufacturing capacity. "
                            * 10
                        },
                    ),
                ),
            ],
        ),
    ):
        worker._extract_batch("tavily", [(unit, lead)])
    accepted = [outcome for outcome in worker.outcomes if outcome["accepted"]]
    assert [(outcome["provider"], outcome["via"]) for outcome in accepted] == [
        ("parallel", "search")
    ]


def test_638_search_fallback_preserves_provider_attribution(
    worker: deepen.DeepenRun, db_session: Session
) -> None:
    from sqlalchemy import select

    from app.models.paid_intel import IntelArticle, PaidApiUsage

    worker.usage.configured["parallel"] = True
    a = Lead("https://fixture.example/a", "AAA agreement", NOW)
    b = Lead("https://fixture.example/b", "AAA factory", NOW)
    body = (
        "The company announced an agreement to build a new factory and expand manufacturing capacity. "
        * 10
    )
    with (
        patch(
            "app.services.paid_search.TavilyClient.search",
            side_effect=[
                PaidResult(200, Decimal(1), Decimal(".008"), leads=[a]),
                PaidResult(429, Decimal(0), Decimal(0), "quota_or_rate"),
            ],
        ),
        patch(
            "app.services.paid_search.ParallelClient.search",
            return_value=PaidResult(200, Decimal(1), Decimal(".005"), leads=[b]),
        ),
        patch("app.services.paid_search.TavilyClient.extract") as tavily_extract,
        patch(
            "app.services.paid_search.ParallelClient.extract",
            side_effect=lambda urls, query: PaidResult(
                200,
                Decimal(len(urls)),
                Decimal(".001") * len(urls),
                bodies={url: body for url in urls},
            ),
        ) as parallel_extract,
        patch.object(deepen, "classify_headlines", return_value=({0: "keep"}, 0, None)),
        patch("app.services.paid_usage.send_ops_alert", return_value=True),
        patch.object(worker, "_extract_batch", wraps=worker._extract_batch) as batches,
    ):
        worker.run_wave(
            [WorkUnit("quiet", "AAA", providers=("tavily",))],
            {"AAA": [headline("AAA agreement"), headline("AAA factory")]},
            {"AAA": ["AAA"]},
        )
    outcomes = {entry["provider"]: entry for entry in worker.outcomes}
    assert {provider: entry["searches"] for provider, entry in outcomes.items()} == {
        "tavily": 2,
        "parallel": 1,
    }
    assert len(worker.outcomes) == 2
    assert outcomes["tavily"]["accepted"] == 0
    assert outcomes["parallel"]["accepted"] == 2
    assert worker.metrics["tavily"]["headlines_resolved"] == 1
    assert worker.metrics["parallel"]["headlines_resolved"] == 1
    assert [
        (call.args[0], [lead.url for _, lead in call.args[1]]) for call in batches.call_args_list
    ] == [("tavily", [a.url]), ("parallel", [b.url])]
    assert "tavily" in worker.usage.disabled
    tavily_extract.assert_not_called()
    assert sorted(url for call in parallel_extract.call_args_list for url in call.args[0]) == [
        a.url,
        b.url,
    ]
    ledger = db_session.scalars(select(PaidApiUsage)).all()
    assert [(row.provider, row.cost_usd) for row in ledger if row.operation == "extract"] == [
        ("parallel", Decimal(".001")),
        ("parallel", Decimal(".001")),
    ]
    assert worker.metrics["tavily"]["cost_usd"] == 0.008
    assert worker.metrics["parallel"]["cost_usd"] == 0.007
    assert [row.status for row in db_session.scalars(select(IntelArticle)).all()] == [
        "accepted",
        "accepted",
    ]


def test_638_unavailable_fallback_creates_no_outcome(worker: deepen.DeepenRun) -> None:
    with (
        patch(
            "app.services.paid_search.TavilyClient.search",
            return_value=PaidResult(429, Decimal(0), Decimal(0), "quota_or_rate"),
        ),
        patch("app.services.paid_search.ParallelClient.search") as parallel_search,
        patch("app.services.paid_usage.send_ops_alert", return_value=True),
    ):
        worker.run_wave(
            [WorkUnit("quiet", "AAA", providers=("tavily",))],
            {"AAA": [headline("AAA agreement")]},
            {"AAA": ["AAA"]},
        )
    parallel_search.assert_not_called()
    assert [(o["provider"], o["searches"], o["note"]) for o in worker.outcomes] == [
        ("tavily", 1, "cap_reached")
    ]
    assert worker.metrics["tavily"]["headlines_unresolved"] == 1
    assert worker.metrics["parallel"]["headlines_unresolved"] == 0
