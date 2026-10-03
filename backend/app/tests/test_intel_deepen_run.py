from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from threading import Event
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.intel import InstrumentProfile
from app.models.paid_intel import IntelArticle, IntelArticleLink, PaidApiUsage
from app.services import intel_deepen as deepen
from app.services import intel_digest
from app.services.instrument_news_sources import CollectedItem
from app.services.instrument_universe import UniverseEntry
from app.services.intel_deepen_config import load_intel_deepen_config
from app.services.intel_leads import Lead, url_key
from app.services.intel_selection import WorkUnit, select_units
from app.services.paid_search import PaidResult
from app.tests.test_intel_deepen_rules import NOW
from app.tests.test_intel_paid import slot


@pytest.fixture(autouse=True)
def no_unmocked_search_classifier() -> Iterator[None]:
    with patch.object(
        deepen,
        "classify_headlines",
        side_effect=AssertionError("search classifier must be mocked"),
    ):
        yield


def settings() -> object:
    return get_settings().model_copy(
        update={"PARALLEL_API_KEY": SecretStr("fixture"), "INTEL_PAID_WORKERS": 1}
    )


def factory(db: Session) -> Session:
    return Session(bind=db.connection(), join_transaction_mode="create_savepoint")


def test_14_invalid_key_fallback_and_ledger(db_session: Session) -> None:
    run = slot(db_session)
    calls = []

    def post(url: str, **kwargs: Any) -> httpx.Response:
        calls.append("tavily" if "tavily" in url else "parallel")
        return httpx.Response(401 if "tavily" in url else 200, json={"results": []})

    with (
        patch.object(deepen, "get_settings", return_value=settings()),
        patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
        patch("app.services.paid_search.post", side_effect=post),
        patch("app.services.paid_usage.send_ops_alert", return_value=True) as alert,
    ):
        worker = deepen.DeepenRun(
            db_session,
            run,
            load_intel_deepen_config(),
            False,
            NOW,
            NOW - timedelta(hours=24),
            [],
            {},
        )
        unit = WorkUnit("quiet", "AAA", providers=("tavily",))
        worker.run_wave(
            [unit],
            {"AAA": [CollectedItem("AAA agreement", NOW, "https://example.com/a")]},
            {"AAA": ["AAA"]},
        )
        worker.run_wave(
            [WorkUnit("quiet", "BBB", providers=("tavily",))],
            {"BBB": [CollectedItem("BBB agreement", NOW, "https://example.com/b")]},
            {"BBB": ["BBB"]},
        )
        worker.close()
    assert calls == ["tavily", "parallel", "parallel"]
    assert all(r.provider == "parallel" for r in db_session.scalars(select(PaidApiUsage)))
    assert alert.call_count == 1


def test_16_weekend_no_conditions(db_session: Session) -> None:
    run = slot(db_session)
    run.run_date = NOW.date() + timedelta(days=1)
    with (
        patch.object(deepen, "get_settings", return_value=settings()),
        patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
    ):
        worker = deepen.DeepenRun(
            db_session,
            run,
            load_intel_deepen_config(),
            True,
            NOW,
            NOW - timedelta(hours=24),
            [],
            {},
        )
        worker.finish(db_session, {}, [])
    assert not db_session.scalars(select(PaidApiUsage)).all()
    run.details = {"deepening": worker.details()}
    assert (
        "weekend: no unit met the conditions" in intel_digest.build_slot_digest(db_session, run)[1]
    )
    assert worker.usage.caps["tavily"] == 8


def test_18_usage_and_ab_digest(db_session: Session) -> None:
    run = slot(db_session)
    run.details = {
        "deepening": {
            "selections": [
                {
                    "kind": "mover",
                    "identifier": "AAA",
                    "reason": "d1 -6.2%",
                    "providers": ["tavily", "parallel"],
                }
            ],
            "usage": {
                "tavily": {
                    "run": 4,
                    "run_cap": 20,
                    "month": 412,
                    "month_limit": 1000,
                    "configured": True,
                },
                "parallel": {
                    "run": 0.013,
                    "run_cap": 0.5,
                    "month": 3.1,
                    "month_limit": 10,
                    "configured": True,
                },
            },
            "metrics": {
                "tavily": {
                    "accepted": 2,
                    "rejected": {"too_short": 1},
                    "cost_usd": 0.032,
                    "cost_per_accepted": 0.016,
                }
            },
            "mode": "ab",
            "warning_ratio": 0.8,
            "errors": [],
        }
    }
    body = intel_digest.build_slot_digest(db_session, run)[1]
    assert "Tavily 4/20 run" in body and "41%" in body and "d1 -6.2%" in body
    assert "too_short" in body and "cost_per_accepted" in body


def test_21_wave_before_collection_finishes(db_session: Session) -> None:
    from app.services.intel_signals import Signal

    run = slot(db_session)
    extracted = Event()
    order = []

    def post(*args: object, **kwargs: Any) -> httpx.Response:
        order.append("extract")
        extracted.set()
        return httpx.Response(200, json={"results": []})

    entries = [UniverseEntry("AAA", "AAA", "US"), UniverseEntry("BBB", "BBB", "US")]
    with (
        patch.object(deepen, "get_settings", return_value=settings()),
        patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
        patch("app.services.paid_search.post", side_effect=post),
    ):
        worker = deepen.DeepenRun(
            db_session,
            run,
            load_intel_deepen_config(),
            False,
            NOW,
            NOW - timedelta(hours=24),
            entries,
            {"AAA": Signal("AAA", mover=True, strength=0.06)},
        )
        worker.collected("AAA", [CollectedItem("AAA agreement", NOW, "https://example.com/a")])
        assert extracted.wait(5)
        order.append("last_collection")
        worker.collected("BBB", [])
        worker.close()
    assert order.index("extract") < order.index("last_collection")


