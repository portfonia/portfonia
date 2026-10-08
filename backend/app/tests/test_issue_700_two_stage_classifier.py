"""Issue #700: two-stage intel classification, retry once, fail open."""

import json
from collections.abc import Callable, Iterator
from datetime import timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.intel import InstrumentProfile, IntelCollectionRun
from app.models.news import News
from app.services import headline_cleaning as hc
from app.services import instrument_news_capture as cap
from app.services import intel_deepen as deepen
from app.services import news_capture as nc
from app.services.instrument_news_sources import CollectedItem
from app.services.instrument_relations import Relation
from app.services.intel_deepen_config import load_intel_deepen_config
from app.services.intel_digest import build_batch_report
from app.services.intel_leads import Lead
from app.services.intel_selection import WorkUnit
from app.services.news_fetcher import FetchNewsResult
from app.services.paid_search import PaidResult
from app.tests.test_instrument_news_capture import ENTRY as CAPTURE_ENTRY
from app.tests.test_instrument_news_capture import leads as capture_leads
from app.tests.test_instrument_news_capture import profile as capture_profile
from app.tests.test_intel_deepen_rules import NOW as DEEPEN_NOW
from app.tests.test_intel_deepen_run import resolve_fixture
from app.tests.test_intel_paid import slot as paid_slot
from app.tests.test_intel_records import NOW
from app.tests.test_intel_slots import run as digest_slot
from app.tests.test_issue_690_macro_pool import pool as macro_pool

SCREEN = "anthropic/claude-haiku-5.5"
REVIEW = "openai/gpt-6-luna"


@pytest.fixture(autouse=True)
def fixture_settings() -> Iterator[None]:
    with patch.object(
        hc,
        "get_settings",
        return_value=get_settings().model_copy(
            update={
                "OPENROUTER_API_KEY": SecretStr("fixture-only"),
                "INTEL_SCREEN_MODEL": SCREEN,
                "INTEL_CLASSIFIER_MODEL": REVIEW,
            }
        ),
    ):
        yield


def lead(i: int) -> CollectedItem:
    return CollectedItem(title=f"Nvidia item {i}", published_at=NOW, url=f"https://x.example/{i}")


def ok(content: str, cost: float = 0.001) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content}}], "usage": {"cost": cost}},
        request=httpx.Request("POST", "https://x.example"),
    )


def fail() -> httpx.Response:
    return httpx.Response(503, request=httpx.Request("POST", "https://x.example"))


def labels_json(rows: list[dict[str, Any]], fenced: bool = False) -> str:
    text = json.dumps({"labels": rows})
    return f"```json\n{text}\n```" if fenced else text


class Router:
    """Fake httpx.post answering per model from queued responses."""

    def __init__(self, answers: dict[str, list[httpx.Response]]) -> None:
        self.answers = answers
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> httpx.Response:
        body = kwargs["json"]
        self.calls.append(body)
        queue = self.answers[body["model"]]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def sent(self, model: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["model"] == model]


def run(router: Router, fn: Callable[[], Any]) -> Any:
    with patch.object(httpx, "post", side_effect=router):
        return fn()


def item_lines(body: dict[str, Any]) -> list[str]:
    content = str(body["messages"][1]["content"])
    return [line for line in content.splitlines() if line[:1].isdigit()]


def test_worked_example_strict_merge_and_reindexing() -> None:
    router = Router(
        {
            SCREEN: [
                ok(
                    labels_json(
                        [
                            {"id": 0, "label": "promo", "duplicate_of": None},
                            {"id": 1, "label": "keep", "duplicate_of": None},
                            {"id": 2, "label": "mention", "duplicate_of": None},
                            {"id": 3, "label": "keep", "duplicate_of": None},
                        ]
                    )
                )
            ],
            REVIEW: [
                ok(
                    labels_json(
                        [
                            {"id": 0, "label": "keep", "duplicate_of": None},
                            {"id": 1, "label": "mention", "duplicate_of": "e3"},
                            {"id": 2, "label": "mention", "duplicate_of": None},
                        ]
                    )
                )
            ],
        }
    )
    stats: dict[str, float] = {}
    labels, cost, error = run(
        router,
        lambda: hc.classify_headlines(
            [lead(i) for i in range(4)],
            "NVDA",
            ["Nvidia"],
            recent_titles=["a", "b", "c", "d"],
            stats=stats,
        ),
    )
    assert labels == {0: "promo", 1: "keep", 2: "duplicate", 3: "mention"}
    assert error is None and cost == pytest.approx(0.002)
    review = router.sent(REVIEW)
    assert len(review) == 1
    assert [line.split("\t")[0] for line in item_lines(review[0])] == ["0", "1", "2"]
    assert "Nvidia item 0" not in str(review[0]["messages"][1]["content"])
    assert "EXISTING" in str(review[0]["messages"][1]["content"])
    assert stats["screen_cost_usd"] == pytest.approx(0.001)
    assert stats["review_cost_usd"] == pytest.approx(0.001)
    assert stats.get("retries", 0) == 0


