"""Issue #620 per-instrument isolation, budgets, batches and leads."""

import logging
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.intel import InstrumentProfile, IntelSlotRun
from app.models.news import News
from app.services import instrument_news_capture as cap
from app.services.headline_cleaning import CleaningConfig, load_cleaning_config
from app.services.instrument_news_sources import CollectedItem
from app.services.instrument_universe import UniverseEntry
from app.tests.test_intel_records import NOW

ENTRY = UniverseEntry("NVDA", "NVDA", "US")


def profile(session: Session, identifier: str = "NVDA", market: str = "US") -> InstrumentProfile:
    p = InstrumentProfile(
        identifier=identifier,
        market=market,
        name_en=identifier,
        aliases=[identifier],
        name_resolved_at=NOW,
    )
    session.add(p)
    session.flush()
    return p


def leads(n: int) -> list[CollectedItem]:
    return [
        CollectedItem(f"NVDA concrete development {i}", NOW, f"https://fixture.example/{i}")
        for i in range(n)
    ]


def test_acceptance_10_source_isolation(db_session: Session) -> None:
    profile(db_session)
    profile(db_session, "AMZN")
    finnhub_item = CollectedItem(
        "NVDA reports quarterly earnings above expectations", NOW, "https://fixture.example/finnhub"
    )
    amazon_items = [
        CollectedItem(
            "AMZN concrete development " + str(i), NOW, "https://fixture.example/amazon" + str(i)
        )
        for i in range(3)
    ]
    with (
        patch.object(
            cap,
            "sources_for",
            side_effect=[
                [
                    (
                        "google_news",
                        lambda: (_ for _ in ()).throw(ValueError("secret https://fixture.example")),
                    ),
                    ("finnhub", lambda: [finnhub_item]),
                    ("yahoo", lambda: leads(1)),
                ],
                [
                    ("google_news", lambda: [amazon_items[0]]),
                    ("finnhub", lambda: [amazon_items[1]]),
                    ("yahoo", lambda: [amazon_items[2]]),
                ],
            ],
        ),
        patch.object(cap, "block_reason", return_value=None),
        patch.object(
            cap,
            "classify_headlines",
            side_effect=lambda items, ticker, aliases, recent_titles=None, stats=None: (
                {i: "keep" for i in range(len(items))},
                0,
                None,
            ),
        ),
    ):
        first = cap.collect_instrument_news(db_session, ENTRY, NOW, load_cleaning_config())
        second = cap.collect_instrument_news(
            db_session, UniverseEntry("AMZN", "AMZN", "US"), NOW, load_cleaning_config()
        )
    assert first.stats["google_news"]["errors"] == 1
    assert len(first.leads) == 2 and len(second.leads) == 3
    assert len(db_session.scalars(select(News)).all()) == 5
    assert "https://" not in str(first.errors) and not second.errors


def test_acceptance_17_dropped_labels_not_stored(db_session: Session) -> None:
    profile(db_session)
    with (
        patch.object(cap, "sources_for", return_value=[("yahoo", lambda: leads(4))]),
        patch.object(cap, "block_reason", return_value=None),
        patch.object(
            cap,
            "classify_headlines",
            return_value=({0: "keep", 1: "mention", 2: "promo", 3: "unrelated"}, 0, None),
        ),
    ):
        result = cap.collect_instrument_news(db_session, ENTRY, NOW, load_cleaning_config())
    assert len(result.leads) == 2
    assert {r.intel_label for r in db_session.scalars(select(News))} == {"keep", "mention"}
    assert result.cleaning["promo_llm"] == result.cleaning["unrelated_llm"] == 1


def test_acceptance_18_failed_batch_stored_null(db_session: Session) -> None:
    profile(db_session)
    with (
        patch.object(cap, "sources_for", return_value=[("yahoo", lambda: leads(1))]),
        patch.object(
            cap, "classify_headlines", return_value=({}, 0, "classifier: HTTPStatusError HTTP 500")
        ) as classifier,
    ):
        result = cap.collect_instrument_news(db_session, ENTRY, NOW, load_cleaning_config())
    assert classifier.call_count == 1 and result.classifier["failed_batches"] == 1
    assert db_session.scalars(select(News)).one().intel_label is None


