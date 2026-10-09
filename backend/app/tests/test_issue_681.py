"""Issue #681 acceptance; every LLM and paid provider is mocked."""

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import httpx
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.core.config import get_settings
from app.core.timezones import ET
from app.models.intel import InstrumentProfile, IntelSlotRun, NewsInstrument
from app.models.news import News
from app.models.paid_intel import IntelArticle
from app.services import headline_cleaning as hc
from app.services import instrument_news_capture as capture
from app.services import intel_deepen as deepen
from app.services import intel_name_check as name_check
from app.services.instrument_news_sources import CollectedItem
from app.services.instrument_profiles import clean_name, resolve_profiles
from app.services.instrument_relations import Relation, load_relations
from app.services.instrument_universe import UniverseEntry
from app.services.intel_deepen_config import load_intel_deepen_config
from app.services.intel_leads import select_leads, stored_headlines
from app.services.intel_selection import WorkUnit
from app.services.intel_signals import compute_signals
from app.services.news_fetcher import NewsItem
from app.services.report_generator import _append_related
from app.services.report_prompts import _build_holding_news_block
from app.services.window_data import load_related_news_by_identifier
from app.tests.test_agent_api import bearer
from app.tests.test_agent_pull import INTEL, seed_intel
from app.tests.test_agent_pull import clock as agent_clock  # noqa: F401
from app.tests.test_agent_pull import token as agent_token  # noqa: F401
from app.tests.test_intel_paid import slot
from app.tests.test_issue_630_classifier import response
from app.tests.test_issue_635_deepening import worker as shared_worker  # noqa: F401
from app.tests.test_issue_635_report import collection, report

NOW = datetime(2026, 10, 2, 16, 15, tzinfo=ET)
TSMC = Relation("TSMC", ("TSMC", "Taiwan Semiconductor"), "competitor")


@pytest.fixture
def worker(request: pytest.FixtureRequest) -> deepen.DeepenRun:
    return cast(deepen.DeepenRun, request.getfixturevalue("shared_worker"))


def stored(
    session: Session,
    title: str,
    published: datetime,
    linked: datetime,
    *,
    identifier: str = "AAA",
    kind: str = "article",
    label: str | None = "keep",
    relation: str | None = None,
) -> News:
    row = News(
        url_hash=hashlib.md5(title.encode()).hexdigest()[:16],
        record={"title": title},
        kind=kind,
        intel_label=label,
        published_at=published,
        fetched_at=linked,
    )
    session.add(row)
    session.flush()
    session.add(
        NewsInstrument(
            news_id=row.id,
            identifier=identifier,
            created_at=linked,
            relation=relation,
            related_to="TSMC" if relation else None,
        )
    )
    session.flush()
    return row


def labelled(title: str, host: str, minutes: int, label: str | None) -> CollectedItem:
    item = CollectedItem(title, NOW - timedelta(minutes=minutes), f"https://{host}/{minutes}")
    item.label = label
    return item


# Part 1


def test_681_01_direct_leads_rank_keep_first(db_session: Session) -> None:
    unit = WorkUnit("mover", "AAA", window_start=NOW.date() - timedelta(days=1))
    cfg = load_intel_deepen_config()
    items = [
        labelled("AAA futures roundup", "benzinga.com", 1, "mention"),
        labelled("AAA IPO league table", "investing.com", 2, "mention"),
        labelled("AAA confirms plant talks", "finance.yahoo.com", 3, "keep"),
        labelled("AAA ends higher on fuel plans", "reuters.com", 4, "keep"),
    ]
    leads = select_leads(db_session, unit, items, ["AAA"], cfg, NOW)
    assert [x.title for x in leads] == ["AAA ends higher on fuel plans", "AAA confirms plant talks"]
    items = [
        labelled("AAA roundup", "benzinga.com", 1, "mention"),
        labelled("AAA pool story", "cnbc.com", 2, None),
    ]
    leads = select_leads(db_session, unit, items, ["AAA"], cfg, NOW)
    assert [x.title for x in leads] == ["AAA pool story", "AAA roundup"]


