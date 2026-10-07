"""Slot pool macro labels and development-only consumers (#690)."""

import contextlib
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from typing import cast
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.intel import InstrumentProfile, IntelCollectionRun, NewsInstrument
from app.models.news import News
from app.services import headline_cleaning as hc
from app.services import intel_deepen as deepen
from app.services import news_capture as nc
from app.services import report_generator as rg
from app.services.headline_cleaning import MacroLabel
from app.services.instrument_news_sources import CollectedItem
from app.services.intel_deepen_config import load_intel_deepen_config
from app.services.intel_digest import build_batch_report
from app.services.intel_leads import Lead, select_leads
from app.services.intel_records import link_instrument, store_headline
from app.services.intel_selection import WorkUnit, select_units
from app.services.macro_detector import detect_macro_signals
from app.services.news_fetcher import FetchNewsResult, NewsItem
from app.tests.conftest import TEST_USER_ID, seed_user
from app.tests.test_intel_deepen_run import factory, settings
from app.tests.test_intel_paid import slot
from app.tests.test_intel_records import NOW, item


@pytest.fixture(autouse=True)
def no_live_calls() -> Iterator[None]:
    with (
        patch.object(hc, "openrouter_json", side_effect=AssertionError("mock classifier required")),
        patch.object(nc, "fetch_news", side_effect=AssertionError("mock RSS required")),
    ):
        yield


def label(kind: str = "development", call: str = "slot-call") -> MacroLabel:
    return cast(MacroLabel, {"type": kind, "importance": 2, "event": "event", "label_call": call})


def pool() -> list[NewsItem]:
    return [
        item(title, f"https://source{i}.example/story")
        for i, title in enumerate(
            [
                "Fed publishes inflation data",
                "Nvidia comments on Fed policy",
                "Fed official attends golf event",
                "Bakery opens downtown",
            ]
        )
    ]


def write_rows(session: Session, items: list[NewsItem], kinds: list[str | None]) -> None:
    for i, (headline, kind) in enumerate(zip(items, kinds, strict=True)):
        nid, _ = store_headline(session, headline, "pool", "article", None)
        row = session.get(News, nid)
        assert row is not None
        row.fetched_at = NOW
        if kind:
            row.record = {**row.record, **label(kind, f"call-{i}")}
    session.flush()


def worker(session: Session, *, weekend: bool = False) -> deepen.DeepenRun:
    return deepen.DeepenRun(
        session,
        slot(session),
        load_intel_deepen_config(),
        weekend,
        NOW,
        NOW - timedelta(days=1),
        [],
        {},
    )


def test_01_02_slot_labels_once_and_keeps_holding_link(db_session: Session) -> None:
    run = slot(db_session)
    db_session.add(InstrumentProfile(identifier="NVDA", market="US", aliases=["Nvidia"]))
    db_session.flush()
    headlines = pool()
    # A capture-node row is labeled on its first slot fetch, without rewriting its record.
    write_rows(db_session, headlines, [None] * 4)
    originals = {r.url_hash: dict(r.record) for r in db_session.scalars(select(News))}
    with (
        patch.object(nc, "fetch_news", return_value=FetchNewsResult(headlines, [])),
        patch.object(
            hc,
            "openrouter_json",
            return_value=(
                {
                    "labels": [
                        {"id": i, "type": kind, "importance": 2, "event": f"event-{i}"}
                        for i, kind in enumerate(["development", "commentary", "off_topic"])
                    ]
                },
                0.001,
            ),
        ) as classify,
    ):
        result = nc.capture_news(db_session, slot_run_id=run.id)
        assert classify.call_count == 1
        rows = list(db_session.scalars(select(News)))
        labeled = [r for r in rows if hc.macro_label(r.record)]
        assert len(labeled) == 3
        calls = {r.record["label_call"] for r in labeled}
        assert len(calls) == 1 and len(str(next(iter(calls)))) == 12
        for r in rows:
            assert {k: v for k, v in r.record.items() if k in originals[r.url_hash]} == originals[
                r.url_hash
            ]
        assert result.items == headlines
        assert db_session.scalars(select(NewsInstrument)).one().identifier == "NVDA"
        saved = {r.url_hash: dict(r.record) for r in rows}
        classify.reset_mock()
        nc.capture_news(db_session, slot_run_id=run.id)
        classify.assert_not_called()
        assert {r.url_hash: r.record for r in db_session.scalars(select(News))} == saved
    stats = list(
        db_session.scalars(select(IntelCollectionRun).order_by(IntelCollectionRun.started_at))
    )
    assert stats[0].stats["macro_classification"] == {
        "candidates": 3,
        "labeled": 3,
        "calls": 1,
        "failed_calls": 0,
        "cost_usd": 0.001,
        "development": 1,
        "commentary": 1,
        "off_topic": 1,
    }
    body = build_batch_report(db_session, run)[1]
    assert "Macro pool review:" in body.split("PART 2")[0]