def test_23_no_urls_persisted(db_session: Session, caplog: pytest.LogCaptureFixture) -> None:
    run = slot(db_session)
    item = CollectedItem("AAA agreement", NOW, "https://fixturepublisher.example/a")
    result = {
        "results": [
            {
                "url": item.url,
                "raw_content": "Agreement announced. " * 30
                + " [Read more](https://fixturepublisher.example/b)",
            }
        ]
    }
    with (
        patch.object(deepen, "get_settings", return_value=settings()),
        patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
        patch("app.services.paid_search.post", return_value=httpx.Response(200, json=result)),
    ):
        worker = deepen.DeepenRun(
            db_session,
            run,
            load_intel_deepen_config(),
            False,
            NOW,
            NOW - timedelta(hours=24),
            [],
            {},
        )
        worker.run_wave(
            [WorkUnit("mover", "AAA", providers=("tavily",))], {"AAA": [item]}, {"AAA": ["AAA"]}
        )
        worker.close()
    run.details = {"deepening": worker.details()}
    for model in [IntelArticle, IntelArticleLink, PaidApiUsage]:
        for row in db_session.scalars(select(model)):
            for column in model.__table__.columns:
                assert "https://" not in str(getattr(row, column.name))
                assert "fixturepublisher" not in str(getattr(row, column.name))
    body = intel_digest.build_slot_digest(db_session, run)[1]
    assert "https://" not in body + str(run.details) + caplog.text


def test_19_invalid_config_preserves_collection_digest(db_session: Session) -> None:
    from app.models.intel import IntelCollectionRun
    from app.services.news_capture import PoolCaptureResult
    from app.tasks import intel_tasks as task

    with (
        patch.object(task, "SessionLocal", return_value=db_session),
        patch.object(task, "now_et", return_value=NOW),
        patch.object(task, "today_et", return_value=NOW.date()),
        patch.object(task, "load_intel_deepen_config", side_effect=ValueError("invalid")),
        patch.object(task, "resolve_profiles", return_value=[]),
        patch.object(task, "capture_news", return_value=PoolCaptureResult(0, [])),
        patch.object(
            task, "collect_slot_news", return_value=IntelCollectionRun(status="ok")
        ) as collect,
        patch.object(task, "send_ops_alert", return_value=True) as send,
    ):
        task.intel_slot_task("post_close")
    assert collect.called and send.call_args.kwargs["severity"] == "WARNING"
    assert "Collection" in send.call_args.args[1] or "collection" in send.call_args.args[1]


def test_11_extract_batch_http_truncation(db_session: Session) -> None:
    cfg = load_intel_deepen_config()
    cfg.caps.leads_per_mover = 12
    configured = get_settings().model_copy(
        update={"TAVILY_RUN_CREDIT_CAP": 3, "PARALLEL_API_KEY": None, "INTEL_PAID_WORKERS": 1}
    )
    calls = []

    def post(url: str, **kwargs: Any) -> httpx.Response:
        calls.append(kwargs["json"])
        return httpx.Response(200, json={"results": []})

    with (
        patch.object(deepen, "get_settings", return_value=configured),
        patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
        patch("app.services.paid_search.post", side_effect=post),
    ):
        worker = deepen.DeepenRun(
            db_session, slot(db_session), cfg, False, NOW, NOW - timedelta(hours=24), [], {}
        )
        worker._call("tavily", "search", "AAA", start=NOW.date())
        worker.run_wave(
            [WorkUnit("mover", "AAA", providers=("tavily",))],
            {
                "AAA": [
                    CollectedItem("AAA agreement", NOW, f"https://host{i}.example/a")
                    for i in range(12)
                ]
            },
            {"AAA": ["AAA"]},
        )
        worker.close()
    assert len(calls) == 2 and len(calls[1]["urls"]) == 10
    assert worker.usage.run_used["tavily"] == 3
    assert worker.metrics["tavily"]["budget"] == 2


@pytest.mark.parametrize("weekend", [False, True])
def test_23_full_slot_no_urls(
    db_session: Session, caplog: pytest.LogCaptureFixture, weekend: bool
) -> None:
    from app.models.intel import InstrumentProfile, IntelSlotRun
    from app.models.ticker_intel import TickerIntel
    from app.services.intel_signals import Signal
    from app.services.news_capture import PoolCaptureResult
    from app.tasks import intel_tasks as task

    now = NOW + timedelta(days=int(weekend))
    db_session.add(InstrumentProfile(identifier="AAA", market="US", aliases=["AAA"]))
    db_session.flush()
    entries = [UniverseEntry("AAA", "AAA", "US")]
    signal = Signal(
        "AAA",
        mover=not weekend,
        strength=0.06,
        reason="d1 -6.0%",
        window_start=now.date() - timedelta(days=1),
    )
    url = "https://fixturepublisher.example/a"

    def collect(
        session: Session,
        run: object,
        now: datetime,
        budget: object,
        callback: Any,
        **kwargs: Any,
    ) -> object:
        collection = kwargs["collection_run"]
        collection.status = "ok"
        callback("AAA", [CollectedItem("AAA agreement", now, url)])
        # Finish this real-Postgres wave before the task's next DB read.
        if callback.__self__.futures:
            callback.__self__.futures[0].result(timeout=5)
        return collection

    with (
        patch.object(task, "SessionLocal", side_effect=lambda: factory(db_session)),
        patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
        patch.object(task, "now_et", return_value=now),
        patch.object(task, "today_et", return_value=now.date()),
        patch.object(task, "intel_universe", return_value=entries),
        patch.object(task, "resolve_profiles", return_value=[]),
        patch.object(task, "compute_signals", return_value={} if weekend else {"AAA": signal}),
        patch.object(task, "capture_news", return_value=PoolCaptureResult(0, [])),
        patch.object(task, "collect_slot_news", side_effect=collect),
        patch.object(task, "run_post_close_analysis", return_value={}) as shared,
        patch.object(deepen, "get_settings", return_value=settings()),
        patch(
            "app.services.paid_search.post",
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": url,
                            "raw_content": "Agreement announced. " * 40,
                            "full_content": "Agreement announced. " * 40,
                        }
                    ]
                },
            ),
        ),
        patch.object(task, "send_ops_alert", return_value=True) as digest,
    ):
        assert task.intel_slot_task("post_close")["status"] == "ok"
    assert shared.called is not weekend
    assert not db_session.scalars(select(TickerIntel)).all()
    articles = db_session.scalars(select(IntelArticle)).all()
    assert len(articles) == (0 if weekend else 2)
    assert all(r.status == "accepted" for r in articles)
    payloads = [digest.call_args.args[1], caplog.text]
    for model in [IntelArticle, IntelArticleLink, PaidApiUsage, IntelSlotRun]:
        for row in db_session.scalars(select(model)):
            payloads.extend(str(getattr(row, c.name)) for c in model.__table__.columns)
    assert all(
        "http://" not in p and "https://" not in p and "fixturepublisher" not in p for p in payloads
    )