# Part 2


def first_note(worker: deepen.DeepenRun) -> object:
    return cast(list[dict[str, object]], worker.details()["outcomes"])[0]["note"]


def search_payloads(worker: deepen.DeepenRun, unit: WorkUnit) -> list[dict[str, Any]]:
    with patch(
        "app.services.paid_search.post", return_value=httpx.Response(200, json={"results": []})
    ) as post:
        worker.run_wave([unit], {}, {"AAA": ["AAA"]})
    return [c.kwargs["json"] for c in post.call_args_list]


def test_681_02_mover_searches_earlier_keep_headlines(
    db_session: Session, worker: deepen.DeepenRun
) -> None:
    stored(db_session, "AAA wins a large order", NOW - timedelta(days=1), NOW - timedelta(days=1))
    stored(db_session, "AAA short report", NOW - timedelta(days=2), NOW - timedelta(days=2))
    unit = WorkUnit(
        "mover", "AAA", window_start=NOW.date() - timedelta(days=5), providers=("tavily",)
    )
    payloads = search_payloads(worker, unit)
    assert [p["query"] for p in payloads] == ["AAA wins a large order", "AAA short report"]
    assert first_note(worker) != "no_news"


def test_681_03_accepted_headline_not_searched_again(
    db_session: Session, worker: deepen.DeepenRun
) -> None:
    row = stored(db_session, "AAA wins a large order", NOW - timedelta(days=1), NOW - timedelta(1))
    stored(db_session, "AAA short report", NOW - timedelta(days=2), NOW - timedelta(days=2))
    db_session.add(
        IntelArticle(
            slot_run_id=db_session.query(IntelSlotRun).one().id,
            provider="tavily",
            url_key="k",
            news_id=row.id,
            status="accepted",
            record={"v": 1, "kind": "instrument", "title": "t", "body": "b"},
            body_chars=1,
            fetched_at=NOW,
        )
    )
    db_session.flush()
    unit = WorkUnit(
        "quiet",
        "AAA",
        reason="near_d3 +8.5%",
        window_start=NOW.date() - timedelta(days=3),
        providers=("tavily",),
    )
    assert [p["query"] for p in search_payloads(worker, unit)] == ["AAA short report"]


@pytest.mark.parametrize(
    "case", ["current", "before_window", "mention", "filing", "related", "no_alias"]
)
def test_681_04_stored_headline_exclusions(db_session: Session, case: str) -> None:
    published, linked = NOW - timedelta(days=1), NOW - timedelta(days=1)
    title = "AAA wins a large order" if case != "no_alias" else "Chip stocks rally"
    stored(
        db_session,
        title,
        NOW - timedelta(days=9) if case == "before_window" else published,
        NOW if case == "current" else linked,
        kind="filing" if case == "filing" else "article",
        label="mention" if case == "mention" else "keep",
        relation="competitor" if case == "related" else None,
    )
    unit = WorkUnit("mover", "AAA", window_start=NOW.date() - timedelta(days=5))
    assert stored_headlines(db_session, unit, ["AAA"], load_intel_deepen_config(), NOW) == []


@pytest.mark.parametrize("reason", ["new_filing", "news_spike 3.0x"])
def test_681_05_filing_and_spike_units_stay_fresh_only(
    db_session: Session, worker: deepen.DeepenRun, reason: str
) -> None:
    stored(db_session, "AAA wins a large order", NOW - timedelta(days=1), NOW - timedelta(days=1))
    unit = WorkUnit("quiet", "AAA", reason=reason, providers=("tavily",))
    assert search_payloads(worker, unit) == []
    assert first_note(worker) == "no_news"