def test_fenced_screen_output_parses_without_retry() -> None:
    router = Router(
        {
            SCREEN: [ok(labels_json([{"id": 0, "label": "keep"}], fenced=True))],
            REVIEW: [ok(labels_json([{"id": 0, "label": "keep"}]))],
        }
    )
    stats: dict[str, float] = {}
    labels, _, error = run(
        router, lambda: hc.classify_headlines([lead(0)], "NVDA", ["Nvidia"], stats=stats)
    )
    assert labels == {0: "keep"} and error is None
    assert len(router.sent(SCREEN)) == 1 and stats.get("retries", 0) == 0


def test_review_fails_twice_screen_labels_stand() -> None:
    router = Router(
        {
            SCREEN: [ok(labels_json([{"id": 0, "label": "keep"}, {"id": 1, "label": "mention"}]))],
            REVIEW: [fail()],
        }
    )
    stats: dict[str, float] = {}
    labels, _, error = run(
        router,
        lambda: hc.classify_headlines([lead(0), lead(1)], "NVDA", ["Nvidia"], stats=stats),
    )
    assert labels == {0: "keep", 1: "mention"}
    assert error == "classifier: review HTTPStatusError HTTP 503 (fail-open)"
    assert len(router.sent(REVIEW)) == 2
    assert stats["review_failed"] == 1 and stats["retries"] == 1


def test_screen_fails_twice_review_sees_all_items() -> None:
    router = Router(
        {
            SCREEN: [fail()],
            REVIEW: [ok(labels_json([{"id": 0, "label": "promo"}, {"id": 1, "label": "keep"}]))],
        }
    )
    stats: dict[str, float] = {}
    labels, _, error = run(
        router,
        lambda: hc.classify_headlines([lead(0), lead(1)], "NVDA", ["Nvidia"], stats=stats),
    )
    assert labels == {0: "promo", 1: "keep"}
    assert error == "classifier: screen HTTPStatusError HTTP 503 (fail-open)"
    assert len(item_lines(router.sent(REVIEW)[0])) == 2
    assert stats["screen_failed"] == 1


def test_both_stages_fail_twice() -> None:
    router = Router({SCREEN: [fail()], REVIEW: [fail()]})
    labels, _, error = run(router, lambda: hc.classify_headlines([lead(0)], "NVDA", ["Nvidia"]))
    assert labels == {}
    assert error == (
        "classifier: screen HTTPStatusError HTTP 503; classifier: review HTTPStatusError HTTP 503"
    )
    assert len(router.calls) == 4


def test_one_failure_then_success_retries_once() -> None:
    router = Router(
        {
            SCREEN: [fail(), ok(labels_json([{"id": 0, "label": "keep"}]))],
            REVIEW: [ok(labels_json([{"id": 0, "label": "keep"}]))],
        }
    )
    stats: dict[str, float] = {}
    labels, _, error = run(
        router, lambda: hc.classify_headlines([lead(0)], "NVDA", ["Nvidia"], stats=stats)
    )
    assert labels == {0: "keep"} and error is None
    assert len(router.sent(SCREEN)) == 2 and stats["retries"] == 1


def test_screen_drops_everything_skips_review() -> None:
    router = Router({SCREEN: [ok(labels_json([{"id": 0, "label": "promo"}]))], REVIEW: [fail()]})
    labels, _, error = run(router, lambda: hc.classify_headlines([lead(0)], "NVDA", ["Nvidia"]))
    assert labels == {0: "promo"} and error is None and not router.sent(REVIEW)