def test_18_worked_example_paid_waves(db_session: Session) -> None:
    from decimal import Decimal

    from app.models.intel import NewsInstrument
    from app.models.news import News
    from app.services.macro_detector import detect_macro_signals
    from app.services.news_fetcher import NewsItem

    run = slot(db_session)

    def items(identifier: str, count: int) -> list[CollectedItem]:
        return [
            CollectedItem(
                f"{identifier} agreement", NOW, f"https://{identifier.lower()}{i}.example/a"
            )
            for i in range(count)
        ]

    owned = {"BBB": items("BBB", 2), "CCC": items("CCC", 3), "DDD": items("DDD", 1)}
    article = News(
        url_hash=url_key(owned["DDD"][0].url),
        published_at=NOW,
        fetched_at=NOW,
        record={"title": "DDD agreement"},
    )
    db_session.add(article)
    db_session.flush()
    db_session.add(NewsInstrument(news_id=article.id, identifier="DDD", created_at=NOW))
    db_session.flush()
    owned["DDD"][0].news_id = article.id
    pool = [
        NewsItem(
            url_key(url := f"https://{theme}{i}.example/a"),
            f"{word} agreement",
            url,
            "fixture",
            NOW,
            None,
        )
        for theme, word in [("energy", "oil"), ("rates", "rate")]
        for i in range(3)
    ]
    requests = []

    def post(url: str, **kwargs: Any) -> httpx.Response:
        payload = kwargs["json"]
        requests.append((url, payload))
        if url.endswith("search"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"url": x.url, "title": x.title, "published_date": NOW.isoformat()}
                        for x in items("AAA", 2)
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": u,
                        "raw_content": "x" * (280 if "bbb1" in u else 600),
                        "full_content": "x" * (280 if "bbb1" in u else 600),
                    }
                    for u in payload["urls"]
                ]
            },
        )

    with (
        patch.object(deepen, "get_settings", return_value=settings()),
        patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
        patch.object(
            deepen,
            "detect_macro_signals",
            side_effect=lambda articles, **kw: detect_macro_signals(
                articles, keyword_table={"energy": ["oil"], "rates": ["rate"]}, **kw
            ),
        ),
        patch("app.services.paid_search.post", side_effect=post),
        patch.object(
            deepen,
            "classify_headlines",
            return_value=({0: "keep", 1: "keep"}, 0.0, None),
        ) as classifier,
    ):
        worker = deepen.DeepenRun(
            db_session,
            run,
            load_intel_deepen_config(),
            False,
            NOW,
            NOW - timedelta(hours=24),
            [],
            {},
        )
        worker.pool_items = pool
        worker.run_wave(
            [
                WorkUnit("mover", "BBB", reason="d5 +21.4%"),
                WorkUnit("mover", "AAA", reason="d1 -6.2%"),
            ],
            owned,
            {t: [t] for t in owned},
        )
        worker.run_wave(
            [
                WorkUnit("quiet", "CCC", reason="near_d3 +9.1%"),
                WorkUnit("quiet", "DDD", reason="new_filing"),
                WorkUnit("macro", theme="energy", reason="theme 14 items"),
                WorkUnit("macro", theme="rates", reason="theme 9 items"),
            ],
            owned,
            {t: [t] for t in owned},
        )
        worker.close()
    assert classifier.call_count == 2
    assert worker.usage.run_used == {"tavily": Decimal(4), "parallel": Decimal(".013")}
    assert [len(p["urls"]) for u, p in requests if "tavily" in u and u.endswith("extract")] == [
        4,
        3,
        3,
    ]
    assert [len(p["urls"]) for u, p in requests if "parallel" in u and u.endswith("extract")] == [
        2,
        2,
        1,
        3,
    ]
    assert all(
        r.record is None
        for r in db_session.scalars(select(IntelArticle).where(IntelArticle.status == "rejected"))
    )
    run.details = {"deepening": worker.details()}
    body = intel_digest.build_slot_digest(db_session, run)[1]
    assert "Tavily 4/20 run" in body and "Parallel $0.013/$0.5 run" in body
    assert "new_filing" in body and "near_d3" in body and "too_short" in body
    assert "cost_per_accepted" in body and "month (" in body