def test_681_05b_search_cap_reached(db_session: Session, worker: deepen.DeepenRun) -> None:
    stored(db_session, "AAA wins a large order", NOW - timedelta(days=1), NOW - timedelta(days=1))
    worker.searches = worker.search_cap
    unit = WorkUnit("mover", "AAA", providers=("tavily",))
    assert search_payloads(worker, unit) == []
    assert first_note(worker) == "cap_reached"


# Part 3


def test_681_06_suffix_without_space() -> None:
    assert clean_name("Ningbo Tuopu Group Co.,Ltd", "601689") == "Ningbo Tuopu Group"
    assert clean_name("Lumentum Holdings Inc.", "LITE") == "Lumentum"
    assert clean_name("Micron Technology, Inc.", "MU") == "Micron Technology"
    assert clean_name("ASML Holding N.V.", "ASML") == "ASML"


def test_681_07_profiles_take_new_aliases(db_session: Session) -> None:
    for identifier, name in (("AVGO", "Broadcom Inc."), ("SPCX", "SpaceX")):
        db_session.add(
            InstrumentProfile(
                identifier=identifier,
                market="US",
                name_en=name,
                aliases=[name, identifier],
                name_source="yfinance" if identifier == "AVGO" else "config",
                name_resolved_at=NOW,
            )
        )
    db_session.flush()
    entries = [UniverseEntry("AVGO", "AVGO", "US"), UniverseEntry("SPCX", "SPCX", "US")]
    assert resolve_profiles(db_session, entries, now=NOW + timedelta(hours=1)) == []
    avgo = db_session.get(InstrumentProfile, "AVGO")
    spcx = db_session.get(InstrumentProfile, "SPCX")
    assert avgo is not None and spcx is not None
    assert (avgo.name_en, avgo.name_source) == ("Broadcom", "config")
    assert "VMware" in avgo.aliases and "Starlink" in spcx.aliases


# Part 4


@pytest.mark.parametrize(
    "entries",
    [
        [{"name": f"E{i}", "aliases": [f"E{i}"], "relation": "competitor"} for i in range(9)],
        [{"name": "E", "aliases": [], "relation": "competitor"}],
        [{"name": "E", "aliases": ["E"], "relation": "partner"}],
    ],
)
def test_681_08_relations_validation(tmp_path: Path, entries: list[dict[str, object]]) -> None:
    path = tmp_path / "relations.yml"
    path.write_text(json.dumps({"relations": {"AAA": entries}}))
    with pytest.raises(ValueError):
        load_relations(path)


def test_681_08b_repository_relations_load() -> None:
    relations = load_relations()
    assert relations["PSH.L"] == []
    assert any(r.name == "TSMC" and r.relation == "supplier" for r in relations["NVDA"])
    assert all(len(v) <= 8 for v in relations.values())


def collect(
    session: Session, items: list[CollectedItem], related: dict[int, dict[str, str]] | None
) -> capture.InstrumentResult:
    labels = related or {}
    with (
        patch.object(capture, "sources_for", return_value=[("finnhub", lambda: items)]),
        patch.object(capture, "classify_headlines", return_value=({}, 0.0, None)),
        patch.object(
            capture,
            "classify_related",
            return_value=(labels, 0.002, None),
        ) as related_classifier,
    ):
        result = capture.collect_instrument_news(
            session,
            UniverseEntry("INTC", "INTC", "US"),
            NOW,
            hc.load_cleaning_config(),
            relations=[TSMC],
        )
    result.classifier["related_calls"] = related_classifier.call_count
    return result


def test_681_09_related_link_stored_and_capped(db_session: Session) -> None:
    db_session.add(InstrumentProfile(identifier="INTC", market="US", aliases=["Intel", "INTC"]))
    db_session.flush()
    items = [
        CollectedItem(
            f"TSMC update number {i} on {word}", NOW - timedelta(hours=i), f"https://x/{i}"
        )
        for i, word in enumerate(["Terafab", "pricing", "capacity", "Arizona", "2nm"])
    ]
    labels = {i: {"label": "keep", "entity": "TSMC"} for i in range(4)}
    labels[4] = {"label": "drop", "entity": "TSMC"}
    result = collect(db_session, items, labels)
    links = db_session.query(NewsInstrument).filter_by(identifier="INTC").all()
    assert len(links) == 3
    assert {(x.relation, x.related_to) for x in links} == {("competitor", "TSMC")}
    assert result.cleaning["related_kept"] == 3
    assert result.cleaning["related_capped"] == 1
    assert result.cleaning["related_dropped_llm"] == 1
    assert result.cleaning.get("unrelated_rule", 0) == 0
    assert result.leads == []


