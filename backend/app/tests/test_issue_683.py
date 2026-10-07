"""Issue #683 acceptance: relation schema and suggested YAML round trips."""

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import yaml
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.timezones import ET
from app.models.intel import InstrumentProfile, IntelSlotRun, NewsInstrument
from app.models.news import News
from app.services import instrument_news_capture as capture
from app.services import instrument_relations
from app.services import intel_name_check as name_check
from app.services.instrument_news_sources import CollectedItem
from app.services.instrument_universe import UniverseEntry

NOW = datetime(2026, 10, 2, 16, 15, tzinfo=ET)
MARKER = "Suggested YAML (review before adding through a PR):"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Unexpected external HTTP call")

    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx, "post", forbidden)


@pytest.mark.parametrize(
    "document",
    [
        "- NVDA\n",
        "scalar\n",
        "false\n",
        "{}\n",
        "relations: []\n",
        "relations: {123: []}\n",
        "relations: {'': []}\n",
        "relations: {'   ': []}\n",
        "relations: {NVDA: null}\n",
        "relations: {NVDA: false}\n",
        "relations: {NVDA: {}}\n",
        "relations: {NVDA: x}\n",
        "relations: {NVDA: 123}\n",
        yaml.safe_dump(
            {"relations": {"NVDA": [{"name": "E", "aliases": ["E"], "relation": "supplier"}] * 9}}
        ),
    ],
    ids=[
        "document-list",
        "document-scalar",
        "document-false",
        "missing-relations",
        "relations-list",
        "integer-key",
        "empty-key",
        "blank-key",
        "null-value",
        "false-value",
        "mapping-value",
        "string-value",
        "number-value",
        "nine-entries",
    ],
)
def test_683_01_invalid_relation_schema(tmp_path: Path, document: str) -> None:
    path = tmp_path / "relations.yml"
    path.write_text(document, encoding="utf-8")
    with pytest.raises(ValueError):
        instrument_relations.load_relations(path)


def test_683_01b_explicit_empty_list_is_reviewed(tmp_path: Path) -> None:
    path = tmp_path / "relations.yml"
    path.write_text("relations: {NVDA: []}\n", encoding="utf-8")
    assert instrument_relations.load_relations(path) == {"NVDA": []}


def test_683_02_production_config_unchanged() -> None:
    path = Path(__file__).resolve().parents[2] / "config/instrument_relations.yml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))["relations"]
    loaded = instrument_relations.load_relations(path)
    assert len(loaded) == 26
    assert loaded["PSH.L"] == []
    assert {
        identifier: [
            {"name": rel.name, "aliases": list(rel.aliases), "relation": rel.relation}
            for rel in entries
        ]
        for identifier, entries in loaded.items()
    } == raw


def test_683_03_invalid_document_continues_direct_collection(
    db_session: Session, tmp_path: Path
) -> None:
    path = tmp_path / "relations.yml"
    path.write_text("- NVDA\n", encoding="utf-8")
    db_session.add(InstrumentProfile(identifier="NVDA", market="US", aliases=["NVDA"]))
    slot = IntelSlotRun(slot="post_close", run_date=NOW.date(), started_at=NOW, status="running")
    db_session.add(slot)
    db_session.flush()
    item = CollectedItem("NVDA opens a new chip factory", NOW, "https://fixture.example/direct")
    related_item = CollectedItem("TSMC raises wafer prices", NOW, "https://fixture.example/related")
    settings = get_settings().model_copy(update={"INTEL_COLLECT_WORKERS": 1})
    with (
        patch.object(instrument_relations, "_PATH", path),
        patch.object(capture, "get_settings", return_value=settings),
        patch.object(capture, "intel_universe", return_value=[UniverseEntry("NVDA", "NVDA", "US")]),
        patch.object(
            capture, "sources_for", return_value=[("yahoo", lambda: [item, related_item])]
        ),
        patch.object(capture, "classify_headlines", return_value=({0: "keep"}, 0.0, None)),
        patch.object(capture, "classify_related") as related,
        patch.object(
            capture,
            "SessionLocal",
            side_effect=lambda: Session(
                bind=db_session.connection(), join_transaction_mode="create_savepoint"
            ),
        ),
    ):
        run = capture.collect_slot_news(
            db_session, slot, NOW, 60, lambda identifier, leads: None, profile_errors=[]
        )
    assert run.status != "failed"
    assert run.status == "partial" and run.instruments_processed == 1
    assert "relations: ValueError" in run.errors
    assert db_session.query(News).one().record["title"] == item.title
    link = db_session.query(NewsInstrument).one()
    assert link.identifier == "NVDA" and link.relation is None and link.related_to is None
    related.assert_not_called()


def test_683_04_weekly_yaml_round_trip() -> None:
    values = [
        'Star"link',
        r"A\B",
        "Name: division",
        "#tag",
        "- leading",
        "\u4e2d\u9645\u65ed\u521b",
    ]
    relation = {"name": 'O"Brien Labs', "aliases": values, "relation": "competitor"}
    details: dict[str, object] = {
        "names": [{"identifier": "SPCX", "name": v, "count": 1} for v in values],
        "relations": [{"identifier": "SPCX", "entity": r"A\B", "relation": "supplier", "count": 2}],
        "suggestions": {
            "SPCX": {"aliases": [values[0], "extra"], "relations": []},
            "NEW": {"aliases": values, "relations": [relation]},
            "EMPTY": {"aliases": [], "relations": []},
        },
    }
    lines = name_check.render_weekly(details)
    yaml_lines = lines[lines.index(MARKER) + 1 : lines.index("Problems: none")]
    assert all(line.startswith("  ") for line in yaml_lines)
    document = yaml.safe_load("\n".join(line[2:] for line in yaml_lines))
    assert document == {
        "entity_aliases": {"SPCX": [*values, "extra"], "NEW": values},
        "relations": {
            "SPCX": [{"name": r"A\B", "aliases": [r"A\B"], "relation": "supplier"}],
            "NEW": [relation],
            "EMPTY": [],
        },
    }
    assert list(document["entity_aliases"]) == ["SPCX", "NEW"]
    assert list(document["relations"]) == ["SPCX", "NEW", "EMPTY"]
    assert values[-1] in "\n".join(yaml_lines)


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        ({"names": [{"identifier": "X", "name": "Alias"}]}, {"entity_aliases": {"X": ["Alias"]}}),
        ({"suggestions": {"X": {"aliases": [], "relations": []}}}, {"relations": {"X": []}}),
    ],
)
def test_683_04b_weekly_yaml_omits_empty_sections(
    details: dict[str, object], expected: dict[str, object]
) -> None:
    lines = name_check.render_weekly(details)
    block = lines[lines.index(MARKER) + 1 : lines.index("Problems: none")]
    assert yaml.safe_load("\n".join(line[2:] for line in block)) == expected