def test_payload_models_and_policy() -> None:
    router = Router(
        {
            SCREEN: [ok(labels_json([{"id": 0, "label": "keep"}]))],
            REVIEW: [ok(labels_json([{"id": 0, "label": "keep"}]))],
        }
    )
    run(router, lambda: hc.classify_headlines([lead(0)], "NVDA", ["Nvidia"]))
    assert [c["model"] for c in router.calls] == [SCREEN, REVIEW]
    for body in router.calls:
        assert body["reasoning"] == {"effort": "low"}
        assert body["provider"] == {"data_collection": "deny"}


def test_openrouter_json_retries_once_then_raises() -> None:
    router = Router({REVIEW: [fail()]})
    with pytest.raises(httpx.HTTPStatusError):
        run(router, lambda: hc.openrouter_json("system", "content"))
    assert len(router.calls) == 2


def related_hits() -> list[list[Relation]]:
    return [[Relation(name="Micron", relation="competitor", aliases=("Micron",))] for _ in range(3)]


def test_related_kept_only_when_both_keep_entity_from_review() -> None:
    router = Router(
        {
            SCREEN: [
                ok(
                    labels_json(
                        [
                            {"id": 0, "label": "keep", "entity": "micron"},
                            {"id": 1, "label": "drop", "entity": "Micron"},
                            {"id": 2, "label": "keep", "entity": "Micron"},
                        ]
                    )
                )
            ],
            REVIEW: [
                ok(
                    labels_json(
                        [
                            {"id": 0, "label": "keep", "entity": "Micron"},
                            {"id": 1, "label": "drop", "entity": "Micron"},
                        ]
                    )
                )
            ],
        }
    )
    labels, _, error = run(
        router,
        lambda: hc.classify_related(
            [lead(i) for i in range(3)], "NVDA", ["Nvidia"], related_hits()
        ),
    )
    assert labels == {
        0: {"label": "keep", "entity": "Micron"},
        1: {"label": "drop", "entity": "Micron"},
        2: {"label": "drop", "entity": "Micron"},
    }
    assert error is None
    assert len(item_lines(router.sent(REVIEW)[0])) == 2


def macro(i: int, kind: str, importance: int, event: str) -> dict[str, Any]:
    return {"id": i, "type": kind, "importance": importance, "event": event}


def test_macro_stricter_type_lower_importance_review_slug() -> None:
    router = Router(
        {
            SCREEN: [
                ok(
                    labels_json(
                        [
                            macro(0, "development", 3, "fed-hike"),
                            macro(1, "off_topic", 1, "golf"),
                            macro(2, "development", 2, "cpi"),
                        ]
                    )
                )
            ],
            REVIEW: [
                ok(
                    labels_json(
                        [
                            macro(0, "commentary", 2, "fed-minutes"),
                            macro(1, "development", 3, "cpi-release"),
                        ]
                    )
                )
            ],
        }
    )
    labels, _, error = run(router, lambda: hc.classify_macro([lead(i) for i in range(3)]))
    assert labels == {
        0: {"type": "commentary", "importance": 2, "event": "fed-minutes"},
        1: {"type": "off_topic", "importance": 1, "event": "s1-golf"},
        2: {"type": "development", "importance": 2, "event": "cpi-release"},
    }
    assert error is None
    assert len(item_lines(router.sent(REVIEW)[0])) == 2


def test_macro_review_failure_keeps_unprefixed_screen_slugs() -> None:
    router = Router(
        {
            SCREEN: [ok(labels_json([macro(0, "development", 3, "fed-hike")]))],
            REVIEW: [fail()],
        }
    )
    labels, _, error = run(router, lambda: hc.classify_macro([lead(0)]))
    assert labels == {0: {"type": "development", "importance": 3, "event": "fed-hike"}}
    assert error is not None and error.endswith("(fail-open)")


