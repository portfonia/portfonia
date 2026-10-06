"""PR #682 review fixes for issue #681; every LLM and paid provider is mocked."""

import contextlib
import threading
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast
from unittest.mock import patch

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.intel import InstrumentProfile, IntelSlotRun, NewsInstrument
from app.models.news import News
from app.models.paid_intel import IntelArticle
from app.services import headline_cleaning as hc
from app.services import instrument_news_capture as capture
from app.services import intel_deepen as deepen
from app.services.instrument_news_sources import CollectedItem
from app.services.instrument_relations import Relation
from app.services.instrument_universe import UniverseEntry
from app.services.intel_deepen_config import load_intel_deepen_config
from app.services.intel_leads import Lead, stored_headlines
from app.services.intel_records import store_headline
from app.services.intel_selection import WorkUnit
from app.services.news_fetcher import url_hash
from app.services.paid_search import PaidResult
from app.tests.test_issue_635_deepening import worker as shared_worker  # noqa: F401
from app.tests.test_issue_681 import NOW, TSMC, stored

# Finding 1: the extracted lead carries the stored headline's news id.


def test_681_r1_stored_title_search_records_news_id(
    db_session: Session, request: pytest.FixtureRequest
) -> None:
    worker = cast(deepen.DeepenRun, request.getfixturevalue("shared_worker"))
    row = stored(db_session, "AAA wins a large order", NOW - timedelta(days=1), NOW - timedelta(1))
    found = Lead("https://fixture.example/order", "AAA wins a large order", NOW - timedelta(days=1))
    with (
        patch(
            "app.services.paid_search.TavilyClient.search",
            return_value=PaidResult(200, Decimal(1), Decimal(0), leads=[found]),
        ),
        patch.object(deepen, "classify_headlines", return_value=({0: "keep"}, 0, None)),
        patch.object(worker, "_extract_batch") as extract,
    ):
        worker.run_wave(
            [
                WorkUnit(
                    "mover", "AAA", window_start=NOW.date() - timedelta(5), providers=("tavily",)
                )
            ],
            {},
            {"AAA": ["AAA"]},
        )
    leads = [lead for _, lead in extract.call_args.args[1]]
    assert [(lead.url, lead.news_id) for lead in leads] == [(found.url, row.id)]
    # `_extract_batch` writes IntelArticle.news_id = lead.news_id; with that row
    # accepted, the next wave no longer selects the stored title.
    db_session.add(
        IntelArticle(
            slot_run_id=db_session.query(IntelSlotRun).one().id,
            provider="tavily",
            url_key="k",
            news_id=leads[0].news_id,
            status="accepted",
            record={"v": 1, "kind": "instrument", "title": "t", "body": "b"},
            body_chars=1,
            fetched_at=NOW,
        )
    )
    db_session.flush()
    unit = WorkUnit("mover", "AAA", window_start=NOW.date() - timedelta(days=5))
    assert stored_headlines(db_session, unit, ["AAA"], load_intel_deepen_config(), NOW) == []


# Finding 2: one URL-hash write order across direct and related headlines.


def ordered_urls() -> tuple[str, str]:
    """Two URLs whose hashes sort low, high."""
    urls = sorted((f"https://fixture.example/r681/{i}" for i in range(2)), key=url_hash)
    return urls[0], urls[1]