def test_acceptance_20_per_instrument_chunks(db_session: Session) -> None:
    profile(db_session)

    def classify(
        items: list[CollectedItem],
        ticker: str,
        aliases: list[str],
        recent_titles: Sequence[str] | None = None,
        stats: dict[str, float] | None = None,
    ) -> tuple[dict[int, str], float, str | None]:
        return {i: "keep" for i in range(len(items))}, 0, None

    settings = get_settings().model_copy(update={"INTEL_CLASSIFIER_BATCH": 150})
    with (
        patch.object(cap, "get_settings", return_value=settings),
        patch.object(cap, "sources_for", return_value=[("yahoo", lambda: leads(230))]),
        patch.object(cap, "block_reason", return_value=None),
        patch.object(cap, "classify_headlines", side_effect=classify) as classifier,
    ):
        result = cap.collect_instrument_news(db_session, ENTRY, NOW, load_cleaning_config())
    assert [len(c.args[0]) for c in classifier.call_args_list] == [100, 100, 30]
    assert len(result.leads) == 230


def test_acceptance_25_hk_queries(db_session: Session) -> None:
    with patch.object(
        cap,
        "get_settings",
        return_value=get_settings().model_copy(update={"INTEL_GOOGLE_NEWS_ENABLED": True}),
    ):
        p = profile(db_session, "0700.HK", "HK")
        p.name_en = "Tencent"
        p.name_zh = "Tencent Chinese"
        sources = cap.sources_for(
            UniverseEntry("0700.HK", "0700.HK", "HK"), p, NOW - timedelta(hours=48), NOW
        )
        with (
            patch.object(cap, "fetch_google_news", return_value=[]) as google,
            patch.object(cap, "fetch_yahoo", return_value=[]),
            patch.object(cap, "fetch_eastmoney_ann", return_value=[]),
        ):
            for _, fetch in sources:
                fetch()
        assert [c.args[1] for c in google.call_args_list] == ["en-US", "zh-HK"]
        p.name_zh = None
        assert (
            len(
                [
                    s
                    for s, _ in cap.sources_for(
                        UniverseEntry("0700.HK", "0700.HK", "HK"), p, NOW - timedelta(hours=48), NOW
                    )
                    if s == "google_news"
                ]
            )
            == 1
        )


@pytest.mark.parametrize(
    "slot_name,expected,unreached",
    [
        ("post_close", "US", {"Japan": ["JP"], "Korea": ["KR"]}),
        ("pre_open", "JP", {"Korea": ["KR"], "US": ["US"]}),
    ],
)
def test_acceptance_09_market_order_and_budget(
    db_session: Session,
    caplog: pytest.LogCaptureFixture,
    slot_name: str,
    expected: str,
    unreached: dict[str, list[str]],
) -> None:
    entries = [
        UniverseEntry("JP", "JP", "Japan"),
        UniverseEntry("US", "US", "US"),
        UniverseEntry("KR", "KR", "Korea"),
    ]
    for e in entries:
        p = profile(db_session, e.identifier, e.market)
        if e.market == "Japan":
            p.news_collected_at = NOW
    slot = IntelSlotRun(slot=slot_name, run_date=NOW.date(), started_at=NOW, status="running")
    db_session.add(slot)
    db_session.flush()
    submitted = []

    def work(
        session: Session,
        e: UniverseEntry,
        now: datetime,
        config: CleaningConfig,
        **_: object,
    ) -> cap.InstrumentResult:
        submitted.append(e.identifier)
        return cap.InstrumentResult()

    logging.getLogger(cap.__name__).disabled = False
    settings = get_settings().model_copy(update={"INTEL_COLLECT_WORKERS": 1})
    with (
        patch.object(cap, "get_settings", return_value=settings),
        patch.object(cap, "intel_universe", return_value=entries),
        patch.object(cap, "resolve_profiles", return_value=[]),
        patch.object(cap, "collect_instrument_news", side_effect=work),
        patch.object(
            cap,
            "SessionLocal",
            side_effect=lambda: Session(
                bind=db_session.connection(), join_transaction_mode="create_savepoint"
            ),
        ),
        patch.object(time, "monotonic", side_effect=[0, 0, 2]),
        caplog.at_level(logging.WARNING),
    ):
        run = cap.collect_slot_news(db_session, slot, NOW, 1, lambda ident, items: None)
    assert submitted == [expected] and run.status == "partial"
    assert run.instruments_processed == 1 and run.stats["unreached"] == unreached
    assert "intel collection budget exhausted:" in caplog.text and "unreached=2" in caplog.text
    assert "https://" not in caplog.text