def test_16_weekend_worked_example(db_session: Session) -> None:
    from app.models.intel import NewsInstrument
    from app.models.news import News
    from app.models.ticker_intel import TickerIntel
    from app.services.intel_signals import Signal
    from app.services.macro_detector import detect_macro_signals
    from app.services.news_fetcher import NewsItem

    now = NOW + timedelta(days=1)
    run = slot(db_session)
    run.run_date, run.started_at = now.date(), now
    article = News(
        url_hash="ddd", record={"title": "DDD agreement"}, published_at=now, fetched_at=now
    )
    db_session.add(article)
    db_session.flush()
    db_session.add(NewsInstrument(identifier="DDD", news_id=article.id, created_at=now))
    pool = []
    for i in range(14):
        url = f"https://energy{i}.example/a"
        pool.append(NewsItem(url_key(url), "oil agreement", url, "fixture", now, None))
        db_session.add(
            News(
                url_hash=url_key(url),
                record={"title": "oil agreement"},
                published_at=now,
                fetched_at=now,
            )
        )
    db_session.flush()
    with (
        patch.object(deepen, "get_settings", return_value=settings()),
        patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
        patch.object(
            deepen,
            "detect_macro_signals",
            side_effect=lambda articles, **kw: detect_macro_signals(
                articles, keyword_table={"energy": ["oil"]}, **kw
            ),
        ),
        patch(
            "app.services.paid_search.post", return_value=httpx.Response(200, json={"results": []})
        ),
    ):
        worker = deepen.DeepenRun(
            db_session,
            run,
            load_intel_deepen_config(),
            True,
            now,
            now - timedelta(hours=24),
            [],
            {},
        )
        worker.collected(
            "DDD",
            [CollectedItem("DDD agreement", now, "https://ddd.example/a", news_id=article.id)],
        )
        worker.finish(
            db_session,
            {"DDD": Signal("DDD", filings=1, window_start=(now - timedelta(hours=24)).date())},
            pool,
        )
    assert [(u.identifier or u.theme, u.providers) for u in worker.selected] == [
        ("DDD", ("tavily",)),
        ("energy", ("parallel",)),
    ]
    assert worker.usage.caps["tavily"] == 8
    assert [(r.provider, r.operation) for r in db_session.scalars(select(PaidApiUsage))] == [
        ("tavily", "extract"),
        ("parallel", "extract"),
    ]
    assert not db_session.scalars(select(TickerIntel)).all()


def test_26_search_requests_use_quiet_windows(db_session: Session) -> None:
    from datetime import UTC, datetime

    from app.models.intel import NewsInstrument
    from app.models.news import News
    from app.services.intel_signals import Signal, compute_signals

    previous = datetime(2026, 10, 2, 2, tzinfo=UTC)
    cfg = load_intel_deepen_config()
    filing = News(
        url_hash="filing",
        kind="filing",
        record={"title": "DDD 8-K"},
        published_at=NOW,
        fetched_at=NOW,
    )
    db_session.add(filing)
    db_session.flush()
    db_session.add(NewsInstrument(identifier="DDD", news_id=filing.id, created_at=NOW))
    db_session.flush()
    signals = compute_signals(
        db_session,
        [UniverseEntry("DDD", "DDD", "US")],
        NOW.date(),
        previous,
        cfg,
        slot="post_close",
        now=NOW,
    )
    signals["CCC"] = Signal.from_closes(
        "CCC",
        [
            (NOW.date() - timedelta(days=i), v)
            for i, v in enumerate([109.1, 109, 108, 107, 106, 100])
        ],
        cfg,
        NOW.date(),
        "post_close",
        previous,
    )
    calls = []

    def post(url: str, **kwargs: Any) -> httpx.Response:
        calls.append(kwargs["json"])
        return httpx.Response(200, json={"results": []})

    with (
        patch.object(
            deepen,
            "get_settings",
            return_value=get_settings().model_copy(
                update={"PARALLEL_API_KEY": None, "INTEL_PAID_WORKERS": 1}
            ),
        ),
        patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
        patch("app.services.paid_search.post", side_effect=post),
    ):
        worker = deepen.DeepenRun(db_session, slot(db_session), cfg, False, NOW, previous, [], {})
        worker.run_wave(select_units(signals, {}, cfg), {}, {})
        worker.close()
    assert [p["start_date"] for p in calls] == ["2026-09-27", "2026-10-01"]


@pytest.mark.parametrize("status", [200, 401, 403, 402, 429, 432, 433, 500])
def test_response_ledger_and_disable(db_session: Session, status: int) -> None:
    with (
        patch.object(deepen, "get_settings", return_value=settings()),
        patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
        patch(
            "app.services.paid_search.post",
            return_value=httpx.Response(status, json={"results": []}),
        ),
    ):
        worker = deepen.DeepenRun(
            db_session,
            slot(db_session),
            load_intel_deepen_config(),
            False,
            NOW,
            NOW - timedelta(hours=24),
            [],
            {},
        )
        worker._call("tavily", "search", "AAA news", start=NOW.date())
        worker.close()
    assert len(db_session.scalars(select(PaidApiUsage)).all()) == (0 if status in (401, 403) else 1)
    assert ("tavily" in worker.usage.disabled) == (status in (401, 403, 402, 429, 432, 433))
    assert worker.usage.reserved["tavily"] == 0