def test_deepen_search_keeps_labels_when_one_stage_failed_open(db_session: Session) -> None:
    db_session.add(InstrumentProfile(identifier="AMKR", market="US", aliases=["Amkor", "AMKR"]))
    db_session.flush()
    leads = [
        Lead("https://fixture.example/a", "Amkor announces a new agreement", DEEPEN_NOW),
        Lead("https://fixture.example/b", "Amkor expands capacity", DEEPEN_NOW),
    ]
    error = "classifier: review ReadTimeout (fail-open)"
    worker = deepen.DeepenRun(
        db_session,
        paid_slot(db_session),
        load_intel_deepen_config(),
        False,
        DEEPEN_NOW,
        DEEPEN_NOW - timedelta(hours=24),
        [],
        {},
    )
    try:
        with (
            patch.object(worker, "_provider", return_value="tavily"),
            patch.object(
                worker,
                "_call",
                return_value=("tavily", PaidResult(200, Decimal(1), Decimal(".008"), leads=leads)),
            ),
            patch.object(
                deepen, "classify_headlines", return_value=({0: "keep", 1: "promo"}, 0.002, error)
            ),
        ):
            _, kept = resolve_fixture(worker, "tavily", WorkUnit("quiet", "AMKR"))
        assert [lead.url for lead in kept] == ["https://fixture.example/a"]
        assert worker.metrics["tavily"]["search_filtered"] == {"promo_llm": 1}
        assert error in worker.errors
    finally:
        worker.close()


def test_slot_macro_labels_applied_when_review_fails(db_session: Session) -> None:
    run_row = paid_slot(db_session)

    def answer(system: str, content: str, **kwargs: object) -> tuple[dict[str, object], float]:
        if kwargs.get("model") != SCREEN:
            raise ValueError("invalid response")
        count = len(content.splitlines())
        return {"labels": [macro(i, "development", 2, f"e{i}") for i in range(count)]}, 0.001

    with (
        patch.object(nc, "fetch_news", return_value=FetchNewsResult(macro_pool()[:3], [])),
        patch.object(hc, "openrouter_json", side_effect=answer),
    ):
        nc.capture_news(db_session, slot_run_id=run_row.id)
    collection = db_session.scalars(select(IntelCollectionRun)).one()
    stats = collection.stats["macro_classification"]
    assert isinstance(stats, dict)
    assert stats["failed_calls"] == 0 and stats["labeled"] >= 1 and stats["review_failed"] >= 1
    assert "classifier: review ValueError (fail-open)" in collection.errors
    assert collection.status == "partial"
    labeled = [r.record for r in db_session.scalars(select(News)) if hc.macro_label(r.record)]
    assert labeled and all(not str(r["event"]).startswith("s1-") for r in labeled)


def test_digest_warns_on_fail_open_and_shows_stage_counts(db_session: Session) -> None:
    slot_row = digest_slot(db_session, status="ok")
    db_session.add(
        IntelCollectionRun(
            kind="instrument",
            slot_run_id=slot_row.id,
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=5),
            status="partial",
            stats={
                "markets": {"US": {"total": 1, "processed": 1}},
                "classifier": {
                    "batches": 1,
                    "items": 3,
                    "cost_usd": 0.004,
                    "screen_cost_usd": 0.003,
                    "review_cost_usd": 0.001,
                    "retries": 1,
                    "screen_failed": 0,
                    "review_failed": 1,
                },
            },
            errors=["classifier: review HTTPStatusError HTTP 503 (fail-open)"],
        )
    )
    db_session.flush()
    _, body, severity = build_batch_report(db_session, slot_row)
    assert severity == "WARNING"
    assert "AI review stage failed, other stage applied (HTTP 503)" in body
    assert (
        "AI review: checked 3 headlines in 1 batches, cost $0.004 (screen $0.003, review $0.001), "
        "1 retries, 0 screen and 1 review stages failed open" in body
    )


def test_digest_stays_info_without_classifier_errors(db_session: Session) -> None:
    slot_row = digest_slot(db_session, status="ok")
    db_session.add(
        IntelCollectionRun(
            kind="instrument",
            slot_run_id=slot_row.id,
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=5),
            status="ok",
            stats={"markets": {"US": {"total": 1, "processed": 1}}},
            errors=[],
        )
    )
    db_session.flush()
    assert build_batch_report(db_session, slot_row)[2] == "INFO"