def test_acceptance_28_parallel_jobs_and_completion_order(session_test_db: None) -> None:
    import threading

    from sqlalchemy import delete

    from app.core.database import get_engine
    from app.models.intel import IntelCollectionRun, NewsInstrument

    entries = [
        UniverseEntry(f"PAR{i}", f"PAR{i}", market)
        for i, market in enumerate(["US", "US", "Japan", "Japan", "Korea", "Korea"])
    ]
    barrier = threading.Barrier(6)
    events = []
    lock = threading.Lock()
    with Session(get_engine()) as session:
        for e in entries:
            profile(session, e.identifier, e.market)
        slot = IntelSlotRun(
            slot="post_close", run_date=NOW.date(), started_at=NOW, status="running"
        )
        session.add(slot)
        session.commit()
        slot_id = slot.id

        def work(
            worker: Session,
            e: UniverseEntry,
            now: datetime,
            config: CleaningConfig,
            **_kwargs: object,
        ) -> cap.InstrumentResult:
            from app.services.intel_records import link_instrument, store_headline
            from app.services.news_fetcher import NewsItem, url_hash

            barrier.wait(timeout=5)
            time.sleep(0.01 * (int(e.identifier[-1]) + 1))
            lead = CollectedItem(
                e.identifier + " development", now, "https://fixture.example/" + e.identifier
            )
            nid, _ = store_headline(
                worker,
                NewsItem(url_hash(lead.url), lead.title, lead.url, "", now, None),
                "instrument",
                "article",
                "keep",
            )
            link_instrument(worker, nid, e.identifier)
            lead.news_id = nid
            with lock:
                events.append(("classified", e.identifier))
            return cap.InstrumentResult(leads=[lead])

        def done(ident: str, items: list[CollectedItem]) -> None:
            with Session(get_engine()) as check:
                assert check.get(News, items[0].news_id) is not None
            with lock:
                events.append(("done", ident))

        try:
            with (
                patch.object(cap, "intel_universe", return_value=entries),
                patch.object(cap, "resolve_profiles", return_value=[]),
                patch.object(cap, "collect_instrument_news", side_effect=work),
            ):
                result = cap.collect_slot_news(session, slot, NOW, 60, done)
            assert result.status == "ok" and result.instruments_processed == 6
            assert sum(name == "done" for name, _ in events) == 6
            for e in entries:
                assert events.index(("classified", e.identifier)) < events.index(
                    ("done", e.identifier)
                )
        finally:
            session.execute(
                delete(News).where(
                    News.id.in_(
                        select(NewsInstrument.news_id).where(
                            NewsInstrument.identifier.in_([e.identifier for e in entries])
                        )
                    )
                )
            )
            session.execute(
                delete(InstrumentProfile).where(
                    InstrumentProfile.identifier.in_([e.identifier for e in entries])
                )
            )
            session.execute(
                delete(IntelCollectionRun).where(IntelCollectionRun.slot_run_id == slot_id)
            )
            session.execute(delete(IntelSlotRun).where(IntelSlotRun.id == slot_id))
            session.commit()


