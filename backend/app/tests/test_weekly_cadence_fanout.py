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

from app.models.forward_event import ForwardEvent
from app.models.report import Report
from app.tests.test_report_generator import _macro_hit, _mock_llm, _news_item
from app.tests.test_shared_compute_a1 import _empty_portfolio_snap
from app.tests.test_user_scope import _U1, _U2, _user


def test_weekly_zero_holdings_user_gets_empty_table_contract_via_beat_path(
    db_session: Session,
) -> None:
    """Not a quiet day (a macro hit is seeded, matching
    test_generate_report_empty_book_content_contract in
    test_report_generator.py) — otherwise the same no-macro-hit/no-anomalies
    quiet-day rule that applies to every user would skip this report and the
    content contract below would never actually render.

    Seeded user's own `locale="en"` (issue #308: generate_incremental_report
    now reads each recipient's own report language, not the global
    Settings.OUTPUT_LANG default) — this test's target is the content
    contract reaching the report through the real Beat/active_users path,
    not the translation pipeline, which has its own coverage elsewhere.
    Before #308 this was achieved by forcing the global OUTPUT_LANG to "en"
    instead; that global override no longer has any effect on this path
    (deliberately — a stale mutation left in place would otherwise silently
    stop testing anything once #308 shipped) and was removed. Uses
    test_report_generator's `_mock_llm` (not test_shared_compute_a1's)
    since that one returns real §1-preserving Pass 1/2 content instead of a
    generic §2-§4-only filler string.
    """
    # Issue #276: the fan-out now requires a verified address; this user is
    # an existing book stand-in, so their account email is verified.
    db_session.add(
        _user(
            _U1,
            "empty-book@example.com",
            cadence="weekly",
            email_verified_at=datetime(2026, 8, 31, 12, 0),
            locale="en",
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


def test_weekly_fanout_two_users_each_get_their_own_locale_after_the_first_users_commit(
    db_session: Session,
) -> None:
    """blacktomb42 PR #309 round-1 review, finding 2: the fan-out loop must
    read each recipient's locale from a snapshot taken ONCE before the
    loop, never from a live `User` ORM object after an earlier iteration's
    `generate_report` call has already `session.commit()`'d — with
    `expire_on_commit` defaulting to True (this session included, via
    `db_session`'s `Session(...)` construction in conftest.py), a live
    object's attribute read on a LATER iteration would trigger an implicit
    refresh query on this same session, the same class of interleaved-
    query hang already fixed once for `active_users` itself.

    The single-user test above cannot catch this — a batch of one has no
    "later iteration" to observe. Two users, sorted by id (`active_users`'
    contract) so U1 (`...b1`, locale="en") processes BEFORE U2 (`...b2`,
    locale="zh") — U2's locale must still resolve correctly (and drive a
    REAL translation pass) after U1's own `generate_report` call already
    committed. Asserted by tracking the real `report_translation._call_llm`
    boundary directly: it must never fire for U1 (`en` is a no-op) and
    must fire for U2 (`zh` requires translation) — proof the SECOND
    iteration's own locale reached `generate_report`, not a stale/expired
    read or U1's language leaking across.
    """
    verified_at = datetime(2026, 8, 31, 12, 0)
    db_session.add_all(
        [
            _user(
                _U1,
                "en-user@example.com",
                cadence="weekly",
                email_verified_at=verified_at,
                locale="en",
            ),
            _user(
                _U2,
                "zh-user@example.com",
                cadence="weekly",
                email_verified_at=verified_at,
                locale="zh",
            ),
        ]
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

    translate_llm = MagicMock(side_effect=_mock_llm)

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
        patch("app.services.report_translation._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_translation._call_llm", translate_llm),
        patch("app.services.report_translation.time.sleep"),  # skip the real per-chunk pacing
    ):
        result = generate_incremental_report.run(
            report_type="incremental", session_node="weekend_snapshot", cadence="weekly"
        )

    assert result["status"] == "completed"
    by_user = {r["user_id"]: r["status"] for r in result["results"]}
    assert by_user[str(_U1)] == "success"
    assert by_user[str(_U2)] == "success"

    en_report = db_session.execute(
        select(Report).where(Report.user_id == _U1, Report.session_node == "weekend_snapshot")
    ).scalar_one()
    zh_report = db_session.execute(
        select(Report).where(Report.user_id == _U2, Report.session_node == "weekend_snapshot")
    ).scalar_one()
    assert en_report.report_md is not None
    assert zh_report.report_md is not None
    # The real proof: translation only ran for the zh user. If U2's locale
    # snapshot had been lost/corrupted/defaulted back to "en" (the bug this
    # test guards against), this call count would be 0.
    assert translate_llm.call_count > 0