@pytest.mark.parametrize("sent", [False, True])
def test_timeout_accounting(db_session: Session, sent: bool) -> None:
    exception = httpx.ReadTimeout("fixture") if sent else httpx.ConnectError("fixture")
    with (
        patch.object(deepen, "get_settings", return_value=settings()),
        patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
        patch("app.services.paid_search.post", side_effect=exception),
    ):
        worker = deepen.DeepenRun(
            db_session,
            slot(db_session),
            load_intel_deepen_config(),
            False,
            NOW,
            NOW - timedelta(hours=24),
            [],
            {},
        )
        worker._call("tavily", "search", "AAA news", start=NOW.date())
        worker.close()
    assert len(db_session.scalars(select(PaidApiUsage)).all()) == int(sent)
    assert worker.usage.run_used["tavily"] == int(sent)
    assert worker.usage.reserved["tavily"] == 0


def test_zero_credit_extract_blocks_second_batch(db_session: Session) -> None:
    from decimal import Decimal

    from app.services.intel_leads import Lead

    with (
        patch.object(
            deepen,
            "get_settings",
            return_value=get_settings().model_copy(
                update={"PARALLEL_API_KEY": None, "TAVILY_RUN_CREDIT_CAP": 1}
            ),
        ),
        patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
        patch(
            "app.services.paid_search.post",
            return_value=httpx.Response(200, json={"usage": {"credits": 0}, "results": []}),
        ) as post,
    ):
        worker = deepen.DeepenRun(
            db_session,
            slot(db_session),
            load_intel_deepen_config(),
            False,
            NOW,
            NOW - timedelta(hours=24),
            [],
            {},
        )
        try:
            for batch_id in range(2):
                worker._extract_batch(
                    "tavily",
                    [
                        (
                            WorkUnit("quiet", "AAA"),
                            Lead(f"https://example.com/{batch_id}/{i}", "AAA"),
                        )
                        for i in range(4)
                    ],
                )
        finally:
            worker.close()
    assert post.call_count == 1
    row = db_session.scalars(select(PaidApiUsage)).one()
    assert row.units == 1 and row.cost_usd == Decimal(".008")
    assert worker.usage.run_used["tavily"] == worker.usage.month_used["tavily"] == 1
    assert worker.metrics["tavily"]["budget"] == 4


def test_no_provider_does_not_reserve(db_session: Session) -> None:
    from app.services.intel_leads import Lead

    with patch.object(deepen, "get_settings", return_value=settings()):
        worker = deepen.DeepenRun(
            db_session,
            slot(db_session),
            load_intel_deepen_config(),
            False,
            NOW,
            NOW - timedelta(hours=24),
            [],
            {},
        )
    before = worker.usage.reserved.copy()
    try:
        with patch.object(worker, "_provider", return_value=None):
            worker._extract_batch(
                "tavily", [(WorkUnit("quiet", "AAA"), Lead("https://example.com/a", "AAA"))]
            )
        assert worker.usage.reserved == before
        assert worker.usage.skipped["tavily"] == 1
        assert worker.metrics["tavily"]["budget"] == 1
    finally:
        worker.close()


def test_budget_skip_does_not_consume_search_slot(db_session: Session) -> None:
    from decimal import Decimal

    with (
        patch.object(
            deepen,
            "get_settings",
            return_value=get_settings().model_copy(
                update={"PARALLEL_API_KEY": None, "TAVILY_RUN_CREDIT_CAP": 1}
            ),
        ),
        patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
        patch(
            "app.services.paid_search.post", return_value=httpx.Response(200, json={"results": []})
        ) as post,
    ):
        worker = deepen.DeepenRun(
            db_session,
            slot(db_session),
            load_intel_deepen_config(),
            False,
            NOW,
            NOW - timedelta(hours=24),
            [],
            {},
        )
        worker.usage.run_used["tavily"] = Decimal(".5")
        try:
            worker._search("tavily", WorkUnit("quiet", "AAA"))
            assert worker.searches == 0
            post.assert_not_called()
            worker.usage.run_used["tavily"] = Decimal(0)
            worker._search("tavily", WorkUnit("quiet", "AAA"))
            assert worker.searches == post.call_count == 1
        finally:
            worker.close()


def test_issue_628_search_filters_rules_and_classifier(db_session: Session) -> None:
    db_session.add(
        InstrumentProfile(
            identifier="AAOI", market="US", aliases=["Applied Optoelectronics", "AAOI"]
        )
    )
    db_session.flush()
    leads = [
        Lead(
            "https://fixture.example/promo",
            "Applied Optoelectronics Is Up 205% This Year. Is It Too Late to Buy AAOI Stock Now?",
            NOW,
        ),
        Lead(
            "https://fixture.example/today",
            "AAOI News Today | Why did Applied Optoelectronics stock go up today? $AAOI",
            NOW,
        ),
        Lead(
            "https://fixture.example/keep",
            "AAOI Stock Rallies As Hyperscale AI Orders Boost Outlook",
            NOW,
        ),
    ]
    worker = deepen.DeepenRun(
        db_session,
        slot(db_session),
        load_intel_deepen_config(),
        False,
        NOW,
        NOW - timedelta(hours=24),
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
                deepen, "classify_headlines", return_value=({0: "keep"}, 0.0002, None)
            ) as classify,
        ):
            chosen, kept = worker._search("tavily", WorkUnit("quiet", "AAOI"))
        assert chosen == "tavily"
        assert [lead.url for lead in kept] == ["https://fixture.example/keep"]
        classify.assert_called_once()
        assert [item.title for item in classify.call_args.args[0]] == [leads[2].title]
        assert classify.call_args.args[1:] == ("AAOI", ["Applied Optoelectronics", "AAOI"])
        assert worker.metrics["tavily"]["search_filtered"] == {"low_value_rule": 2}
        assert worker.metrics["tavily"]["search_classifier_cost_usd"] == 0.0002
        assert worker.metrics["tavily"]["cost_usd"] == 0.0
    finally:
        worker.close()


