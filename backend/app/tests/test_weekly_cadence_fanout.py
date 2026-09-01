"""Issue #191 regression: a weekly, zero-holdings user must get the #221 §8
empty-table content contract when reached through the REAL Beat/fan-out path
(generate_incremental_report -> active_user_ids), not just via the admin
manual-generate path that test_generate_report_empty_book_content_contract
(test_report_generator.py) already covers by calling generate_report()
directly.

Follows the same real-DB / mocked-LLM-boundary pattern as
test_shared_compute_a1.py's _run_batch — SessionLocal is rebound to
db_session by the db_session fixture itself (conftest.py), so
generate_incremental_report.run() operates on the same real transaction the
test seeds into.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.forward_event import ForwardEvent
from app.models.report import Report
from app.tests.test_report_generator import _macro_hit, _mock_llm, _news_item
from app.tests.test_shared_compute_a1 import _empty_portfolio_snap
from app.tests.test_user_scope import _U1, _user


def test_weekly_zero_holdings_user_gets_empty_table_contract_via_beat_path(
    db_session: Session,
) -> None:
    """Not a quiet day (a macro hit is seeded, matching
    test_generate_report_empty_book_content_contract in
    test_report_generator.py) — otherwise the same no-macro-hit/no-anomalies
    quiet-day rule that applies to every user would skip this report and the
    content contract below would never actually render.

    OUTPUT_LANG forced to "en" (generate_incremental_report otherwise passes
    the zh default through to a real translation pass): this test's target
    is the content contract reaching the report through the real Beat/
    active_user_ids path, not the translation pipeline, which has its own
    coverage elsewhere. Uses test_report_generator's `_mock_llm` (not
    test_shared_compute_a1's) since that one returns real §1-preserving
    Pass 1/2 content instead of a generic §2-§4-only filler string.
    """
    get_settings().OUTPUT_LANG = "en"
    # Issue #276: the fan-out now requires a verified address; this user is
    # an existing book stand-in, so their account email is verified.
    db_session.add(
        _user(
            _U1,
            "empty-book@example.com",
            cadence="weekly",
            email_verified_at=datetime(2026, 8, 31, 12, 0),
        )
    )
    db_session.add(
        ForwardEvent(
            event_type="macro",
            name="FOMC Meeting",
            ticker="",
            scheduled_date=date.today() + timedelta(days=1),
            source="fomc",
        )
    )
    db_session.flush()

    from app.tasks.report_tasks import generate_incremental_report

    with (
        patch(
            "app.services.report_generator.compute_portfolio", return_value=_empty_portfolio_snap()
        ),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
        patch("app.services.report_generator._run_tavily_search", return_value=[]),
    ):
        result = generate_incremental_report.run(
            report_type="incremental", session_node="weekend_snapshot", cadence="weekly"
        )

    assert result["status"] == "completed"
    assert result["results"][0]["status"] == "success"

    report = db_session.execute(
        select(Report).where(Report.user_id == _U1, Report.session_node == "weekend_snapshot")
    ).scalar_one()
    assert report.report_md is not None
    assert "§1 Portfolio Snapshot" in report.report_md
    assert "USD 0" in report.report_md  # zero total, no crash on an empty book
    assert "§2.5 Forward Calendar" in report.report_md
    assert "FOMC Meeting" in report.report_md
    assert "| FOMC Meeting | —" in report.report_md  # no holding to expose it to