def test_681_09b_related_classifier_failure_stores_nothing(db_session: Session) -> None:
    items = [CollectedItem("TSMC raises prices", NOW, "https://x/1")]
    with (
        patch.object(capture, "sources_for", return_value=[("finnhub", lambda: items)]),
        patch.object(capture, "classify_related", return_value=({}, 0.0, "classifier: HTTPError")),
    ):
        result = capture.collect_instrument_news(
            db_session,
            UniverseEntry("INTC", "INTC", "US"),
            NOW,
            hc.load_cleaning_config(),
            relations=[TSMC],
        )
    assert db_session.query(NewsInstrument).count() == 0
    assert result.cleaning["related_dropped_failed"] == 1
    assert "classifier: HTTPError" in result.errors


def test_681_10_no_relation_hit_is_not_classified(db_session: Session) -> None:
    unmatched: list[CollectedItem] = []
    items = [CollectedItem("Chip stocks rally on Monday", NOW, "https://x/1")]
    with (
        patch.object(capture, "sources_for", return_value=[("finnhub", lambda: items)]),
        patch.object(capture, "classify_related") as related_classifier,
    ):
        result = capture.collect_instrument_news(
            db_session,
            UniverseEntry("INTC", "INTC", "US"),
            NOW,
            hc.load_cleaning_config(),
            relations=[TSMC],
            unmatched=unmatched,
        )
    assert related_classifier.call_count == 0
    assert result.cleaning["unrelated_rule"] == 1
    assert [x.title for x in unmatched] == ["Chip stocks rally on Monday"]


def test_681_10b_related_classifier_prompt() -> None:
    item = CollectedItem("TSMC raises prices", NOW, "https://x/1", summary="Foundry pricing")
    with patch.object(
        httpx,
        "post",
        return_value=response([{"id": 0, "label": "keep", "entity": "TSMC"}]),
    ) as post:
        labels, cost, error = hc.classify_related([item], "INTC", ["Intel"], [[TSMC]])
    payload = post.call_args.kwargs["json"]
    assert payload["provider"] == {"data_collection": "deny"}
    user = payload["messages"][1]["content"]
    assert "related: TSMC (competitor)" in user and "TSMC raises prices" in user
    assert labels == {0: {"label": "keep", "entity": "TSMC"}} and error is None and cost


def test_681_11_related_links_excluded_from_signals_and_leads(db_session: Session) -> None:
    stored(db_session, "TSMC raises prices", NOW, NOW, relation="competitor")
    signals = compute_signals(
        db_session,
        [UniverseEntry("AAA", "AAA", "US")],
        NOW.date(),
        NOW - timedelta(hours=8),
        load_intel_deepen_config(),
        slot="post_close",
        now=NOW,
        weekend=False,
    )
    assert signals["AAA"].fresh == 0


def test_681_11b_agent_pull_excludes_related(
    app_client: TestClient, db_session: Session, request: pytest.FixtureRequest
) -> None:
    token = request.getfixturevalue("agent_token")
    seed_intel(db_session)
    from app.tests.test_agent_pull import NOW as AGENT_NOW

    row = News(
        url_hash="related-0",
        origin="instrument",
        published_at=AGENT_NOW.replace(hour=1),
        record={"title": "TSMC raises prices"},
    )
    db_session.add(row)
    db_session.flush()
    db_session.add(
        NewsInstrument(news_id=row.id, identifier="NVDA", relation="supplier", related_to="TSMC")
    )
    db_session.commit()
    body = app_client.get(
        INTEL, params={"date": AGENT_NOW.date().isoformat()}, headers=bearer(token)
    ).json()
    titles = [h["title"] for h in body["holdings"][0]["headlines"]]
    assert "TSMC raises prices" not in titles