def test_issue_628_classifier_failure_drops_all_without_retry(db_session: Session) -> None:
    db_session.add(InstrumentProfile(identifier="AMKR", market="US", aliases=["Amkor", "AMKR"]))
    db_session.flush()
    leads = [
        Lead("https://fixture.example/a", "Amkor announces a new agreement", NOW),
        Lead("https://fixture.example/b", "Amkor expands capacity", NOW),
    ]
    worker = deepen.DeepenRun(
        db_session,
        slot(db_session),
        load_intel_deepen_config(),
        False,
        NOW,
        NOW - timedelta(hours=24),
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
            ) as search,
            patch.object(
                deepen,
                "classify_headlines",
                return_value=({}, 0.0, "classifier: HTTPStatusError HTTP 502"),
            ) as classifier,
        ):
            _, kept = worker._search("tavily", WorkUnit("quiet", "AMKR"))
        assert kept == []
        assert search.call_count == 1
        classifier.assert_called_once()
        assert worker.searches == 1
        assert worker.metrics["tavily"]["search_filtered"] == {"classifier_failed": 2}
        assert "classifier: HTTPStatusError HTTP 502" in worker.errors
    finally:
        worker.close()


def test_issue_628_search_drops_unrelated_and_promo(db_session: Session) -> None:
    db_session.add(InstrumentProfile(identifier="AMKR", market="US", aliases=["Amkor", "AMKR"]))
    db_session.flush()
    leads = [
        Lead(
            "https://fixture.example/amkr",
            "Amkor (AMKR) Stock Could Be 45% Undervalued On Cash Flow",
            NOW,
        ),
        Lead(
            "https://fixture.example/dell",
            "Dell Stocks Slip as Japan Plans $15 Billion AI Campus",
            NOW,
        ),
    ]
    worker = deepen.DeepenRun(
        db_session,
        slot(db_session),
        load_intel_deepen_config(),
        False,
        NOW,
        NOW - timedelta(hours=24),
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
                deepen, "classify_headlines", return_value=({0: "promo"}, 0.0, None)
            ) as classify,
            patch.object(worker, "_extract_batch") as extract,
            patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
        ):
            worker.run_wave([WorkUnit("quiet", "AMKR", providers=("tavily",))], {}, worker.aliases)
        classify.assert_called_once()
        extract.assert_not_called()
        assert len(classify.call_args.args[0]) == 1
        assert worker.metrics["tavily"]["search_filtered"] == {
            "unrelated_rule": 1,
            "promo_llm": 1,
        }
    finally:
        worker.close()


def test_issue_628_search_drops_quote_page_without_classifier(db_session: Session) -> None:
    db_session.add(
        InstrumentProfile(identifier="2333.HK", market="HK", aliases=["Great Wall Motor", "2333"])
    )
    db_session.flush()
    lead = Lead(
        "https://fixture.example/quote",
        "Great Wall Motor Company Limited (2333.HK) Stock Price, News, Quote & History",
        NOW,
    )
    worker = deepen.DeepenRun(
        db_session,
        slot(db_session),
        load_intel_deepen_config(),
        False,
        NOW,
        NOW - timedelta(hours=24),
        [],
        {},
    )
    try:
        with (
            patch.object(worker, "_provider", return_value="tavily"),
            patch.object(
                worker,
                "_call",
                return_value=("tavily", PaidResult(200, Decimal(1), Decimal(".008"), leads=[lead])),
            ),
            patch.object(deepen, "classify_headlines") as classify,
        ):
            _, kept = worker._search("tavily", WorkUnit("quiet", "2333.HK"))
        assert kept == []
        classify.assert_not_called()
        assert worker.metrics["tavily"]["search_filtered"] == {"low_value_rule": 1}
    finally:
        worker.close()


def test_issue_628_search_drops_missing_classifier_label(db_session: Session) -> None:
    db_session.add(InstrumentProfile(identifier="AMKR", market="US", aliases=["Amkor", "AMKR"]))
    db_session.flush()
    leads = [
        Lead("https://fixture.example/a", "Amkor announces a new agreement", NOW),
        Lead("https://fixture.example/b", "Amkor expands capacity", NOW),
    ]
    worker = deepen.DeepenRun(
        db_session,
        slot(db_session),
        load_intel_deepen_config(),
        False,
        NOW,
        NOW - timedelta(hours=24),
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
            patch.object(deepen, "classify_headlines", return_value=({1: "mention"}, 0.0, None)),
        ):
            _, kept = worker._search("tavily", WorkUnit("quiet", "AMKR"))
        assert [lead.url for lead in kept] == ["https://fixture.example/b"]
        assert worker.metrics["tavily"]["search_filtered"] == {"unlabeled_llm": 1}
    finally:
        worker.close()


def test_issue_628_search_classifier_failures_accumulate(db_session: Session) -> None:
    db_session.add(InstrumentProfile(identifier="AMKR", market="US", aliases=["Amkor", "AMKR"]))
    db_session.flush()
    leads = [
        Lead("https://fixture.example/a", "Amkor announces a new agreement", NOW),
        Lead("https://fixture.example/b", "Amkor expands capacity", NOW),
    ]
    worker = deepen.DeepenRun(
        db_session,
        slot(db_session),
        load_intel_deepen_config(),
        False,
        NOW,
        NOW - timedelta(hours=24),
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
                deepen,
                "classify_headlines",
                return_value=({}, 0.0, "classifier: HTTPStatusError HTTP 502"),
            ),
        ):
            worker._search("tavily", WorkUnit("quiet", "AMKR"))
            worker._search("tavily", WorkUnit("quiet", "AMKR"))
        assert worker.metrics["tavily"]["search_filtered"] == {"classifier_failed": 4}
    finally:
        worker.close()


