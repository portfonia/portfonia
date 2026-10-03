"""Issue #630 ingestion acceptance with real Postgres and mocked HTTP."""

from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.intel import InstrumentProfile, IntelSlotRun, NewsInstrument
from app.models.news import News
from app.services import headline_cleaning as hc
from app.services import instrument_news_capture as cap
from app.services.instrument_news_sources import CollectedItem
from app.services.instrument_universe import UniverseEntry
from app.services.intel_deepen_config import load_intel_deepen_config
from app.services.intel_records import link_instrument, store_headline
from app.services.news_capture import PoolCaptureResult
from app.tasks import intel_tasks as task
from app.tests.test_intel_deepen_rules import NOW
from app.tests.test_issue_630_classifier import STORED, TITLES, items, response

ENTRY = UniverseEntry("AVGO", "AVGO", "US")


def profile(session: Session) -> None:
    session.add(
        InstrumentProfile(
            identifier="AVGO",
            market="US",
            name_en="Broadcom",
            aliases=["Broadcom", "AVGO"],
            name_resolved_at=NOW,
        )
    )
    session.flush()


def stored(session: Session, title: str, index: int = 0) -> None:
    lead = CollectedItem(
        title, NOW - timedelta(minutes=index + 1), f"https://fixture.example/stored/{index}"
    )
    nid, _ = store_headline(session, lead.headline(), "instrument", "article", "keep")
    link_instrument(session, nid, "AVGO")
    session.query(NewsInstrument).filter_by(news_id=nid, identifier="AVGO").one().created_at = (
        NOW - timedelta(days=1)
    )
    session.flush()


def test_630_11_stored_and_batch_duplicates(db_session: Session) -> None:
    profile(db_session)
    stored(db_session, STORED)
    rows = [
        {"id": 0, "label": "keep", "duplicate_of": "e0"},
        {"id": 1, "label": "keep", "duplicate_of": None},
        {"id": 2, "label": "keep", "duplicate_of": 1},
    ]
    with (
        patch.object(cap, "sources_for", return_value=[("yahoo", lambda: items())]),
        patch.object(cap, "classify_headlines", side_effect=hc.classify_headlines),
        patch.object(httpx, "post", return_value=response(rows)) as post,
    ):
        result = cap.collect_instrument_news(db_session, ENTRY, NOW, hc.load_cleaning_config())
    new = [row for row in db_session.scalars(select(News)) if row.record["title"] != STORED]
    assert len(new) == 1
    assert new[0].record["title"] == TITLES[1]
    assert result.cleaning["duplicate_llm"] == 2
    assert set(result.samples["duplicate_llm"]) == {TITLES[0], TITLES[2]}
    assert post.call_count == 1


def test_630_12_latest_100_history_titles(db_session: Session) -> None:
    profile(db_session)
    for i in range(120):
        stored(db_session, f"Historical public report {i}", i)
    candidate = items(["Broadcom signs semiconductor manufacturing agreement"])
    with (
        patch.object(cap, "sources_for", return_value=[("yahoo", lambda: candidate)]),
        patch.object(cap, "classify_headlines", side_effect=hc.classify_headlines),
        patch.object(httpx, "post", return_value=response([{"id": 0, "label": "keep"}])) as post,
    ):
        cap.collect_instrument_news(db_session, ENTRY, NOW, hc.load_cleaning_config())
    content = post.call_args.kwargs["json"]["messages"][1]["content"]
    history = content.splitlines()[1:101]
    assert history == [f"e{i}\tHistorical public report {i}" for i in range(100)]
    assert len(content.splitlines()) == 102
    assert "Historical public report 100" not in content


def test_630_12_jaccard_keeps_full_history(db_session: Session) -> None:
    profile(db_session)
    for i in range(120):
        stored(db_session, f"Historical public report {i}", i)
    stored(db_session, "Broadcom signs semiconductor manufacturing agreement", 120)
    with (
        patch.object(
            cap,
            "sources_for",
            return_value=[
                ("yahoo", lambda: items(["Broadcom signs semiconductor manufacturing agreement"]))
            ],
        ),
        patch.object(cap, "classify_headlines") as classifier,
    ):
        result = cap.collect_instrument_news(db_session, ENTRY, NOW, hc.load_cleaning_config())
    assert result.cleaning["duplicate_earlier"] == 1
    assert not result.leads
    classifier.assert_not_called()


