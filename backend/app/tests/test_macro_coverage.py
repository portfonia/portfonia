"""Tests for macro_coverage.py (issue #440): sidecar parse/strip, the
continuity read (success-only, self-excluding, most-recent-per-key), and
persist's delete-then-insert replace semantics."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.macro_coverage import MacroCoverage
from app.models.report import Report
from app.services import macro_coverage as mc
from app.tests.conftest import seed_user

_USER = uuid.UUID("00000000-0000-0000-0000-0000000004a0")


@pytest.fixture(autouse=True)
def _seed_test_user(db_session: Session) -> None:
    seed_user(db_session, _USER)


def _make_report(
    db_session: Session,
    *,
    report_date: date,
    status: str = "success",
    period_end: datetime | None = None,
) -> Report:
    report = Report(
        user_id=_USER,
        report_date=report_date,
        report_type="incremental",
        session_node="manual",
        status=status,
        period_start=datetime(2026, 1, 1, tzinfo=UTC),
        period_end=period_end or datetime.combine(report_date, datetime.min.time(), tzinfo=UTC),
    )
    db_session.add(report)
    db_session.flush()
    return report


def _sidecar(items: list[dict[str, object]]) -> str:
    return f"{mc._SIDECAR_START}\n{json.dumps({'items': items})}\n{mc._SIDECAR_END}"


def _raw_item(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "development_key": "Fed Rate Path",
        "theme_keys": ["monetary-policy"],
        "coverage_mode": "NEW",
        "depth_tier": "anchor",
        "facts_covered": ["fact one"],
        "explanations_covered": ["explanation one"],
        "open_questions": ["will the pace change next quarter?"],
        "observables": ["next FOMC statement wording"],
        "affected_identifiers": ["AAPL"],
        "paragraph_excerpt": "The Fed signaled a slower pace...",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Sidecar extraction
# ---------------------------------------------------------------------------


def test_extract_macro_sidecar_strips_and_parses() -> None:
    body = "## §2 Macro Signals\n\nSome prose.\n\n" + _sidecar([_raw_item()])
    visible, items = mc.extract_macro_sidecar(body)

    assert mc._SIDECAR_START not in visible
    assert "MACRO_COVERAGE" not in visible
    assert "Some prose." in visible
    assert len(items) == 1
    assert items[0].development_key == "fed rate path"  # normalized
    assert items[0].coverage_mode == "NEW"
    assert items[0].depth_tier == "anchor"
    assert items[0].affected_identifiers == ["AAPL"]


def test_extract_macro_sidecar_missing_returns_body_unchanged() -> None:
    """No sidecar at all (an older prompt version, or a model that ignored
    the instruction) — must not raise, must not touch the body."""
    body = "## §2 Macro Signals\n\nSome prose with no sidecar."
    visible, items = mc.extract_macro_sidecar(body)
    assert visible == body
    assert items == []


def test_extract_macro_sidecar_malformed_json_strips_block_anyway() -> None:
    """A malformed sidecar must never leak into the rendered report — the
    block is still stripped even though nothing could be parsed from it."""
    body = f"Prose.\n\n{mc._SIDECAR_START}\nnot valid json{{\n{mc._SIDECAR_END}"
    visible, items = mc.extract_macro_sidecar(body)
    assert "MACRO_COVERAGE" not in visible
    assert "not valid json" not in visible
    assert items == []


def test_extract_macro_sidecar_drops_items_with_invalid_enum_values() -> None:
    body = _sidecar(
        [
            _raw_item(development_key="a", coverage_mode="BOGUS"),
            _raw_item(development_key="b", depth_tier="BOGUS"),
            _raw_item(development_key="c"),
        ]
    )
    _visible, items = mc.extract_macro_sidecar(body)
    assert [i.development_key for i in items] == ["c"]


def test_extract_macro_sidecar_empty_items_list_is_valid() -> None:
    body = _sidecar([])
    visible, items = mc.extract_macro_sidecar(body)
    assert items == []
    assert "MACRO_COVERAGE" not in visible


# ---------------------------------------------------------------------------
# Persist — delete-then-insert replace semantics
# ---------------------------------------------------------------------------


def test_persist_macro_coverage_replaces_not_upserts(db_session: Session) -> None:
    """Contract constraints "Regenerate after a topic was removed: No stale
    coverage for that report" — a second persist for the SAME report_id
    with a different item set must not leave the first set's rows behind."""
    report = _make_report(db_session, report_date=date(2026, 9, 1))
    as_of = datetime(2026, 9, 1, 21, 0, tzinfo=UTC)

    _visible, first_items = mc.extract_macro_sidecar(
        _sidecar([_raw_item(development_key="topic-a"), _raw_item(development_key="topic-b")])
    )
    mc.persist_macro_coverage(
        db_session, report_id=report.id, user_id=_USER, as_of=as_of, items=first_items
    )
    db_session.commit()
    rows = (
        db_session.execute(select(MacroCoverage).where(MacroCoverage.report_id == report.id))
        .scalars()
        .all()
    )
    assert {r.development_key for r in rows} == {"topic-a", "topic-b"}

    _visible, second_items = mc.extract_macro_sidecar(
        _sidecar([_raw_item(development_key="topic-a")])
    )
    mc.persist_macro_coverage(
        db_session, report_id=report.id, user_id=_USER, as_of=as_of, items=second_items
    )
    db_session.commit()
    rows = (
        db_session.execute(select(MacroCoverage).where(MacroCoverage.report_id == report.id))
        .scalars()
        .all()
    )
    assert {r.development_key for r in rows} == {"topic-a"}