def news(title: str, minutes: int) -> NewsItem:
    return NewsItem(
        hashlib.md5(title.encode()).hexdigest()[:16],
        title,
        "",
        "",
        NOW - timedelta(minutes=minutes),
        None,
    )


def test_681_12_report_appends_two_related_after_direct() -> None:
    direct = {"AAA": [news(f"AAA direct {i}", i) for i in range(6)]}
    related = {
        "AAA": [(news(f"TSMC item {i}", i), "TSMC", "supplier") for i in range(3)],
        "BBB": [(news("TSMC only item", 1), "TSMC", "customer")],
    }
    merged, extra = _append_related(direct, related, ["AAA", "BBB"])
    assert [x["title"] for x in merged["AAA"]][6:] == ["TSMC item 0", "TSMC item 1"]
    assert merged["AAA"][6]["related"] == "TSMC (supplier)"
    assert list(merged) == ["AAA", "BBB"]
    assert merged["BBB"][0]["related"] == "TSMC (customer)"
    assert {k: len(v) for k, v in extra.items()} == {"AAA": 2, "BBB": 1}
    block = _build_holding_news_block(merged)
    assert "  [related company: TSMC (supplier)] TSMC item 0" in block
    assert "are about that company, not the holding" in block


def test_681_12b_load_related_news(db_session: Session) -> None:
    from app.tests.conftest import TEST_USER_ID, seed_user

    seed_user(db_session, TEST_USER_ID)
    stored(db_session, "TSMC raises prices", NOW, NOW, relation="supplier")
    stored(db_session, "AAA direct story", NOW, NOW)
    rows = load_related_news_by_identifier(
        db_session, NOW - timedelta(days=1), NOW + timedelta(hours=1), TEST_USER_ID, ["AAA"]
    )
    assert [(x.title, name, rel) for x, name, rel in rows["AAA"]] == [
        ("TSMC raises prices", "TSMC", "supplier")
    ]


# Migration


def test_681_13_migration_round_trip(alembic_cfg: Config) -> None:
    command.upgrade(alembic_cfg, "head")
    engine = create_engine(get_settings().database_url)
    insert = text(
        "INSERT INTO news_instruments (news_id, identifier, relation, related_to) "
        "VALUES (:nid, 'AAA', :relation, :related_to)"
    )
    try:
        with engine.begin() as conn:
            nid = conn.execute(
                text(
                    "INSERT INTO news (url_hash, published_at, record) "
                    "VALUES ('m681', now(), '{\"title\": \"x\"}') RETURNING id"
                )
            ).scalar_one()
        for relation, related_to in (("supplier", None), ("partner", "TSMC")):
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(insert, {"nid": nid, "relation": relation, "related_to": related_to})
        with engine.begin() as conn:
            conn.execute(insert, {"nid": nid, "relation": "supplier", "related_to": "TSMC"})
        command.downgrade(alembic_cfg, "d67500000001")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM news_instruments")).scalar_one() == 0
        command.upgrade(alembic_cfg, "head")
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM news WHERE url_hash='m681'"))
        engine.dispose()


# Part 5


def test_681_14_weekly_due_only_sunday_pre_open() -> None:
    assert name_check.weekly_due("pre_open", date(2026, 10, 4))
    assert not name_check.weekly_due("post_close", date(2026, 10, 4))
    assert not name_check.weekly_due("pre_open", date(2026, 10, 5))


def gap_reply(labels: list[dict[str, object]]) -> httpx.Response:
    return response(labels)