def test_630_13_cross_source_order(db_session: Session) -> None:
    profile(db_session)
    a = [
        CollectedItem(
            "Broadcom opens a new factory", NOW.replace(hour=10), "https://fixture.example/a"
        ),
        CollectedItem(
            "Broadcom appoints chief financial officer",
            NOW.replace(hour=9),
            "https://fixture.example/c",
        ),
    ]
    b = [
        CollectedItem(
            "Broadcom reports quarterly earnings", NOW.replace(hour=8), "https://fixture.example/b"
        )
    ]
    with (
        patch.object(
            cap, "sources_for", return_value=[("yahoo", lambda: a), ("finnhub", lambda: b)]
        ),
        patch.object(
            cap, "classify_headlines", return_value=({0: "keep", 1: "keep", 2: "keep"}, 0, None)
        ) as classifier,
    ):
        cap.collect_instrument_news(db_session, ENTRY, NOW, hc.load_cleaning_config())
    assert [item.published_at.hour for item in classifier.call_args.args[0]] == [8, 9, 10]


def test_630_13_earliest_before_jaccard(db_session: Session) -> None:
    profile(db_session)
    title = "Broadcom signs semiconductor manufacturing agreement"
    late = CollectedItem(title, NOW, "https://fixture.example/late")
    early = CollectedItem(title, NOW.replace(hour=8), "https://fixture.example/early")
    with (
        patch.object(
            cap,
            "sources_for",
            return_value=[("yahoo", lambda: [late]), ("finnhub", lambda: [early])],
        ),
        patch.object(cap, "classify_headlines", return_value=({0: "keep"}, 0, None)),
    ):
        result = cap.collect_instrument_news(db_session, ENTRY, NOW, hc.load_cleaning_config())
    assert len(result.leads) == 1 and result.leads[0].url == early.url
    assert db_session.scalars(select(News)).one().published_at == early.published_at
    assert result.cleaning["duplicate"] == 1


def test_630_14_cross_batch_context(db_session: Session) -> None:
    profile(db_session)
    titles = [
        "Broadcom reports quarterly earnings",
        "Broadcom opens a new factory",
        "Broadcom appoints chief financial officer",
    ]
    candidates = items(titles)
    for i, candidate in enumerate(candidates):
        candidate.published_at = NOW - timedelta(minutes=3 - i)
    with (
        patch.object(
            cap,
            "get_settings",
            return_value=get_settings().model_copy(update={"INTEL_CLASSIFIER_BATCH": 2}),
        ),
        patch.object(cap, "sources_for", return_value=[("yahoo", lambda: candidates)]),
        patch.object(cap, "classify_headlines", side_effect=hc.classify_headlines),
        patch.object(
            httpx,
            "post",
            side_effect=[
                response([{"id": 0, "label": "keep"}, {"id": 1, "label": "mention"}]),
                response([{"id": 0, "label": "keep"}]),
            ],
        ) as post,
    ):
        result = cap.collect_instrument_news(db_session, ENTRY, NOW, hc.load_cleaning_config())
    assert post.call_count == 2 and len(result.leads) == 3
    content = post.call_args.kwargs["json"]["messages"][1]["content"]
    assert content.startswith(f"EXISTING:\ne0\t{titles[1]}\ne1\t{titles[0]}\n")


def test_630_16_classifier_failure_null_labels(db_session: Session) -> None:
    profile(db_session)
    with (
        patch.object(cap, "sources_for", return_value=[("yahoo", lambda: items())]),
        patch.object(
            cap, "classify_headlines", return_value=({}, 0, "classifier: ReadTimeout")
        ) as classifier,
    ):
        result = cap.collect_instrument_news(db_session, ENTRY, NOW, hc.load_cleaning_config())
    rows = db_session.scalars(select(News)).all()
    assert len(rows) == 3 and all(row.intel_label is None for row in rows)
    assert result.cleaning["stored_null_label"] == 3
    assert "duplicate_llm" not in result.cleaning
    assert result.errors == ["classifier: ReadTimeout"]
    assert classifier.call_count == 1