def test_03_capture_node_never_classifies(db_session: Session) -> None:
    from app.tasks import API_QUIET_BEAT_ENTRIES

    with patch.object(nc, "fetch_news", return_value=FetchNewsResult(pool(), [])):
        assert nc.capture_news(db_session).inserted == 4
    assert all(hc.macro_label(r.record) is None for r in db_session.scalars(select(News)))
    entries = {k: v for k, v in API_QUIET_BEAT_ENTRIES.items() if k.startswith("capture-news-")}
    assert len(entries) == 16 and not any(entries.values())


@pytest.mark.parametrize(
    "output", [({}, 0.0, "classifier: ReadTimeout"), ({"labels": []}, 0.001, None)]
)
def test_04_failure_is_unlabeled_fallback(
    db_session: Session, output: tuple[dict[str, object], float, str | None]
) -> None:
    run = slot(db_session)
    with (
        patch.object(nc, "fetch_news", return_value=FetchNewsResult(pool()[:3], [])),
        patch.object(hc, "openrouter_json", side_effect=ValueError("invalid response"))
        if output[2]
        else patch.object(hc, "openrouter_json", return_value=(output[0], output[1])),
    ):
        nc.capture_news(db_session, slot_run_id=run.id)
    collection = db_session.scalars(select(IntelCollectionRun)).one()
    stats = collection.stats["macro_classification"]
    assert isinstance(stats, dict)
    assert stats["failed_calls"] == 1
    assert collection.status == "ok"
    assert all(hc.macro_label(r.record) is None for r in db_session.scalars(select(News)))
    with patch.object(deepen, "get_settings", return_value=settings()):
        # Reuse the existing slot row (worker helper would insert the same key).
        w = deepen.DeepenRun(
            db_session, run, load_intel_deepen_config(), False, NOW, NOW - timedelta(days=1), [], {}
        )
        with patch.object(w, "run_wave"):
            w.finish(db_session, {}, pool()[:3])
    assert w.theme_counts_development == w.theme_counts
    assert sum(w.theme_counts_development.values()) >= 3


def test_05_weekday_counts_and_zero_theme(db_session: Session) -> None:
    headlines = [item(f"Fed event {i}", f"https://source{i}.example/a") for i in range(4)]
    headlines.append(item("Oil supply outlook", "https://oil.example/a"))
    write_rows(
        db_session,
        headlines,
        ["development", "development", "commentary", "off_topic", "commentary"],
    )
    raw = detect_macro_signals(headlines, max_articles_per_theme=5)
    fed_theme = next(h.theme for h in raw.hits if len(h.articles) == 4)
    oil_theme = next(h.theme for h in raw.hits if headlines[-1] in h.articles)
    with patch.object(deepen, "get_settings", return_value=settings()):
        w = worker(db_session)
        with patch.object(w, "run_wave") as wave:
            w.finish(db_session, {}, headlines)
    assert w.theme_counts[fed_theme] == 4
    assert w.theme_counts_development[fed_theme] == 2
    assert w.theme_counts_development[oil_theme] == 0
    assert all(u.theme != oil_theme for u in wave.call_args.args[0])
    assert any(u.theme == fed_theme for u in wave.call_args.args[0])
    assert not select_units({}, {"empty": 0}, load_intel_deepen_config())


@pytest.mark.parametrize("development,expected", [(0, False), (1, True)])
def test_06_weekend_requires_raw_gate_and_development(development: int, expected: bool) -> None:
    cfg = load_intel_deepen_config()
    assert (
        bool(
            select_units(
                {},
                {"energy": 14},
                cfg,
                weekend=True,
                theme_history={"energy": [3, 4, 4, 5, 6]},
                macro_development_counts={"energy": development},
            )
        )
        == expected
    )
    assert not select_units(
        {},
        {"energy": 14},
        cfg,
        weekend=True,
        theme_history={"energy": [10, 12, 14, 15, 16]},
        macro_development_counts={"energy": 1},
    )