def test_681_14b_weekly_check_findings_and_suggestions(db_session: Session) -> None:
    db_session.add_all(
        [
            InstrumentProfile(identifier="SPCX", market="US", name_en="SpaceX", aliases=["SpaceX"]),
            InstrumentProfile(identifier="NEW", market="US", name_en="Newco", aliases=["Newco"]),
        ]
    )
    db_session.flush()
    unmatched = {
        "SPCX": [CollectedItem(f"Starlink headline {i}", NOW, f"https://x/{i}") for i in range(4)]
    }
    gap = gap_reply(
        [
            {"id": 0, "label": "named", "name": "Starlink"},
            {"id": 1, "label": "indirect", "entity": "Rocket Lab", "relation": "competitor"},
            {"id": 2, "label": "indirect", "entity": "Rocket Lab", "relation": "competitor"},
            {"id": 3, "label": "indirect", "entity": "Blue Origin", "relation": "competitor"},
        ]
    )
    suggestion = httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "aliases": ["Newco Labs"],
                                "relations": [
                                    {
                                        "name": "Oldco",
                                        "aliases": ["Oldco"],
                                        "relation": "competitor",
                                    }
                                ],
                            }
                        )
                    }
                }
            ],
            "usage": {"cost": 0.001},
        },
        request=httpx.Request("POST", "https://fixture.example"),
    )
    with patch.object(httpx, "post", side_effect=[gap, suggestion]) as post:
        details = name_check.run_weekly_check(
            db_session,
            unmatched,
            [UniverseEntry("SPCX", "SPCX", "US"), UniverseEntry("NEW", "NEW", "US")],
            {"SPCX": []},
        )
    assert post.call_count == 2
    assert details["names"] == [
        {"identifier": "SPCX", "name": "Starlink", "count": 1, "samples": ["Starlink headline 0"]}
    ]
    gaps = cast(list[dict[str, object]], details["relations"])
    assert [(r["entity"], r["count"]) for r in gaps] == [("Rocket Lab", 2)]
    assert details["unreviewed"] == ["NEW"]
    suggestions = cast(dict[str, dict[str, object]], details["suggestions"])
    assert suggestions["NEW"]["aliases"] == ["Newco Labs"]
    lines = name_check.render_weekly(details)
    assert lines[0] == "PART 3 - WEEKLY NAME AND RELATION CHECK"
    assert '  SPCX: "Starlink" (1 headlines)' in lines
    assert "  SPCX: Rocket Lab, competitor (2 headlines)" in lines
    assert any("Newco Labs" in line for line in lines)
    assert lines[-1] == "Problems: none"


def test_681_14c_weekly_failure_is_reported_not_raised(db_session: Session) -> None:
    unmatched = {"AAA": [CollectedItem("Headline", NOW, "https://x/1")]}
    with patch.object(httpx, "post", side_effect=httpx.ConnectError("down")):
        details = name_check.run_weekly_check(
            db_session, unmatched, [UniverseEntry("AAA", "AAA", "US")], {"AAA": []}
        )
    assert details["errors"] == ["classifier: ConnectError"]
    assert name_check.render_weekly(details)[-2:] == [
        "Problems:",
        "  AI review: AI review failed (1 times)",
    ]


def test_681_15_batch_report_related_line_and_part3(db_session: Session) -> None:
    run = slot(db_session)
    collection(
        db_session,
        run,
        {"cleaning": {"kept": 2, "related_candidates": 5, "related_kept": 2, "related_capped": 1}},
    )
    body = report(db_session, run)[1]
    assert (
        "Related-company headlines: 5 checked by AI, 2 kept (1 over the per-company limit)." in body
    )
    assert "PART 3" not in body
    run.details = {
        **run.details,
        "weekly_check": {
            "checked": 0,
            "calls": 0,
            "cost_usd": 0,
            "names": [],
            "relations": [],
            "unreviewed": [],
            "suggestions": {},
            "errors": [],
        },
    }
    assert "PART 3 - WEEKLY NAME AND RELATION CHECK" in report(db_session, run)[1]