def test_630_17_filings_not_classified(db_session: Session) -> None:
    profile(db_session)
    filing = CollectedItem(
        "8-K Item 2.02 0000000000-26-000001",
        NOW,
        "https://fixture.example/filing",
        kind="filing",
        filing_form="8-K",
    )
    with (
        patch.object(
            cap, "sources_for", return_value=[("sec", lambda: [filing]), ("yahoo", lambda: items())]
        ),
        patch.object(
            cap,
            "classify_headlines",
            return_value=({0: "duplicate", 1: "duplicate", 2: "duplicate"}, 0, None),
        ) as classifier,
    ):
        result = cap.collect_instrument_news(db_session, ENTRY, NOW, hc.load_cleaning_config())
    assert db_session.scalars(select(News)).one().kind == "filing"
    assert result.cleaning["filings_stored"] == 1 and result.cleaning["duplicate_llm"] == 3
    assert all(
        item.kind != "filing" and item.title != filing.title
        for item in classifier.call_args.args[0]
    )


def test_630_09_invalid_config_slot_keeps_headlines(db_session: Session, tmp_path: Path) -> None:
    profile(db_session)
    data = load_intel_deepen_config().model_dump()
    data["body_cleaning"]["residue_line_patterns"] = ["["]
    path = tmp_path / "invalid.yml"
    path.write_text(yaml.safe_dump(data))

    def factory() -> Session:
        return Session(bind=db_session.connection(), join_transaction_mode="create_savepoint")

    with (
        patch.object(task, "SessionLocal", side_effect=factory),
        patch.object(cap, "SessionLocal", side_effect=factory),
        patch.object(task, "now_et", return_value=NOW),
        patch.object(task, "today_et", return_value=NOW.date()),
        patch.object(
            task, "load_intel_deepen_config", side_effect=lambda: load_intel_deepen_config(path)
        ),
        patch.object(task, "intel_universe", return_value=[ENTRY]),
        patch.object(cap, "intel_universe", return_value=[ENTRY]),
        patch.object(task, "resolve_profiles", return_value=[]),
        patch.object(task, "capture_news", return_value=PoolCaptureResult(0, [])),
        patch.object(
            cap,
            "get_settings",
            return_value=get_settings().model_copy(update={"INTEL_COLLECT_WORKERS": 1}),
        ),
        patch.object(cap, "sources_for", return_value=[("yahoo", lambda: items([TITLES[1]]))]),
        patch.object(cap, "classify_headlines", return_value=({0: "keep"}, 0, None)),
        patch.object(task, "send_ops_alert", return_value=True) as send,
    ):
        assert task.intel_slot_task("post_close") == {"status": "partial"}
    assert db_session.scalars(select(News)).one().record["title"] == TITLES[1]
    assert db_session.scalars(select(IntelSlotRun)).one().details["deepening_errors"] == [
        "deepening: ValueError"
    ]
    assert "Paid deepening: unexpected error (ValueError)" in send.call_args.args[1]


def test_630_21_spike_counts_stored_events(db_session: Session) -> None:
    from app.services.intel_signals import compute_signals

    profile(db_session)
    with (
        patch.object(cap, "sources_for", return_value=[("yahoo", lambda: items())]),
        patch.object(
            cap,
            "classify_headlines",
            return_value=({0: "keep", 1: "duplicate", 2: "duplicate"}, 0, None),
        ),
    ):
        cap.collect_instrument_news(db_session, ENTRY, NOW, hc.load_cleaning_config())
    signal = compute_signals(
        db_session,
        [ENTRY],
        NOW.date(),
        NOW - timedelta(hours=24),
        load_intel_deepen_config(),
        slot="post_close",
        now=NOW,
    )["AVGO"]
    assert signal.fresh == 1 and signal.filings == 0
    assert not signal.news_spike