def test_681_r2_parallel_direct_and_related_writes_do_not_deadlock(session_test_db: None) -> None:
    low, high = ordered_urls()
    # INTC keeps `high` directly and `low` only as a TSMC-related headline; TSM keeps
    # both directly. Before the fix INTC wrote high then low while TSM wrote low then high.
    shared_low = CollectedItem("TSM and TSMC raise wafer prices", NOW, low)
    shared_high = CollectedItem("INTC and TSM sign a packaging deal", NOW, high)
    items = {"INTC": [shared_low, shared_high], "TSM": [shared_low, shared_high]}
    relations = {"INTC": [TSMC], "TSM": []}
    barrier = threading.Barrier(2)
    local = threading.local()
    real_store = store_headline
    errors: list[BaseException] = []

    def store(*args: Any, **kwargs: Any) -> Any:
        result = real_store(*args, **kwargs)
        if not getattr(local, "waited", False):
            local.waited = True
            # Hold the first row lock until the other worker has taken its first one;
            # with a shared write order the other worker is blocked instead, and the
            # barrier times out.
            with contextlib.suppress(threading.BrokenBarrierError):
                barrier.wait(timeout=3)
        return result

    def sources(entry: UniverseEntry, *_: object) -> list[tuple[str, Callable[[], Any]]]:
        return [
            (
                "finnhub",
                lambda: [
                    CollectedItem(i.title, i.published_at, i.url) for i in items[entry.identifier]
                ],
            )
        ]

    def run(identifier: str) -> None:
        try:
            with SessionLocal() as session:
                capture.collect_instrument_news(
                    session,
                    UniverseEntry(identifier, identifier, "US"),
                    NOW,
                    hc.load_cleaning_config(),
                    relations=relations[identifier],
                )
                session.commit()
        except BaseException as exc:
            errors.append(exc)

    hashes = [url_hash(low), url_hash(high)]
    try:
        with (
            patch.object(capture, "sources_for", side_effect=sources),
            patch.object(capture, "classify_headlines", return_value=({}, 0.0, None)),
            patch.object(
                capture,
                "classify_related",
                return_value=({0: {"label": "keep", "entity": "TSMC"}}, 0.0, None),
            ),
            patch.object(capture, "store_headline", side_effect=store),
        ):
            threads = [threading.Thread(target=run, args=(i,)) for i in ("INTC", "TSM")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        assert errors == []
        with SessionLocal() as session:
            links = session.execute(
                select(NewsInstrument.identifier, News.url_hash, NewsInstrument.relation)
                .join(News, News.id == NewsInstrument.news_id)
                .where(News.url_hash.in_(hashes))
            ).all()
        assert sorted(links) == sorted(
            [
                ("INTC", url_hash(low), "competitor"),
                ("INTC", url_hash(high), None),
                ("TSM", url_hash(low), None),
                ("TSM", url_hash(high), None),
            ]
        )
    finally:
        with SessionLocal() as session:
            session.execute(delete(News).where(News.url_hash.in_(hashes)))
            session.execute(
                delete(InstrumentProfile).where(InstrumentProfile.identifier.in_(["INTC", "TSM"]))
            )
            session.commit()


# Finding 3: related candidates seed duplicate checks only once kept.


def test_681_r3_dropped_related_title_does_not_block_its_rewording(db_session: Session) -> None:
    older = CollectedItem(
        "TSMC raises full year revenue guidance", NOW - timedelta(hours=2), "https://x/1"
    )
    newer = CollectedItem("TSMC raises full year revenue guidance now", NOW, "https://x/2")
    labels = {
        0: {"label": "drop", "entity": "TSMC"},
        1: {"label": "keep", "entity": "TSMC"},
    }
    with (
        patch.object(capture, "sources_for", return_value=[("finnhub", lambda: [older, newer])]),
        patch.object(capture, "classify_related", return_value=(labels, 0.0, None)) as classifier,
    ):
        result = capture.collect_instrument_news(
            db_session,
            UniverseEntry("INTC", "INTC", "US"),
            NOW,
            hc.load_cleaning_config(),
            relations=[TSMC],
        )
    assert [x.title for x in classifier.call_args.args[0]] == [older.title, newer.title]
    kept = db_session.scalars(
        select(News.url_hash).join(NewsInstrument).where(NewsInstrument.identifier == "INTC")
    ).all()
    assert kept == [url_hash(newer.url)]
    assert result.cleaning["related_kept"] == 1


def test_681_r3b_two_kept_rewordings_store_one(db_session: Session) -> None:
    older = CollectedItem(
        "TSMC raises full year revenue guidance", NOW - timedelta(hours=2), "https://x/1"
    )
    newer = CollectedItem("TSMC raises full year revenue guidance now", NOW, "https://x/2")
    labels = {i: {"label": "keep", "entity": "TSMC"} for i in range(2)}
    with (
        patch.object(capture, "sources_for", return_value=[("finnhub", lambda: [older, newer])]),
        patch.object(capture, "classify_related", return_value=(labels, 0.0, None)),
    ):
        result = capture.collect_instrument_news(
            db_session,
            UniverseEntry("INTC", "INTC", "US"),
            NOW,
            hc.load_cleaning_config(),
            relations=[Relation("TSMC", ("TSMC",), "competitor")],
        )
    assert result.cleaning["related_kept"] == 1
    assert result.cleaning["related_duplicate"] == 1
