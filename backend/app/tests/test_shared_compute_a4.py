"""Cross-user assembly and regeneration regressions after #622."""

from __future__ import annotations

import contextlib
from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.report import Report
from app.services import report_generator as rg
from app.tests.conftest import SHARED_COMPUTE_NOW
from app.tests.test_shared_compute_a1 import _seed_price_snapshots

_ASSEMBLY_MODEL = "shadow/cheap"
_PASS2_MARKER = "ZZZ_PASS2_BODY_ZZZ"
_ASSEMBLY_MARKER = "ZZZ_ASSEMBLED_BODY_ZZZ"
_BODY = (
    "## §2 Macro Events\n\nNothing.\n\n## §3 Holdings Context\n\n%s\n\n## §4 Exposure & Price Data\n\nFacts.\n\n"
    + ("filler " * 500)
)


def _reports(db_session: Session) -> dict[Any, Report]:
    rows = (
        db_session.execute(select(Report).where(Report.session_node != "fixture_seed"))
        .scalars()
        .all()
    )
    return {row.user_id: row for row in rows}


def _run_batch(*, shared: bool, clusters: bool = False) -> None:
    from app.tasks.report_tasks import generate_incremental_report

    settings = get_settings()

    def read_l1(_session: object, identifiers: list[str], _trade_date: date) -> dict[str, str]:
        return {identifier: "Scheduled fact. [Established]" for identifier in identifiers}

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(settings, "SHARED_COMPUTE_ENABLED", shared))
        stack.enter_context(patch.object(settings, "ASSEMBLY_LLM_MODEL", _ASSEMBLY_MODEL))
        stack.enter_context(
            patch(
                "app.services.report_generator.intel_trade_date",
                return_value=SHARED_COMPUTE_NOW.date(),
            )
        )
        stack.enter_context(
            patch("app.services.report_generator.read_l1_intel", side_effect=read_l1)
        )
        stack.enter_context(patch("app.services.report_generator.read_l2_intel", return_value={}))
        stack.enter_context(
            patch(
                "app.services.report_generator.read_day_synthesis",
                return_value=[
                    {
                        "identifiers": ["NVDA", "AAPL", "SGOL"],
                        "summary": "The long end repriced and the whole channel followed.",
                        "confidence": "Probable",
                    }
                ]
                if clusters
                else [],
            )
        )
        stack.enter_context(
            patch("app.services.report_generator._openrouter_client", return_value=MagicMock())
        )
        stack.enter_context(
            patch("app.services.report_generator._call_llm", return_value=_BODY % _PASS2_MARKER)
        )
        stack.enter_context(
            patch("app.services.report_generator._run_tavily_search", return_value=[])
        )
        stack.enter_context(
            patch("app.services.report_translation._openrouter_client", return_value=MagicMock())
        )
        stack.enter_context(
            patch("app.services.report_translation._call_llm", return_value="translated " * 500)
        )
        stack.enter_context(patch("app.services.report_translation.time.sleep"))
        stack.enter_context(
            patch("app.services.report_assembly._call_llm", return_value=_BODY % _ASSEMBLY_MARKER)
        )
        result = generate_incremental_report.run()
    assert result["status"] == "completed"


def test_assembled_reports_never_carry_another_users_holdings(
    db_session: Session, three_user_holdings: dict[str, Any]
) -> None:
    _seed_price_snapshots(db_session)
    _run_batch(shared=True)
    reports = _reports(db_session)
    u1 = reports[three_user_holdings["U1"]]
    u3 = reports[three_user_holdings["U3"]]
    assert u1.report_inputs is not None and u3.report_inputs is not None
    assert "NVDA" in u1.report_inputs["assembly_prompt"]
    assert "SGOL" not in u1.report_inputs["assembly_prompt"]
    assert "SGOL" in u3.report_inputs["assembly_prompt"]
    assert "NVDA" not in u3.report_inputs["assembly_prompt"]
    assert u1.report_md is not None and u3.report_md is not None
    assert "SGOL" not in u1.report_md and "NVDA" not in u3.report_md


def test_uat8_disabled_switch_reproduces_the_pre_a4_batch(
    db_session: Session, three_user_holdings: dict[str, Any]
) -> None:
    _seed_price_snapshots(db_session)
    _run_batch(shared=False)
    for report in _reports(db_session).values():
        assert report.report_inputs is not None
        assert report.report_inputs["body_source"] == "pass2"
        assert report.report_inputs["assembly_raw"] == ""
        assert report.report_md is not None and _ASSEMBLY_MARKER not in report.report_md


def test_cross_name_clusters_never_carry_another_users_holdings(
    db_session: Session, three_user_holdings: dict[str, Any]
) -> None:
    _seed_price_snapshots(db_session)
    _run_batch(shared=True, clusters=True)
    reports = _reports(db_session)
    u1 = reports[three_user_holdings["U1"]]
    u3 = reports[three_user_holdings["U3"]]
    assert u1.report_inputs is not None and u3.report_inputs is not None
    assert u1.report_inputs["cross_name_intel"]
    assert sorted(u1.report_inputs["cross_name_intel"][0]["identifiers"]) == ["AAPL", "NVDA"]
    for report, foreign in ((u1, "SGOL"), (u3, "NVDA")):
        inputs = report.report_inputs
        assert inputs is not None
        for cluster in inputs["cross_name_intel"]:
            assert foreign not in cluster["identifiers"]
            assert foreign not in cluster["summary"]
        assert report.report_md is not None
        assert foreign not in report.report_md


def test_uat9_assembled_report_rerenders_with_zero_llm_calls(
    db_session: Session, three_user_holdings: dict[str, Any]
) -> None:
    _seed_price_snapshots(db_session)
    _run_batch(shared=True)
    report = _reports(db_session)[three_user_holdings["U1"]]
    assert report.report_inputs is not None
    assert report.report_inputs["body_source"] == "assembly"
    with (
        patch("app.services.report_generator._call_llm") as pass2,
        patch("app.services.report_assembly._call_llm") as assembly,
        patch("app.services.report_translation._call_llm") as translate,
    ):
        rebuilt = rg.regenerate_report(
            db_session,
            report.id,
            user_id=three_user_holdings["U1"],
            mode="render",
            output_lang="en",
        )
    pass2.assert_not_called()
    assembly.assert_not_called()
    translate.assert_not_called()
    assert rebuilt.report_md is not None and _ASSEMBLY_MARKER in rebuilt.report_md