@pytest.mark.parametrize("call", ["headlines", "related", "macro"])
def test_missing_labels_retries_then_fails_open(call: str) -> None:
    good = {
        "headlines": [{"id": 0, "label": "keep"}],
        "related": [{"id": 0, "label": "keep", "entity": "Micron"}],
        "macro": [macro(0, "development", 2, "fed")],
    }[call]
    router = Router({SCREEN: [ok("{}")], REVIEW: [ok(labels_json(good))]})
    stats: dict[str, float] = {}
    fn: Callable[[], Any] = {
        "headlines": lambda: hc.classify_headlines([lead(0)], "NVDA", ["Nvidia"], stats=stats),
        "related": lambda: hc.classify_related(
            [lead(0)], "NVDA", ["Nvidia"], related_hits()[:1], stats=stats
        ),
        "macro": lambda: hc.classify_macro([lead(0)], stats=stats),
    }[call]
    labels, _, error = run(router, fn)
    assert len(router.sent(SCREEN)) == 2 and stats["retries"] == 1
    assert error == "classifier: screen KeyError (fail-open)"
    assert 0 in labels


def test_related_successful_stage_omission_is_not_kept() -> None:
    keep = labels_json([{"id": 0, "label": "keep", "entity": "Micron"}])
    for answers in (
        {SCREEN: [ok(labels_json([]))], REVIEW: [ok(keep)]},
        {SCREEN: [ok(keep)], REVIEW: [ok(labels_json([]))]},
    ):
        labels, _, error = run(
            Router(answers),
            lambda: hc.classify_related([lead(0)], "NVDA", ["Nvidia"], related_hits()[:1]),
        )
        assert labels == {} and error is None


def test_related_entity_comes_from_review() -> None:
    router = Router(
        {
            SCREEN: [ok(labels_json([{"id": 0, "label": "keep", "entity": "Micron"}]))],
            REVIEW: [ok(labels_json([{"id": 0, "label": "keep", "entity": ""}]))],
        }
    )
    labels, _, _ = run(
        router, lambda: hc.classify_related([lead(0)], "NVDA", ["Nvidia"], related_hits()[:1])
    )
    assert labels == {0: {"label": "keep", "entity": ""}}


def test_review_integer_duplicate_refers_to_reindexed_survivor() -> None:
    router = Router(
        {
            SCREEN: [
                ok(
                    labels_json(
                        [
                            {"id": 0, "label": "promo", "duplicate_of": None},
                            {"id": 1, "label": "keep", "duplicate_of": None},
                            {"id": 2, "label": "keep", "duplicate_of": None},
                        ]
                    )
                )
            ],
            # Survivors 1 and 2 arrive as 0 and 1; review marks the second a duplicate of 0.
            REVIEW: [
                ok(
                    labels_json(
                        [
                            {"id": 0, "label": "keep", "duplicate_of": None},
                            {"id": 1, "label": "keep", "duplicate_of": 0},
                        ]
                    )
                )
            ],
        }
    )
    labels, _, _ = run(
        router,
        lambda: hc.classify_headlines(
            [lead(i) for i in range(3)], "NVDA", ["Nvidia"], recent_titles=["x"]
        ),
    )
    assert labels == {0: "promo", 1: "keep", 2: "duplicate"}


def test_review_failure_then_success_retries_once() -> None:
    keep = labels_json([{"id": 0, "label": "keep"}])
    router = Router({SCREEN: [ok(keep)], REVIEW: [fail(), ok(keep)]})
    stats: dict[str, float] = {}
    labels, _, error = run(
        router, lambda: hc.classify_headlines([lead(0)], "NVDA", ["Nvidia"], stats=stats)
    )
    assert labels == {0: "keep"} and error is None
    assert len(router.sent(REVIEW)) == 2 and stats["retries"] == 1


def test_headline_keep_needs_both_answering_stages() -> None:
    keep = labels_json([{"id": 0, "label": "keep"}])
    # A stage that answered but omitted the item cannot make it `keep`.
    for answers in (
        {SCREEN: [ok(labels_json([]))], REVIEW: [ok(keep)]},
        {SCREEN: [ok(keep)], REVIEW: [ok(labels_json([]))]},
    ):
        labels, _, error = run(
            Router(answers), lambda: hc.classify_headlines([lead(0)], "NVDA", ["Nvidia"])
        )
        assert labels == {0: "mention"} and error is None
    # A whole failed stage is skipped: the other stage decides (fail-open).
    labels, _, _ = run(
        Router({SCREEN: [ok(keep)], REVIEW: [fail()]}),
        lambda: hc.classify_headlines([lead(0)], "NVDA", ["Nvidia"]),
    )
    assert labels == {0: "keep"}