def test_07_all_stored_labels_reused_and_holding_path_ignores_them(db_session: Session) -> None:
    headlines = pool()[:3]
    write_rows(db_session, headlines, ["development", "commentary", "off_topic"])
    with (
        patch.object(deepen, "get_settings", return_value=settings()),
        patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
        patch.object(
            deepen, "classify_macro", side_effect=AssertionError("stored labels must be reused")
        ),
    ):
        w = worker(db_session)
        w.pool_items = list(headlines)
        batches: list[tuple[WorkUnit, Lead]] = []
        theme = detect_macro_signals(headlines).hits[0].theme
        with patch.object(w, "_extract_batch", side_effect=lambda p, b: batches.extend(b)):
            w.run_wave([WorkUnit("macro", theme=theme, providers=("tavily",))], {}, {})
        w.close()
    assert [lead.title for _, lead in batches] == [headlines[0].title]
    assert batches[0][1].macro_label == label("development", "call-0")
    assert w.macro_rank_failed == w.macro_rank_partial == 0
    # Even an off-topic macro headline remains an instrument extraction candidate.
    assert (
        len(
            select_leads(
                db_session,
                WorkUnit("mover", "Fed"),
                [CollectedItem(headlines[2].title, NOW, headlines[2].url)],
                ["Fed"],
                load_intel_deepen_config(),
                NOW,
            )
        )
        == 1
    )


@pytest.mark.parametrize("same_call,expected", [(True, 1), (False, 2)])
def test_08_event_scope(db_session: Session, same_call: bool, expected: int) -> None:
    items = [
        CollectedItem("Factory output expands", NOW, "https://one.example/a"),
        CollectedItem("Central bank announces policy decision", NOW, "https://two.example/a"),
    ]
    labels = {0: label(), 1: label(call="slot-call" if same_call else "other-call")}
    leads = select_leads(
        db_session,
        WorkUnit("macro", theme="economy"),
        items,
        [],
        load_intel_deepen_config(),
        NOW,
        macro_labels=labels,
    )
    assert len(leads) == expected


@pytest.mark.parametrize("same_url", [False, True], ids=["different-url", "same-url"])
@pytest.mark.parametrize("different_event", [False, True], ids=["same-event", "different-event"])
def test_08_cross_call_e1(db_session: Session, same_url: bool, different_event: bool) -> None:
    items = [
        CollectedItem("Factory output expands", NOW, "https://one.example/a"),
        CollectedItem("Factory output expands!", NOW, "https://two.example/a"),
    ]
    if same_url:
        items[1] = replace(items[1], title="Unrelated wording", url=items[0].url)
    assert (
        len(
            select_leads(
                db_session,
                WorkUnit("macro", theme="economy"),
                items,
                [],
                load_intel_deepen_config(),
                NOW,
                macro_labels={
                    0: label(),
                    1: {
                        **label(call="other"),
                        "event": "other-event" if different_event else "event",
                    },
                },
            )
        )
        == 1
    )


def test_09_report_filters_macro_only(db_session: Session) -> None:
    from app.tests.test_report_generator import _normal_path_patches

    seed_user(db_session, TEST_USER_ID)
    headline = item("AAPL Fed outlook", "https://commentary.example/a")
    write_rows(db_session, [headline], ["commentary"])
    row = db_session.scalars(select(News)).one()
    link_instrument(db_session, row.id, "AAPL")
    db_session.flush()
    with contextlib.ExitStack() as stack:
        for i, patcher in enumerate(_normal_path_patches()):
            if i not in (1, 2):
                stack.enter_context(cast(contextlib.AbstractContextManager[object], patcher))
        stack.enter_context(patch.object(rg, "intel_trade_date", return_value=NOW.date()))
        report = rg.generate_report(db_session, user_id=TEST_USER_ID, now=NOW, output_lang="en")
    assert report.status == "success"
    assert report.report_inputs is not None
    assert report.report_inputs["macro_signals"]["hits"] == []
    assert report.report_inputs["holding_news"]["AAPL"][0]["title"] == headline.title
    assert report.report_inputs["news_items"][0]["title"] == headline.title


def test_10_prompt_targeted_invariants() -> None:
    lines = hc.MACRO_PROMPT.splitlines()
    assert "for one macro theme" not in lines[0]
    assert (
        lines[-2]
        == "Do not provide investment advice. Stay within Layer 3: facts, contextual relationships and observable signals, never instructions, price targets or forecasts."
    )
    assert (
        lines[-1]
        == 'Output ONLY JSON: {"labels": [{"id": int, "type": "development|commentary|off_topic", "importance": 1|2|3, "event": "short-slug"}]}'
    )