def test_profile_collection_timestamp_survives_name_failure(db_session: Session) -> None:
    with patch.object(cap, "sources_for", return_value=[]):
        cap.collect_instrument_news(db_session, ENTRY, NOW, load_cleaning_config())
    p = db_session.get(InstrumentProfile, "NVDA")
    assert p is not None and p.news_collected_at == NOW and p.name_resolved_at is None


def test_acceptance_09_same_market_carry_over(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    from app.services.intel_digest import build_batch_report

    entries = [UniverseEntry(i, i, "US") for i in ["AAA", "BBB", "CCC"]]
    for e in entries:
        profile(db_session, e.identifier)
    slot = IntelSlotRun(slot="post_close", run_date=NOW.date(), started_at=NOW, status="running")
    db_session.add(slot)
    db_session.flush()
    submitted: list[str] = []

    def work(
        worker: Session,
        e: UniverseEntry,
        now: datetime,
        config: CleaningConfig,
        **_: object,
    ) -> cap.InstrumentResult:
        submitted.append(e.identifier)
        p = worker.get(InstrumentProfile, e.identifier)
        assert p is not None
        p.news_collected_at = now
        return cap.InstrumentResult()

    settings = get_settings().model_copy(update={"INTEL_COLLECT_WORKERS": 1})
    with (
        patch.object(cap, "get_settings", return_value=settings),
        patch.object(cap, "intel_universe", side_effect=lambda s: list(entries)),
        patch.object(cap, "resolve_profiles", return_value=[]),
        patch.object(cap, "collect_instrument_news", side_effect=work),
        patch.object(
            cap,
            "SessionLocal",
            side_effect=lambda: Session(
                bind=db_session.connection(), join_transaction_mode="create_savepoint"
            ),
        ),
        patch.object(time, "monotonic", side_effect=[0, 0, 2]),
        caplog.at_level(logging.WARNING),
    ):
        run = cap.collect_slot_news(db_session, slot, NOW, 1, lambda ident, items: None)
    assert run.stats["unreached"] == {"US": ["BBB", "CCC"]}
    assert run.status == "partial" and run.instruments_processed == 1
    warnings = [r.message for r in caplog.records if "budget exhausted" in r.message]
    assert warnings == [
        f"intel collection budget exhausted: slot=post_close run_date={NOW.date()} unreached=2 US: BBB,CCC"
    ]
    slot.finished_at = NOW + timedelta(minutes=1)
    db_session.flush()
    assert "Instruments covered: 1 of 3" in build_batch_report(db_session, slot)[1]
    assert "Not reached in the time limit: US: BBB,CCC" in build_batch_report(db_session, slot)[1]
    db_session.expire_all()
    submitted.clear()
    with (
        patch.object(cap, "get_settings", return_value=settings),
        patch.object(cap, "intel_universe", side_effect=lambda s: list(entries)),
        patch.object(cap, "resolve_profiles", return_value=[]),
        patch.object(cap, "collect_instrument_news", side_effect=work),
        patch.object(
            cap,
            "SessionLocal",
            side_effect=lambda: Session(
                bind=db_session.connection(), join_transaction_mode="create_savepoint"
            ),
        ),
    ):
        run = cap.collect_slot_news(
            db_session, slot, NOW + timedelta(days=1), 60, lambda ident, items: None
        )
    assert submitted == ["BBB", "CCC", "AAA"]
    assert "unreached" not in run.stats


def test_acceptance_20_separate_instrument_calls(db_session: Session) -> None:
    entries = [ENTRY, UniverseEntry("AMZN", "AMZN", "US")]
    for e in entries:
        profile(db_session, e.identifier)
    slot = IntelSlotRun(slot="post_close", run_date=NOW.date(), started_at=NOW, status="running")
    db_session.add(slot)
    db_session.flush()
    events: list[tuple[str, str]] = []

    def sources(
        e: UniverseEntry, *args: object
    ) -> list[tuple[str, Callable[[], list[CollectedItem]]]]:
        return [
            (
                "yahoo",
                lambda: [
                    CollectedItem(
                        e.ticker + f" development {i}",
                        NOW,
                        "https://fixture.example/" + e.ticker + str(i),
                    )
                    for i in range(5)
                ],
            )
        ]

    def classify(
        items: list[CollectedItem],
        ticker: str,
        aliases: list[str],
        recent_titles: Sequence[str] | None = None,
        stats: dict[str, float] | None = None,
    ) -> tuple[dict[int, str], float, str | None]:
        assert len(items) == 5
        events.append(("classified", ticker))
        return {i: "keep" for i in range(5)}, 0, None

    def done(identifier: str, items: list[CollectedItem]) -> None:
        assert len(items) == 5 and all(x.identifier == identifier and x.news_id for x in items)
        events.append(("done", identifier))

    settings = get_settings().model_copy(update={"INTEL_COLLECT_WORKERS": 1})
    with (
        patch.object(cap, "get_settings", return_value=settings),
        patch.object(cap, "intel_universe", return_value=entries),
        patch.object(cap, "resolve_profiles", return_value=[]),
        patch.object(cap, "sources_for", side_effect=sources),
        patch.object(cap, "block_reason", return_value=None),
        patch.object(cap, "classify_headlines", side_effect=classify) as classifier,
        patch.object(
            cap,
            "SessionLocal",
            side_effect=lambda: Session(
                bind=db_session.connection(), join_transaction_mode="create_savepoint"
            ),
        ),
    ):
        result = cap.collect_slot_news(db_session, slot, NOW, 60, done)
    assert result.instruments_processed == 2 and classifier.call_count == 2
    for entry in entries:
        assert events.index(("classified", entry.ticker)) < events.index(("done", entry.identifier))


def test_acceptance_23_invalid_config_fails_run(db_session: Session) -> None:
    from app.services.intel_digest import build_batch_report

    slot = IntelSlotRun(slot="post_close", run_date=NOW.date(), started_at=NOW, status="running")
    db_session.add(slot)
    db_session.flush()
    with (
        patch.object(cap, "intel_universe", return_value=[]),
        patch.object(cap, "resolve_profiles", return_value=[]),
        patch.object(cap, "load_cleaning_config", side_effect=ValueError("bad regex")),
    ):
        result = cap.collect_slot_news(db_session, slot, NOW, 60, lambda ident, items: None)
    slot.finished_at = NOW + timedelta(minutes=1)
    db_session.flush()
    assert result.status == "failed" and not db_session.scalars(select(News)).all()
    assert build_batch_report(db_session, slot)[2] == "WARNING"


def test_acceptance_19_missing_label_is_stored_null(db_session: Session) -> None:
    profile(db_session)
    with (
        patch.object(cap, "sources_for", return_value=[("yahoo", lambda: leads(2))]),
        patch.object(cap, "block_reason", return_value=None),
        patch.object(cap, "classify_headlines", return_value=({0: "keep"}, 0, None)),
    ):
        result = cap.collect_instrument_news(db_session, ENTRY, NOW, load_cleaning_config())
    assert len(result.leads) == 2
    assert {row.intel_label for row in db_session.scalars(select(News))} == {"keep", None}


def test_crossed_url_order_two_postgres_sessions(session_test_db: None) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import delete, text

    from app.core.database import get_engine
    from app.models.intel import NewsInstrument

    entries = [UniverseEntry(i, i, "US") for i in ["CROSSA", "CROSSB"]]
    items = [
        CollectedItem("Shared earnings", NOW, "https://fixture.example/cross-x"),
        CollectedItem("Shared acquisition", NOW, "https://fixture.example/cross-y"),
    ]
    rendezvous = threading.Barrier(2)
    first_done = {e.identifier: threading.Event() for e in entries}
    calls: dict[int, int] = {}
    from app.services.intel_records import store_headline as real_store

    def store(session: Session, *args: object, **kwargs: object) -> object:
        key = id(session)
        count = calls.get(key, 0)
        calls[key] = count + 1
        ident = str(session.info["identifier"])
        other = entries[1 if ident == entries[0].identifier else 0].identifier
        if count == 0:
            rendezvous.wait(timeout=5)
        result = real_store(session, *args, **kwargs)  # type: ignore[arg-type]
        if count == 0:
            first_done[ident].set()
            first_done[other].wait(timeout=0.3)
        return result

    def sources(
        e: UniverseEntry, *args: object
    ) -> list[tuple[str, Callable[[], list[CollectedItem]]]]:
        ordered = items if e.identifier == entries[0].identifier else list(reversed(items))
        return [("yahoo", lambda: ordered)]

    def classify(
        items: list[CollectedItem], *args: object, **kwargs: object
    ) -> tuple[dict[int, str], float, None]:
        return (
            {i: "keep" if x.title == "Shared earnings" else "mention" for i, x in enumerate(items)},
            0,
            None,
        )

    def worker(e: UniverseEntry) -> cap.InstrumentResult:
        with Session(get_engine()) as session:
            session.execute(text("SET LOCAL statement_timeout = '8s'"))
            session.info["identifier"] = e.identifier
            result = cap.collect_instrument_news(session, e, NOW, load_cleaning_config())
            session.commit()
            return result

    with Session(get_engine()) as session:
        for e in entries:
            profile(session, e.identifier)
        session.commit()
        try:
            with (
                patch.object(cap, "sources_for", side_effect=sources),
                patch.object(cap, "block_reason", return_value=None),
                patch.object(cap, "classify_headlines", side_effect=classify),
                patch.object(cap, "store_headline", side_effect=store),
                ThreadPoolExecutor(max_workers=2) as pool,
            ):
                futures = [pool.submit(worker, e) for e in entries]
                results = [future.result(timeout=12) for future in futures]
            assert sum(r.stats["yahoo"]["inserted"] for r in results) == 2
            rows = session.scalars(
                select(News).where(News.url_hash.in_([x.headline().url_hash for x in items]))
            ).all()
            assert len(rows) == 2
            assert {str(r.record["title"]): r.intel_label for r in rows} == {
                "Shared earnings": "keep",
                "Shared acquisition": "mention",
            }
            links = session.scalars(
                select(NewsInstrument).where(NewsInstrument.news_id.in_([r.id for r in rows]))
            ).all()
            assert len(links) == 4
            assert {x.identifier for x in links} == {e.identifier for e in entries}
        finally:
            session.execute(
                delete(News).where(News.url_hash.in_([x.headline().url_hash for x in items]))
            )
            session.execute(
                delete(InstrumentProfile).where(
                    InstrumentProfile.identifier.in_([e.identifier for e in entries])
                )
            )
            session.commit()


def test_filings_count_separately_from_missing_classifier_labels(db_session: Session) -> None:
    from app.models.intel import IntelCollectionRun
    from app.services.intel_digest import build_batch_report

    profile(db_session)
    filing = CollectedItem(
        "8-K announcement", NOW, "https://fixture.example/filing", kind="filing", filing_form="8-K"
    )
    with (
        patch.object(
            cap,
            "sources_for",
            return_value=[("sec", lambda: [filing]), ("yahoo", lambda: leads(1))],
        ),
        patch.object(cap, "block_reason", return_value=None),
        patch.object(cap, "classify_headlines", return_value=({}, 0, None)) as classify,
    ):
        result = cap.collect_instrument_news(db_session, ENTRY, NOW, load_cleaning_config())
    assert result.cleaning["stored_null_label"] == 1
    assert result.cleaning["filings_stored"] == 1
    assert len(classify.call_args.args[0]) == 1
    slot = IntelSlotRun(
        slot="post_close",
        run_date=NOW.date(),
        started_at=NOW,
        finished_at=NOW + timedelta(minutes=1),
        status="ok",
    )
    db_session.add(slot)
    db_session.flush()
    db_session.add(
        IntelCollectionRun(
            kind="instrument",
            slot_run_id=slot.id,
            started_at=NOW,
            finished_at=NOW,
            status="ok",
            stats={"cleaning": result.cleaning},
            errors=[],
        )
    )
    db_session.flush()
    body = build_batch_report(db_session, slot)[1]
    assert "1 kept without a label" in body and "1 company filings" in body