def test_persist_macro_coverage_empty_items_creates_no_coverage(db_session: Session) -> None:
    """Design §5: "A report with no macro prose creates no macro coverage"."""
    report = _make_report(db_session, report_date=date(2026, 9, 2))
    mc.persist_macro_coverage(
        db_session,
        report_id=report.id,
        user_id=_USER,
        as_of=datetime(2026, 9, 2, tzinfo=UTC),
        items=[],
    )
    db_session.commit()
    rows = (
        db_session.execute(select(MacroCoverage).where(MacroCoverage.report_id == report.id))
        .scalars()
        .all()
    )
    assert rows == []


# ---------------------------------------------------------------------------
# Read — success-only eligibility, self-exclusion, most-recent-per-key
# ---------------------------------------------------------------------------


def test_load_recent_macro_coverage_excludes_non_success_reports(db_session: Session) -> None:
    success_report = _make_report(db_session, report_date=date(2026, 9, 1), status="success")
    needs_review_report = _make_report(
        db_session, report_date=date(2026, 9, 3), status="needs_review"
    )
    mc.persist_macro_coverage(
        db_session,
        report_id=success_report.id,
        user_id=_USER,
        as_of=datetime(2026, 9, 1, tzinfo=UTC),
        items=mc.extract_macro_sidecar(_sidecar([_raw_item(development_key="visible-topic")]))[1],
    )
    mc.persist_macro_coverage(
        db_session,
        report_id=needs_review_report.id,
        user_id=_USER,
        as_of=datetime(2026, 9, 3, tzinfo=UTC),
        items=mc.extract_macro_sidecar(_sidecar([_raw_item(development_key="hidden-topic")]))[1],
    )
    db_session.commit()

    result = mc.load_recent_macro_coverage(db_session, _USER)
    keys = {item["development_key"] for item in result}
    assert "visible-topic" in keys
    assert "hidden-topic" not in keys


def test_load_recent_macro_coverage_self_excludes_report(db_session: Session) -> None:
    report = _make_report(db_session, report_date=date(2026, 9, 5), status="success")
    mc.persist_macro_coverage(
        db_session,
        report_id=report.id,
        user_id=_USER,
        as_of=datetime(2026, 9, 5, tzinfo=UTC),
        items=mc.extract_macro_sidecar(_sidecar([_raw_item(development_key="self-topic")]))[1],
    )
    db_session.commit()

    result = mc.load_recent_macro_coverage(db_session, _USER, exclude_report_id=report.id)
    assert result == []
    result_unfiltered = mc.load_recent_macro_coverage(db_session, _USER)
    assert any(item["development_key"] == "self-topic" for item in result_unfiltered)


def test_load_recent_macro_coverage_keeps_most_recent_per_development_key(
    db_session: Session,
) -> None:
    older = _make_report(
        db_session,
        report_date=date(2026, 9, 1),
        status="success",
        period_end=datetime(2026, 9, 1, 17, 0, tzinfo=UTC),
    )
    newer = _make_report(
        db_session,
        report_date=date(2026, 9, 5),
        status="success",
        period_end=datetime(2026, 9, 5, 17, 0, tzinfo=UTC),
    )
    mc.persist_macro_coverage(
        db_session,
        report_id=older.id,
        user_id=_USER,
        as_of=datetime(2026, 9, 1, 17, 0, tzinfo=UTC),
        items=mc.extract_macro_sidecar(
            _sidecar([_raw_item(development_key="fed-path", depth_tier="anchor")])
        )[1],
    )
    mc.persist_macro_coverage(
        db_session,
        report_id=newer.id,
        user_id=_USER,
        as_of=datetime(2026, 9, 5, 17, 0, tzinfo=UTC),
        items=mc.extract_macro_sidecar(
            _sidecar([_raw_item(development_key="fed-path", depth_tier="update")])
        )[1],
    )
    db_session.commit()

    result = mc.load_recent_macro_coverage(db_session, _USER)
    fed_rows = [item for item in result if item["development_key"] == "fed-path"]
    assert len(fed_rows) == 1
    assert fed_rows[0]["depth_tier"] == "update"


def test_load_recent_macro_coverage_empty_for_cold_user(db_session: Session) -> None:
    assert mc.load_recent_macro_coverage(db_session, _USER) == []


# ---------------------------------------------------------------------------
# Continuity block rendering
# ---------------------------------------------------------------------------


def test_render_macro_continuity_block_empty_is_omitted() -> None:
    assert mc.render_macro_continuity_block([]) == ""


def test_render_macro_continuity_block_contains_key_fields() -> None:
    block = mc.render_macro_continuity_block(
        [
            {
                "development_key": "fed-path",
                "as_of": "2026-09-05T17:00:00+00:00",
                "depth_tier": "anchor",
                "coverage_mode": "NEW",
                "open_questions": ["will cuts continue?"],
                "observables": ["next dot plot"],
                "paragraph_excerpt": "The Fed cut rates...",
            }
        ]
    )
    assert "MACRO COVERAGE CONTINUITY" in block
    assert "fed-path" in block
    assert "will cuts continue?" in block
    assert "next dot plot" in block
    assert "never restate it verbatim" in block