@pytest.mark.parametrize(
    "kind,expected", [("commentary", False), ("development", True), (None, True)]
)
def test_06_weekend_fresh_gate_excludes_refetched_development(
    db_session: Session, kind: str | None, expected: bool
) -> None:
    headlines = [item(f"Fed event {i}", f"https://fresh{i}.example/a") for i in range(14)]
    old = item("Fed publishes older data", "https://old.example/a")
    write_rows(db_session, [*headlines, old], [kind, *(["commentary"] * 13), "development"])
    row = db_session.scalar(select(News).where(News.url_hash == old.url_hash))
    assert row is not None
    row.fetched_at = NOW - timedelta(days=2)
    db_session.flush()
    theme = detect_macro_signals(headlines).hits[0].theme
    with patch.object(deepen, "get_settings", return_value=settings()):
        w = worker(db_session, weekend=True)
        with patch.object(w, "run_wave") as wave:
            w.finish(db_session, {}, [*headlines, old])
    assert w.theme_counts[theme] == 14
    assert w.theme_counts_development[theme] == int(expected)
    assert bool(wave.call_args.args[0]) == expected


def test_d1_first_theme_groups_chunks_partial_and_url_free_event(db_session: Session) -> None:
    run = slot(db_session)
    headlines = [item(f"Fed Trump event {i}", f"https://fixture.example/{i}") for i in range(31)]
    headlines.append(item("Oil supply disruption", "https://oil.example/a"))
    sizes: list[int] = []

    def classify(system: str, content: str) -> tuple[dict[str, object], float]:
        size = len(content.splitlines())
        sizes.append(size)
        return {
            "labels": [
                {
                    "id": i,
                    "type": "development",
                    "importance": 2,
                    "event": "oil https://private.example/a" if size == 1 else "event",
                }
                for i in range(size)
                if i != 29
            ]
        }, 0.001

    with (
        patch.object(nc, "fetch_news", return_value=FetchNewsResult(headlines, [])),
        patch.object(hc, "openrouter_json", side_effect=classify),
    ):
        nc.capture_news(db_session, slot_run_id=run.id)
    stats = db_session.scalars(select(IntelCollectionRun)).one().stats["macro_classification"]
    assert sizes == [30, 1, 1]
    assert isinstance(stats, dict)
    assert stats["candidates"] == 32 and stats["labeled"] == 31 and stats["failed_calls"] == 0
    labels = [r.record for r in db_session.scalars(select(News)) if hc.macro_label(r.record)]
    assert len({r["label_call"] for r in labels}) == 3
    assert all("http" not in str(r) for r in labels)


@pytest.mark.parametrize(
    "output,error,failed,partial",
    [
        ({}, "classifier: ReadTimeout", 1, 0),
        ({0: {"type": "development", "importance": 3, "event": "new"}}, None, 0, 1),
    ],
)
def test_d3_mixed_labels_only_classify_remainder(
    db_session: Session, output: dict[int, MacroLabel], error: str | None, failed: int, partial: int
) -> None:
    headlines = [
        item(title, f"https://source{i}.example/a")
        for i, title in enumerate(
            [
                "Fed announces bank policy decision",
                "Fed professor interview outlook",
                "Fed publishes factory output survey",
                "Fed releases inflation figures",
            ]
        )
    ]
    write_rows(db_session, headlines, ["development", "commentary", None, None])
    with (
        patch.object(deepen, "get_settings", return_value=settings()),
        patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
        patch.object(deepen, "classify_macro", return_value=(output, 0.001, error)) as classifier,
    ):
        w = worker(db_session)
        w.pool_items = list(headlines)
        batches: list[tuple[WorkUnit, Lead]] = []
        theme = detect_macro_signals(headlines).hits[0].theme
        with patch.object(w, "_extract_batch", side_effect=lambda p, b: batches.extend(b)):
            w.run_wave([WorkUnit("macro", theme=theme, providers=("tavily",))], {}, {})
        w.close()
    assert classifier.call_count == 1
    assert {x.title for x in classifier.call_args.args[0]} == {x.title for x in headlines[2:]}
    assert w.macro_rank_failed == failed and w.macro_rank_partial == partial
    assert headlines[1].title not in [lead.title for _, lead in batches]
    assert {headline.title for headline in headlines[2:]} <= {lead.title for _, lead in batches}
    stored = next(lead for _, lead in batches if lead.title == headlines[0].title)
    assert stored.macro_label == label(call="call-0")
    for _, lead in batches:
        if lead.macro_label and lead.title != stored.title:
            assert len(lead.macro_label["label_call"]) == 12