def test_issue_628_collected_direct_lead_skips_search_and_classifier(
    db_session: Session,
) -> None:
    worker = deepen.DeepenRun(
        db_session,
        slot(db_session),
        load_intel_deepen_config(),
        False,
        NOW,
        NOW - timedelta(hours=24),
        [],
        {},
    )
    item = CollectedItem("AAA agreement", NOW, "https://fixture.example/a")
    try:
        with (
            patch.object(worker, "_search") as search,
            patch.object(deepen, "classify_headlines") as classify,
            patch.object(worker, "_extract_batch"),
            patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
        ):
            worker.run_wave(
                [WorkUnit("quiet", "AAA", providers=("tavily",))],
                {"AAA": [item]},
                {"AAA": ["AAA"]},
            )
        search.assert_not_called()
        classify.assert_not_called()
    finally:
        worker.close()


@pytest.mark.parametrize("weekend", [False, True])
def test_macro_lead_window_weekday_and_weekend(db_session: Session, weekend: bool) -> None:
    from app.core.timezones import ET
    from app.models.news import News
    from app.services.macro_detector import detect_macro_signals
    from app.services.news_fetcher import NewsItem

    now = (
        datetime(2026, 10, 3, 16, 15, tzinfo=ET)
        if weekend
        else datetime(2026, 9, 30, 16, 15, tzinfo=ET)
    )
    previous = now.replace(hour=7, minute=30)
    published = datetime(2026, 9, 29, 12, tzinfo=ET)
    run = slot(db_session)
    run.run_date, run.started_at = now.date(), now
    pool = []
    for i in range(10):
        url = f"https://energy{i}.example/a"
        pool.append(NewsItem(url_key(url), "oil agreement", url, "fixture", published, None))
        db_session.add(
            News(
                url_hash=url_key(url),
                record={"title": "oil agreement"},
                published_at=published,
                fetched_at=now,
            )
        )
    db_session.flush()
    with (
        patch.object(
            deepen,
            "get_settings",
            return_value=get_settings().model_copy(
                update={"PARALLEL_API_KEY": None, "INTEL_PAID_WORKERS": 1}
            ),
        ),
        patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
        patch.object(
            deepen,
            "detect_macro_signals",
            side_effect=lambda articles, **kw: detect_macro_signals(
                articles, keyword_table={"energy": ["oil"]}, **kw
            ),
        ),
        patch(
            "app.services.paid_search.post", return_value=httpx.Response(200, json={"results": []})
        ) as post,
    ):
        worker = deepen.DeepenRun(
            db_session, run, load_intel_deepen_config(), weekend, now, previous, [], {}
        )
        worker.finish(db_session, {}, pool)
    assert worker.selected[0].window_start == (previous.date() if weekend else None)
    assert post.call_count == (0 if weekend else 1)
    if not weekend:
        assert len(post.call_args.kwargs["json"]["urls"]) == 3


@pytest.mark.parametrize("provider", ["tavily", "parallel"])
@pytest.mark.parametrize("status", [402, 429, 432, 433, 400, 500])
def test_failed_search_settles_zero(db_session: Session, provider: str, status: int) -> None:
    with (
        patch.object(deepen, "get_settings", return_value=settings()),
        patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
        patch("app.services.paid_search.post", return_value=httpx.Response(status)),
    ):
        worker = deepen.DeepenRun(
            db_session,
            slot(db_session),
            load_intel_deepen_config(),
            False,
            NOW,
            NOW - timedelta(hours=24),
            [],
            {},
        )
        before_run = worker.usage.run_used[provider]
        before_month = worker.usage.month_used[provider]
        try:
            worker._call(provider, "search", "AAA news", start=NOW.date())
        finally:
            worker.close()
    row = db_session.scalars(select(PaidApiUsage)).one()
    assert row.provider == provider and row.http_status == status
    assert row.units == row.cost_usd == 0
    assert worker.usage.run_used[provider] == before_run
    assert worker.usage.month_used[provider] == before_month
    assert worker.usage.reserved[provider] == 0


@pytest.mark.parametrize("provider,used", [("tavily", Decimal(799)), ("parallel", Decimal(".799"))])
def test_failed_search_does_not_trigger_monthly_warning(
    db_session: Session,
    provider: str,
    used: Decimal,
) -> None:
    configured = get_settings().model_copy(
        update={
            "PARALLEL_API_KEY": SecretStr("fixture"),
            "PARALLEL_MONTHLY_USD_LIMIT": Decimal(1),
        }
    )
    db_session.add(
        PaidApiUsage(
            provider=provider,
            operation="search",
            units=used if provider == "tavily" else 1,
            cost_usd=used * Decimal(".008") if provider == "tavily" else used,
            http_status=200,
            created_at=NOW,
        )
    )
    db_session.flush()
    with (
        patch.object(deepen, "get_settings", return_value=configured),
        patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
        patch("app.services.paid_search.post", return_value=httpx.Response(500)),
        patch("app.services.paid_usage.send_ops_alert", return_value=True) as alert,
    ):
        worker = deepen.DeepenRun(
            db_session,
            slot(db_session),
            load_intel_deepen_config(),
            False,
            NOW,
            NOW - timedelta(hours=24),
            [],
            {},
        )
        try:
            worker._call(provider, "search", "AAA news", start=NOW.date())
        finally:
            worker.close()
    assert worker.usage.month_used[provider] == used
    alert.assert_not_called()