def test_macro_survivor_omitted_by_review_keeps_prefixed_screen_label() -> None:
    router = Router(
        {
            SCREEN: [
                ok(
                    labels_json(
                        [macro(0, "development", 3, "fed"), macro(1, "development", 2, "cpi")]
                    )
                )
            ],
            REVIEW: [ok(labels_json([macro(0, "development", 2, "fed-minutes")]))],
        }
    )
    labels, _, error = run(router, lambda: hc.classify_macro([lead(0), lead(1)]))
    assert labels == {
        0: {"type": "development", "importance": 2, "event": "fed-minutes"},
        1: {"type": "development", "importance": 2, "event": "s1-cpi"},
    }
    assert error is None


def test_required_key_not_a_list_is_retried() -> None:
    router = Router({REVIEW: [ok('{"labels": "none"}'), ok(labels_json([]))]})
    data, cost = run(router, lambda: hc.openrouter_json("s", "c", required="labels"))
    assert data == {"labels": []} and cost == pytest.approx(0.002) and len(router.calls) == 2


def test_final_exception_carries_billed_cost() -> None:
    router = Router({REVIEW: [ok("{}", cost=0.003)]})
    with pytest.raises(KeyError) as caught:
        run(router, lambda: hc.openrouter_json("s", "c", required="labels"))
    assert vars(caught.value)["cost_usd"] == pytest.approx(0.006)


def test_collection_stores_null_labels_when_both_stages_fail(db_session: Session) -> None:
    capture_profile(db_session)
    with (
        patch.object(cap, "sources_for", return_value=[("yahoo", lambda: capture_leads(1))]),
        patch.object(cap, "classify_headlines", side_effect=hc.classify_headlines),
        patch.object(httpx, "post", return_value=fail()) as post,
    ):
        result = cap.collect_instrument_news(
            db_session, CAPTURE_ENTRY, NOW, hc.load_cleaning_config()
        )
    assert post.call_count == 4
    assert result.classifier["failed_batches"] == 1
    assert result.classifier["screen_failed"] == 1 and result.classifier["review_failed"] == 1
    assert result.classifier["retries"] == 2
    assert db_session.scalars(select(News)).one().intel_label is None


def test_digest_shows_deepening_and_macro_stage_costs(db_session: Session) -> None:
    slot_row = digest_slot(db_session, status="ok")
    db_session.add(
        IntelCollectionRun(
            kind="rss",
            slot_run_id=slot_row.id,
            node="slot-post_close",
            started_at=NOW,
            finished_at=NOW,
            status="ok",
            stats={
                "feeds": {"FT": {"items": 1, "errors": 0}},
                "macro_classification": {
                    "candidates": 2,
                    "labeled": 2,
                    "calls": 1,
                    "failed_calls": 0,
                    "cost_usd": 0.0012,
                    "development": 2,
                    "commentary": 0,
                    "off_topic": 0,
                    "screen_cost_usd": 0.0008,
                    "review_cost_usd": 0.0004,
                    "retries": 0,
                    "screen_failed": 0,
                    "review_failed": 0,
                },
            },
            errors=[],
        )
    )
    slot_row.details = {
        "deepening": {
            "macro_rank_failed": 0,
            "macro_rank_partial": 0,
            "classifier_stages": {
                "screen_cost_usd": 0.123,
                "review_cost_usd": 0.456,
                "retries": 7,
                "screen_failed": 0,
                "review_failed": 2,
            },
        }
    }
    db_session.flush()
    body = build_batch_report(db_session, slot_row)[1]
    assert "screen $0.000800, review $0.000400, 0 retries" in body
    assert (
        "AI review in deepening (paid search and macro ranking): screen $0.123000, "
        "review $0.456000, 7 retries, 0 screen and 2 review stages failed open." in body
    )