def test_search_http_does_not_block_collected(db_session: Session) -> None:
    posted, release, collected = Event(), Event(), Event()

    def post(*args: object, **kwargs: object) -> httpx.Response:
        posted.set()
        assert release.wait(5)
        return httpx.Response(200, json={"results": []})

    with (
        patch.object(deepen, "get_settings", return_value=settings()),
        patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
        patch("app.services.paid_search.post", side_effect=post),
    ):
        worker = deepen.DeepenRun(
            db_session,
            slot(db_session),
            load_intel_deepen_config(),
            False,
            NOW,
            NOW - timedelta(hours=24),
            [],
            {},
        )

        def collect() -> None:
            worker.collected("BBB", [])
            collected.set()

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                search = pool.submit(worker._search, "tavily", WorkUnit("quiet", "AAA"))
                try:
                    assert posted.wait(5)
                    collection = pool.submit(collect)
                    assert collected.wait(1), "collected() blocked during the HTTP post"
                    assert not release.is_set()
                    collection.result(timeout=5)
                finally:
                    release.set()
                    search.result(timeout=5)
        finally:
            worker.close()
    assert worker.searches == 1


def test_issue_628_classifier_cost_is_separate_from_paid_usage(db_session: Session) -> None:
    db_session.add(InstrumentProfile(identifier="AAOI", market="US", aliases=["AAOI"]))
    db_session.flush()
    run = slot(db_session)
    with (
        patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
        patch.object(deepen, "get_settings", return_value=settings()),
        patch(
            "app.services.paid_search.post",
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://fixture.example/orders",
                            "title": "AAOI Stock Rallies As Hyperscale AI Orders Boost Outlook",
                        }
                    ]
                },
            ),
        ) as search,
        patch.object(deepen, "classify_headlines", return_value=({0: "keep"}, 0.0002, None)),
    ):
        worker = deepen.DeepenRun(
            db_session,
            run,
            load_intel_deepen_config(),
            False,
            NOW,
            NOW - timedelta(hours=24),
            [],
            {},
        )
        try:
            _, kept = worker._search("tavily", WorkUnit("quiet", "AAOI"))
            assert len(kept) == 1
            assert worker.metrics["tavily"]["search_classifier_cost_usd"] == 0.0002
            assert worker.metrics["tavily"]["cost_usd"] == 0.008
            assert worker.metrics["tavily"]["searches"] == worker.searches == 1
            assert worker.usage.run_used["tavily"] == Decimal(1)
            usage = list(db_session.scalars(select(PaidApiUsage)))
            assert len(usage) == 1 and usage[0].cost_usd == Decimal(".008")
            run.details = {"deepening": worker.details()}
            digest = intel_digest.build_slot_digest(db_session, run)[1]
            assert "search_classifier_cost_usd=0.0002" in digest
            assert "search_filtered={}" in digest
            search.assert_called_once()
        finally:
            worker.close()


def test_issue_628_invalid_cleaning_disables_only_search(db_session: Session) -> None:
    with patch.object(deepen, "load_cleaning_config", side_effect=ValueError("fixture")):
        worker = deepen.DeepenRun(
            db_session,
            slot(db_session),
            load_intel_deepen_config(),
            False,
            NOW,
            NOW - timedelta(hours=24),
            [],
            {},
        )
    item = CollectedItem("AAA agreement", NOW, "https://fixture.example/direct")
    try:
        with (
            patch.object(worker, "_provider", return_value="tavily"),
            patch.object(worker, "_call") as provider,
            patch.object(deepen, "classify_headlines") as classifier,
            patch.object(worker, "_extract_batch") as extract,
            patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
        ):
            assert worker._search("tavily", WorkUnit("quiet", "AAA")) == ("tavily", [])
            provider.assert_not_called()
            classifier.assert_not_called()
            assert worker.searches == 0
            assert worker.errors == ["search_filter: ValueError"]
            worker.run_wave(
                [WorkUnit("quiet", "AAA", providers=("tavily",))],
                {"AAA": [item]},
                {"AAA": ["AAA"]},
            )
            extract.assert_called_once()
            assert extract.call_args.args[1][0][1].url == item.url
    finally:
        worker.close()


@pytest.mark.parametrize("provider", ["tavily", "parallel"])
def test_issue_628_filters_before_limit_without_duplicate_check(
    db_session: Session,
    provider: str,
) -> None:
    db_session.add(InstrumentProfile(identifier="AMKR", market="US", aliases=["Amkor"]))
    db_session.flush()
    # The newest rejected result must not consume a slot; identical survivors
    # deliberately prove that this search does not use the near-duplicate gate.
    leads = [
        Lead("https://fixture.example/first", "Amkor announces a new agreement", NOW),
        Lead("https://fixture.example/second", "Amkor expands capacity", NOW - timedelta(hours=1)),
        Lead("https://fixture.example/third", "Amkor expands capacity", NOW - timedelta(hours=2)),
    ]
    worker = deepen.DeepenRun(
        db_session,
        slot(db_session),
        load_intel_deepen_config(),
        False,
        NOW,
        NOW - timedelta(hours=24),
        [],
        {},
    )
    try:
        with (
            patch.object(worker, "_provider", return_value=provider),
            patch.object(
                worker,
                "_call",
                return_value=(
                    provider,
                    PaidResult(
                        200,
                        Decimal(1),
                        Decimal(".008"),
                        leads=leads,
                    ),
                ),
            ),
            patch.object(
                deepen,
                "classify_headlines",
                return_value=(
                    {0: "unrelated", 1: "keep", 2: "mention"},
                    0.0,
                    None,
                ),
            ) as classifier,
        ):
            chosen, kept = worker._search(provider, WorkUnit("quiet", "AMKR"))
        assert chosen == provider
        assert kept == leads[1:]
        assert [item.title for item in classifier.call_args.args[0]] == [
            lead.title for lead in leads
        ]
        assert worker.metrics[provider]["search_filtered"] == {"unrelated_llm": 1}
    finally:
        worker.close()
